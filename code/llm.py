"""Gemini LLM wrapper module using google-genai SDK.

Features:
- Fixed temperature 0.0, timeout support.
- Exponential backoff retry on HTTP 429 and 5xx (max 3 retries).
- Typed QuotaExhausted exception.
- Disk caching in cache/llm/ keyed by:
  sha256(model + prompt + tool schemas + response schema + image bytes hash).
- Usage tracking integrated via usage.py.
"""

from dataclasses import dataclass, field
import hashlib
import json
import logging
import os
from pathlib import Path
import sys
import time
from typing import Any, Callable, Dict, List, Optional, Union

from google import genai
from google.genai import errors, types

_current_dir = Path(__file__).resolve().parent
if str(_current_dir) not in sys.path:
    sys.path.insert(0, str(_current_dir))

import config
import usage

logger = logging.getLogger(__name__)


class QuotaExhausted(Exception):
    """Raised when API quota or rate limits (HTTP 429 / RESOURCE_EXHAUSTED) are exhausted."""
    pass


@dataclass
class FunctionCall:
    name: str
    args: Dict[str, Any]


@dataclass
class LLMResult:
    text: str
    function_calls: List[FunctionCall] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    cache_hit: bool = False
    latency_ms: float = 0.0


def compute_cache_key(
    model: str,
    prompt: str,
    tools: Optional[Any] = None,
    response_schema: Optional[Any] = None,
    image_bytes: Optional[bytes] = None,
) -> str:
    """Compute sha256(model + prompt + tool schemas + response schema + image bytes hash)."""
    # 1. tool schemas string
    if tools is None:
        tool_schemas_str = ""
    elif isinstance(tools, (list, tuple, dict)):
        try:
            tool_schemas_str = json.dumps(tools, sort_keys=True, default=str)
        except Exception:
            tool_schemas_str = str(tools)
    else:
        tool_schemas_str = str(tools)

    # 2. response schema string
    if response_schema is None:
        response_schema_str = ""
    elif hasattr(response_schema, "model_json_schema"):
        response_schema_str = json.dumps(response_schema.model_json_schema(), sort_keys=True)
    elif isinstance(response_schema, dict):
        response_schema_str = json.dumps(response_schema, sort_keys=True)
    else:
        response_schema_str = str(response_schema)

    # 3. image bytes hash
    if image_bytes is None:
        image_bytes_hash = ""
    else:
        image_bytes_hash = hashlib.sha256(image_bytes).hexdigest()

    raw_key = model + prompt + tool_schemas_str + response_schema_str + image_bytes_hash
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


def get_client(api_key: Optional[str] = None) -> genai.Client:
    """Instantiate a google-genai Client with the configured API key."""
    key = api_key or config.get_gemini_api_key()
    return genai.Client(api_key=key)


def _is_rate_limit_error(exc: Exception) -> bool:
    code = getattr(exc, "code", None)
    if code == 429:
        return True
    msg = str(exc)
    return "429" in msg or "RESOURCE_EXHAUSTED" in msg or "quota" in msg.lower()


def _is_server_error(exc: Exception) -> bool:
    if isinstance(exc, errors.ServerError):
        return True
    code = getattr(exc, "code", None)
    if isinstance(code, int) and 500 <= code < 600:
        return True
    msg = str(exc)
    return any(c in msg for c in ["500", "502", "503", "504", "Server Error"])


def generate(
    prompt: str,
    model: Optional[str] = None,
    purpose: str = "general",
    tools: Optional[Any] = None,
    response_schema: Optional[Any] = None,
    image_bytes: Optional[bytes] = None,
    image_mime_type: str = "image/png",
    temperature: float = 0.0,
    timeout_ms: int = 60000,
    max_retries: int = 3,
    use_cache: bool = True,
    client: Optional[Any] = None,
    cache_dir: Optional[Path] = None,
    ledger_path: Optional[Path] = None,
) -> LLMResult:
    """Generate content with Gemini, disk caching, retries, and usage logging.

    Args:
        prompt: Text instruction or prompt.
        model: Model name. Defaults to config.get_model_agent().
        purpose: Label for usage logging.
        tools: Optional tool definitions or functions for function calling.
        response_schema: Optional Pydantic model or schema for structured output.
        image_bytes: Optional raw bytes of an image to analyze.
        image_mime_type: MIME type of image (default: 'image/png').
        temperature: Temperature setting (fixed at 0.0).
        timeout_ms: Request timeout in milliseconds (default: 60000).
        max_retries: Max retries for 429 and 5xx errors (default: 3).
        use_cache: Whether to use disk cache (default: True).
        client: Optional pre-configured genai Client (useful for tests/mocking).
        cache_dir: Optional custom cache directory.
        ledger_path: Optional custom usage ledger file path.
    """
    model_name = model or config.get_model_agent()
    c_dir = cache_dir or config.CACHE_LLM_DIR
    c_dir.mkdir(parents=True, exist_ok=True)

    cache_key = compute_cache_key(
        model=model_name,
        prompt=prompt,
        tools=tools,
        response_schema=response_schema,
        image_bytes=image_bytes,
    )
    cache_file = c_dir / f"{cache_key}.json"

    t0 = time.perf_counter()

    # Check disk cache
    if use_cache and cache_file.exists():
        try:
            cached_data = json.loads(cache_file.read_text(encoding="utf-8"))
            latency_ms = (time.perf_counter() - t0) * 1000.0
            fc_list = [
                FunctionCall(name=fc["name"], args=fc.get("args", {}))
                for fc in cached_data.get("function_calls", [])
            ]
            res = LLMResult(
                text=cached_data.get("text", ""),
                function_calls=fc_list,
                input_tokens=cached_data.get("input_tokens", 0),
                output_tokens=cached_data.get("output_tokens", 0),
                cache_hit=True,
                latency_ms=latency_ms,
            )
            usage.log_usage(
                purpose=purpose,
                model=model_name,
                input_tokens=res.input_tokens,
                output_tokens=res.output_tokens,
                cache_hit=True,
                latency_ms=latency_ms,
                ledger_path=ledger_path,
            )
            return res
        except Exception:
            # If cache read fails, fall through to live API call
            pass

    # Build contents
    contents: List[Any] = []
    if image_bytes is not None:
        contents.append(types.Part.from_bytes(data=image_bytes, mime_type=image_mime_type))
    contents.append(prompt)

    # Build GenerateContentConfig
    config_args: Dict[str, Any] = {
        "temperature": temperature,
        "http_options": types.HttpOptions(timeout=timeout_ms),
    }

    if response_schema is not None:
        config_args["response_mime_type"] = "application/json"
        config_args["response_schema"] = response_schema

    if tools is not None:
        config_args["tools"] = tools

    gen_config = types.GenerateContentConfig(**config_args)

    active_client = client or get_client()

    # Live call with exponential backoff on 429 and 5xx
    last_error: Optional[Exception] = None
    for attempt in range(max_retries + 1):
        try:
            response = active_client.models.generate_content(
                model=model_name,
                contents=contents,
                config=gen_config,
            )
            break
        except Exception as e:
            last_error = e
            rate_limit_flag = _is_rate_limit_error(e)
            server_error_flag = _is_server_error(e)

            if (rate_limit_flag or server_error_flag) and attempt < max_retries:
                backoff_delay = 1.0 * (2 ** attempt)
                logger.warning(
                    f"LLM call encountered error ({e}). Retrying in {backoff_delay:.1f}s (attempt {attempt + 1}/{max_retries})..."
                )
                time.sleep(backoff_delay)
                continue
            elif rate_limit_flag:
                raise QuotaExhausted(f"Gemini API rate limit or quota exceeded: {e}") from e
            else:
                raise e
    else:
        if last_error and _is_rate_limit_error(last_error):
            raise QuotaExhausted(f"Gemini API rate limit or quota exceeded after retries: {last_error}") from last_error
        raise last_error or RuntimeError("LLM call failed without explicit error.")

    latency_ms = (time.perf_counter() - t0) * 1000.0

    # Parse response
    resp_text = response.text or ""
    fcs: List[FunctionCall] = []
    if hasattr(response, "function_calls") and response.function_calls:
        for fc in response.function_calls:
            fcs.append(FunctionCall(name=fc.name, args=dict(fc.args or {})))

    inp_tokens = 0
    out_tokens = 0
    if hasattr(response, "usage_metadata") and response.usage_metadata:
        inp_tokens = getattr(response.usage_metadata, "prompt_token_count", 0) or 0
        out_tokens = getattr(response.usage_metadata, "candidates_token_count", 0) or 0

    # Write to disk cache
    if use_cache:
        cache_payload = {
            "text": resp_text,
            "function_calls": [{"name": fc.name, "args": fc.args} for fc in fcs],
            "input_tokens": inp_tokens,
            "output_tokens": out_tokens,
        }
        cache_file.write_text(json.dumps(cache_payload, indent=2), encoding="utf-8")

    # Record in usage ledger
    usage.log_usage(
        purpose=purpose,
        model=model_name,
        input_tokens=inp_tokens,
        output_tokens=out_tokens,
        cache_hit=False,
        latency_ms=latency_ms,
        ledger_path=ledger_path,
    )

    return LLMResult(
        text=resp_text,
        function_calls=fcs,
        input_tokens=inp_tokens,
        output_tokens=out_tokens,
        cache_hit=False,
        latency_ms=latency_ms,
    )

"""Unified LLM wrapper module supporting Google Gemini and OpenAI-compatible (e.g. NVIDIA NIM) backends.

Features:
- Backends: 'openai_compat' (NVIDIA NIM / OpenAI-compatible) and 'gemini' (google-genai SDK).
- Unified interface: text generation, tool calling, image input, structured JSON output.
- Disk caching in cache/llm/ keyed by:
  sha256(model + prompt + tool schemas + response schema + image bytes hash).
- Retry up to max 3 with exponential backoff and Retry-After support; 120s timeout.
- Typed QuotaExhausted exception (daily-quota 429 raises immediately with no retries).
- Usage tracking integrated via usage.py recording provider, model, prompt/completion tokens.
"""

import base64
from dataclasses import dataclass, field
import hashlib
import inspect
import json
import logging
import os
from pathlib import Path
import sys
import time
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

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


def get_gemini_client(api_key: Optional[str] = None):
    """Instantiate a google-genai Client with configured API key."""
    from google import genai
    key = api_key or config.get_gemini_api_key()
    return genai.Client(api_key=key)


def get_openai_client(api_key: Optional[str] = None, base_url: Optional[str] = None):
    """Instantiate an OpenAI Client with configured API key and base URL (NIM default)."""
    from openai import OpenAI
    key = api_key or config.get_nvidia_api_key()
    url = base_url or os.getenv("NVIDIA_BASE_URL", "https://integrate.api.nvidia.com/v1")
    return OpenAI(api_key=key, base_url=url, timeout=120.0)


# Backward compatibility
get_client = get_gemini_client


def python_func_to_openai_tool(fn: Callable) -> Dict[str, Any]:
    """Convert a Python callable to an OpenAI function tool specification."""
    sig = inspect.signature(fn)
    doc = (fn.__doc__ or "").strip()
    params_properties = {}
    required = []
    type_map = {
        int: "integer",
        float: "number",
        str: "string",
        bool: "boolean",
        list: "array",
        dict: "object",
    }
    for p_name, param in sig.parameters.items():
        p_type = type_map.get(param.annotation, "string")
        params_properties[p_name] = {"type": p_type}
        if param.default is inspect.Parameter.empty:
            required.append(p_name)

    return {
        "type": "function",
        "function": {
            "name": fn.__name__,
            "description": doc,
            "parameters": {
                "type": "object",
                "properties": params_properties,
                "required": required,
            },
        },
    }


def _determine_provider(model: str, explicit_provider: Optional[str] = None, purpose: str = "general") -> str:
    """Determine whether to use 'gemini' or 'openai_compat' backend."""
    if explicit_provider:
        p = explicit_provider.lower()
        if p in ["openai_compat", "nim", "nvidia", "openai"]:
            return "openai_compat"
        return "gemini"

    # Check env by purpose
    p_lower = purpose.lower()
    if "vision" in p_lower or "png" in p_lower:
        prov = config.get_provider_vision()
    elif "escalat" in p_lower:
        prov = config.get_provider_escalation()
    elif "agent" in p_lower:
        prov = config.get_provider_agent()
    else:
        prov = os.getenv("LLM_PROVIDER_AGENT", "openai_compat")

    if prov and prov.lower() in ["openai_compat", "nim", "nvidia", "openai"]:
        return "openai_compat"
    if "gemini" in model.lower():
        return "gemini"
    return "openai_compat"


def _generate_openai_compat(
    prompt: str,
    model: str,
    tools: Optional[Any] = None,
    response_schema: Optional[Any] = None,
    image_bytes: Optional[bytes] = None,
    image_mime_type: str = "image/png",
    temperature: float = 0.0,
    timeout_s: float = 120.0,
    max_retries: int = 3,
    client: Optional[Any] = None,
) -> Tuple[str, List[FunctionCall], int, int]:
    """Execute completion call against OpenAI-compatible endpoint with retries and tool conversion."""
    # 1. Build messages
    if image_bytes is not None:
        b64_img = base64.b64encode(image_bytes).decode("utf-8")
        user_content = [
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {"url": f"data:{image_mime_type};base64,{b64_img}"}},
        ]
    else:
        user_content = prompt

    messages: List[Dict[str, Any]] = [{"role": "user", "content": user_content}]

    # 2. Build tools
    openai_tools = None
    if tools:
        openai_tools = []
        for t in tools:
            if isinstance(t, dict):
                if "type" in t and "function" in t:
                    openai_tools.append(t)
                elif "name" in t:
                    openai_tools.append({"type": "function", "function": t})
            elif callable(t):
                openai_tools.append(python_func_to_openai_tool(t))

    # 3. Response schema handling
    response_format = None
    if response_schema is not None:
        if hasattr(response_schema, "model_json_schema"):
            schema_dict = response_schema.model_json_schema()
        elif isinstance(response_schema, dict):
            schema_dict = response_schema
        else:
            schema_dict = str(response_schema)
        schema_str = json.dumps(schema_dict)
        if isinstance(user_content, str):
            messages[0]["content"] += f"\n\nOutput strictly valid JSON matching this schema: {schema_str}"
        elif isinstance(user_content, list):
            messages[0]["content"][0]["text"] += f"\n\nOutput strictly valid JSON matching this schema: {schema_str}"
        response_format = {"type": "json_object"}

    active_client = client or get_openai_client()
    last_error: Optional[Exception] = None

    for attempt in range(max_retries + 1):
        try:
            kwargs: Dict[str, Any] = {
                "model": model,
                "messages": messages,
                "temperature": temperature,
                "timeout": timeout_s,
            }
            if openai_tools:
                kwargs["tools"] = openai_tools
            if response_format:
                kwargs["response_format"] = response_format

            response = active_client.chat.completions.create(**kwargs)
            break
        except Exception as e:
            last_error = e
            err_msg = str(e).lower()
            status_code = getattr(e, "status_code", None)

            # If model rejects json_object response_format, fallback to prompt-only JSON
            if status_code == 400 and response_format and "response_format" in err_msg:
                response_format = None
                continue

            # 429 Rate Limit / Quota check
            is_429 = (status_code == 429) or ("429" in err_msg) or ("rate limit" in err_msg)
            if is_429:
                if any(q in err_msg for q in ["daily", "quota", "credit", "balance", "insufficient", "exceeded your current quota", "limit: 0"]):
                    raise QuotaExhausted(f"Daily quota or credits exhausted: {e}") from e

                retry_after: Optional[float] = None
                if hasattr(e, "response") and e.response is not None:
                    h = getattr(e.response, "headers", {})
                    if "retry-after" in h:
                        try:
                            retry_after = float(h["retry-after"])
                        except Exception:
                            pass

                if retry_after is not None and retry_after > 30:
                    raise QuotaExhausted(f"Rate limit retry-after too high ({retry_after}s): {e}") from e

                if attempt < max_retries:
                    backoff = retry_after if retry_after is not None else (1.5 * (2 ** attempt))
                    logger.warning(f"Rate limit on {model} (attempt {attempt+1}/{max_retries}). Retrying in {backoff:.1f}s...")
                    time.sleep(backoff)
                    continue
                else:
                    raise QuotaExhausted(f"Rate limit exhausted after retries: {e}") from e

            # Server errors (5xx) or network timeouts
            is_server_err = (status_code and 500 <= status_code < 600) or any(
                c in err_msg for c in ["500", "502", "503", "504", "connection", "timeout", "timed out"]
            )
            if is_server_err and attempt < max_retries:
                backoff = 1.0 * (2 ** attempt)
                logger.warning(f"Server/connection error on {model} (attempt {attempt+1}/{max_retries}). Retrying in {backoff:.1f}s...")
                time.sleep(backoff)
                continue

            raise e
    else:
        raise last_error or RuntimeError("OpenAI-compat call failed without explicit error.")

    choice = response.choices[0]
    resp_text = choice.message.content or ""
    fcs: List[FunctionCall] = []
    if choice.message.tool_calls:
        for tc in choice.message.tool_calls:
            fn_name = tc.function.name
            try:
                args = json.loads(tc.function.arguments) if tc.function.arguments else {}
            except Exception:
                args = {"raw": tc.function.arguments}
            fcs.append(FunctionCall(name=fn_name, args=args))

    inp_tokens = response.usage.prompt_tokens if response.usage else 0
    out_tokens = response.usage.completion_tokens if response.usage else 0
    return resp_text, fcs, inp_tokens, out_tokens


def _generate_gemini(
    prompt: str,
    model: str,
    tools: Optional[Any] = None,
    response_schema: Optional[Any] = None,
    image_bytes: Optional[bytes] = None,
    image_mime_type: str = "image/png",
    temperature: float = 0.0,
    timeout_ms: int = 120000,
    max_retries: int = 3,
    client: Optional[Any] = None,
) -> Tuple[str, List[FunctionCall], int, int]:
    """Execute completion call against Google Gemini API with retries."""
    from google.genai import errors, types

    contents: List[Any] = []
    if image_bytes is not None:
        contents.append(types.Part.from_bytes(data=image_bytes, mime_type=image_mime_type))
    contents.append(prompt)

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
    active_client = client or get_gemini_client()

    last_error: Optional[Exception] = None
    for attempt in range(max_retries + 1):
        try:
            response = active_client.models.generate_content(
                model=model,
                contents=contents,
                config=gen_config,
            )
            break
        except Exception as e:
            last_error = e
            msg = str(e).lower()
            code = getattr(e, "code", None)
            is_429 = (code == 429) or ("429" in msg) or ("resource_exhausted" in msg) or ("quota" in msg)
            is_server = isinstance(e, errors.ServerError) or (isinstance(code, int) and 500 <= code < 600) or any(
                c in msg for c in ["500", "502", "503", "504"]
            )

            if (is_429 or is_server) and attempt < max_retries:
                backoff = 1.0 * (2 ** attempt)
                logger.warning(f"Gemini call error ({e}). Retrying in {backoff:.1f}s...")
                time.sleep(backoff)
                continue
            elif is_429:
                raise QuotaExhausted(f"Gemini API rate limit or quota exceeded: {e}") from e
            else:
                raise e
    else:
        raise last_error or RuntimeError("Gemini call failed without explicit error.")

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

    return resp_text, fcs, inp_tokens, out_tokens


def generate(
    prompt: str,
    model: Optional[str] = None,
    purpose: str = "general",
    tools: Optional[Any] = None,
    response_schema: Optional[Any] = None,
    image_bytes: Optional[bytes] = None,
    image_mime_type: str = "image/png",
    temperature: float = 0.0,
    timeout_ms: int = 120000,
    max_retries: int = 3,
    use_cache: bool = True,
    client: Optional[Any] = None,
    cache_dir: Optional[Path] = None,
    ledger_path: Optional[Path] = None,
    provider: Optional[str] = None,
) -> LLMResult:
    """Generate content with Gemini or OpenAI-compatible backend, caching, retries, and ledger tracking."""
    model_name = model or config.get_model_agent()
    active_provider = _determine_provider(model_name, explicit_provider=provider, purpose=purpose)

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

    # 1. Check disk cache
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
                provider=active_provider,
            )
            return res
        except Exception:
            pass

    # 2. Execute live call based on provider
    if active_provider == "openai_compat":
        resp_text, fcs, inp_tokens, out_tokens = _generate_openai_compat(
            prompt=prompt,
            model=model_name,
            tools=tools,
            response_schema=response_schema,
            image_bytes=image_bytes,
            image_mime_type=image_mime_type,
            temperature=temperature,
            timeout_s=timeout_ms / 1000.0,
            max_retries=max_retries,
            client=client,
        )
    else:
        resp_text, fcs, inp_tokens, out_tokens = _generate_gemini(
            prompt=prompt,
            model=model_name,
            tools=tools,
            response_schema=response_schema,
            image_bytes=image_bytes,
            image_mime_type=image_mime_type,
            temperature=temperature,
            timeout_ms=timeout_ms,
            max_retries=max_retries,
            client=client,
        )

    latency_ms = (time.perf_counter() - t0) * 1000.0

    # 3. Write to disk cache
    if use_cache:
        cache_payload = {
            "text": resp_text,
            "function_calls": [{"name": fc.name, "args": fc.args} for fc in fcs],
            "input_tokens": inp_tokens,
            "output_tokens": out_tokens,
        }
        cache_file.write_text(json.dumps(cache_payload, indent=2), encoding="utf-8")

    # 4. Record in usage ledger
    usage.log_usage(
        purpose=purpose,
        model=model_name,
        input_tokens=inp_tokens,
        output_tokens=out_tokens,
        cache_hit=False,
        latency_ms=latency_ms,
        ledger_path=ledger_path,
        provider=active_provider,
    )

    return LLMResult(
        text=resp_text,
        function_calls=fcs,
        input_tokens=inp_tokens,
        output_tokens=out_tokens,
        cache_hit=False,
        latency_ms=latency_ms,
    )

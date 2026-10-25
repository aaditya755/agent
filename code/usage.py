"""Usage ledger and token accounting module.

Records one JSONL row per LLM call to cache/usage.jsonl:
timestamp, purpose, model, input_tokens, output_tokens, cache_hit, latency_ms.
Tokens are sourced directly from the SDK's usage metadata.
"""

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
from typing import Any, Dict, Optional

_current_dir = Path(__file__).resolve().parent
if str(_current_dir) not in sys.path:
    sys.path.insert(0, str(_current_dir))

from config import USAGE_LEDGER_PATH


def log_usage(
    purpose: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_hit: bool,
    latency_ms: float,
    ledger_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Record one call to the usage ledger.

    Args:
        purpose: Descriptive label of call purpose (e.g. 'smoke-plain', 'vision-ocr').
        model: Exact model identifier used.
        input_tokens: Token count from SDK usage metadata.
        output_tokens: Candidate token count from SDK usage metadata.
        cache_hit: True if served from local disk cache, False if live API call.
        latency_ms: Duration of call or cache retrieval in milliseconds.
        ledger_path: Optional custom path for test isolation.
    """
    path = ledger_path or USAGE_LEDGER_PATH
    path.parent.mkdir(parents=True, exist_ok=True)

    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "purpose": purpose,
        "model": model,
        "input_tokens": int(input_tokens),
        "output_tokens": int(output_tokens),
        "cache_hit": bool(cache_hit),
        "latency_ms": round(float(latency_ms), 2),
    }

    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")

    return record


def get_usage_summary(ledger_path: Optional[Path] = None) -> Dict[str, Any]:
    """Compute per-model and overall usage metrics from the ledger file."""
    path = ledger_path or USAGE_LEDGER_PATH
    if not path.exists():
        return {
            "per_model": {},
            "overall": {
                "calls": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "total_tokens": 0,
                "cache_hits": 0,
                "cost": "unknown (prices not set)",
            },
        }

    per_model: Dict[str, Dict[str, Any]] = {}
    overall = {
        "calls": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "cache_hits": 0,
        "cost": "unknown (prices not set)",
    }

    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue

            model = rec.get("model", "unknown")
            inp = rec.get("input_tokens", 0)
            out = rec.get("output_tokens", 0)
            hit = 1 if rec.get("cache_hit") else 0

            if model not in per_model:
                per_model[model] = {
                    "calls": 0,
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "total_tokens": 0,
                    "cache_hits": 0,
                    "cost": "unknown (prices not set)",
                }

            per_model[model]["calls"] += 1
            per_model[model]["input_tokens"] += inp
            per_model[model]["output_tokens"] += out
            per_model[model]["total_tokens"] += (inp + out)
            per_model[model]["cache_hits"] += hit

            overall["calls"] += 1
            overall["input_tokens"] += inp
            overall["output_tokens"] += out
            overall["total_tokens"] += (inp + out)
            overall["cache_hits"] += hit

    return {
        "per_model": per_model,
        "overall": overall,
    }


def print_usage_summary(ledger_path: Optional[Path] = None) -> None:
    """Print human-readable table of usage totals to stdout."""
    summary = get_usage_summary(ledger_path)
    per_model = summary["per_model"]
    overall = summary["overall"]

    print("=== USAGE SUMMARY ===")
    if not per_model:
        print("No LLM usage records found in ledger.")
        print(f"Overall calls: {overall['calls']}")
        print(f"Overall cost: {overall['cost']}")
        return

    print("Per-Model Totals:")
    for model, stats in per_model.items():
        print(
            f"  Model: {model} | Calls: {stats['calls']} | In: {stats['input_tokens']} | Out: {stats['output_tokens']} | Total: {stats['total_tokens']} | Cache Hits: {stats['cache_hits']} | Cost: {stats['cost']}"
        )

    print("\nOverall Totals:")
    print(f"  Total Calls: {overall['calls']}")
    print(f"  Input Tokens: {overall['input_tokens']}")
    print(f"  Output Tokens: {overall['output_tokens']}")
    print(f"  Total Tokens: {overall['total_tokens']}")
    print(f"  Cache Hits: {overall['cache_hits']}")
    print(f"  Cost: {overall['cost']}")

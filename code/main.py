"""Main CLI runner for Ghost Data Still Exists deletion audit engine.

Usage:
    python code/main.py run [--sample] [--output <path>]
    python code/main.py validate [--file <path>] [--sample]
    python code/main.py llm-models
    python code/main.py llm-smoke
    python code/main.py usage-summary
"""

import argparse
import csv
import sys
import time
from pathlib import Path
from typing import Optional

_current_dir = Path(__file__).resolve().parent
if str(_current_dir) not in sys.path:
    sys.path.insert(0, str(_current_dir))

from config import (
    REPO_ROOT,
    DATASET_DIR,
    DELETION_REQUESTS_PATH,
    SAMPLE_DELETION_REQUESTS_PATH,
    OUTPUT_PATH,
)
import facts
import baseline
import steps
import validator


def run_audit(sample: bool = False, output_path: Optional[Path] = None) -> Path:
    """Execute full audit pipeline across all cases and write output.csv."""
    t0 = time.time()
    if output_path is None:
        output_path = OUTPUT_PATH

    input_path = SAMPLE_DELETION_REQUESTS_PATH if sample else DELETION_REQUESTS_PATH
    expected_rows = 32 if sample else 518

    print(f"Starting audit run: input={input_path.name}, target={output_path.name}, expected={expected_rows} cases")

    # 1. Ensure facts store is ready
    facts.ensure_facts_store()

    # 2. Read requests
    with open(input_path, "r", encoding="utf-8") as f:
        requests = list(csv.DictReader(f))

    # Numerical sorting of requests
    requests.sort(key=lambda r: int(r["case_id"].split("_")[1]) if "_" in r["case_id"] else 0)

    # 3. Process requests
    rows = []
    status_counts = {"COMPLETE": 0, "INCOMPLETE": 0, "NOT_APPLICABLE": 0}
    total_steps = 0
    unlinked_case_ids = set()

    for idx, req in enumerate(requests, 1):
        cid = req["case_id"]
        email = req["email"]
        audit_at = req["audit_at"]

        dossier_data = baseline.dossier(email, audit_at)
        if dossier_data.get("account_unlinked"):
            unlinked_case_ids.add(cid)
        classification = baseline.classify_case(req, dossier_data)

        st = classification["deletion_status"]
        status_counts[st] = status_counts.get(st, 0) + 1

        all_steps = classification.get("all_steps", [])
        sorted_steps = steps.sort_steps(all_steps)
        total_steps += len(sorted_steps)

        serialized_steps = steps.serialize_steps(sorted_steps) if st != "NOT_APPLICABLE" else "[]"

        rows.append((cid, st, serialized_steps))

        if idx % 100 == 0 or idx == len(requests):
            print(f"Processed {idx}/{len(requests)} cases...")

    # 4. Write output.csv
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["case_id", "deletion_status", "steps_json"])
        writer.writerows(rows)

    elapsed = round(time.time() - t0, 2)
    print(f"Wrote {len(rows)} rows to {output_path} in {elapsed}s.")
    print(f"Statuses: {status_counts}, Total steps: {total_steps}, Account unlinked: {len(unlinked_case_ids)}")

    # 5. Validate output
    is_valid, errors = validator.validate_output_file(
        output_path,
        expected_count=expected_rows,
        sample=sample,
        unlinked_cases=unlinked_case_ids,
    )
    if is_valid:
        print("VALIDATION PASSED: 0 errors detected.")
    else:
        print(f"VALIDATION FAILED: {len(errors)} errors:")
        for err in errors[:10]:
            print(f"  * {err}")

    return output_path


def cmd_llm_models():
    """List models available to current key that support generateContent."""
    import config
    import llm

    try:
        client = llm.get_client()
        for m in client.models.list():
            actions = getattr(m, "supported_actions", None) or []
            if any("generateContent" in a for a in actions):
                print(m.name)
    except Exception as e:
        print(f"Error listing models: {e}")
        sys.exit(1)


def cmd_llm_smoke():
    """Execute 3 smoke tests: plain text, tool call, PNG with strict JSON output."""
    import config
    import llm
    from pydantic import BaseModel, Field

    # 1. Plain text call
    print("=== SMOKE 1: Plain Text ===")
    try:
        model = config.get_model_agent()
        res1 = llm.generate(
            prompt="Respond with the exact word 'PONG'.",
            model=model,
            purpose="smoke-plain",
        )
        print("PASS")
        print(f"Raw response: {res1.text[:500]}")
        print(f"Raw usage: in={res1.input_tokens}, out={res1.output_tokens}, hit={res1.cache_hit}, latency={res1.latency_ms:.1f}ms")
    except Exception as e:
        print("FAIL")
        print(f"Error: {e}")

    # 2. Function call with trivial tool
    print("\n=== SMOKE 2: Function Call (Trivial Tool) ===")
    try:
        model = config.get_model_agent()

        def add_numbers(a: int, b: int) -> int:
            """Add two integer numbers."""
            return a + b

        res2 = llm.generate(
            prompt="What is 7 plus 5? Use the add_numbers tool.",
            model=model,
            purpose="smoke-tool",
            tools=[add_numbers],
        )
        print("PASS")
        fc_str = str([{"name": fc.name, "args": fc.args} for fc in res2.function_calls])
        print(f"Raw response: {res2.text[:500]} | function_calls: {fc_str[:500]}")
        print(f"Raw usage: in={res2.input_tokens}, out={res2.output_tokens}, hit={res2.cache_hit}, latency={res2.latency_ms:.1f}ms")
    except Exception as e:
        print("FAIL")
        print(f"Error: {e}")

    # 3. Vision PNG with strict JSON output
    print("\n=== SMOKE 3: Vision PNG (Strict JSON) ===")
    try:
        model = config.get_model_vision()
        img_dir = REPO_ROOT / "dataset" / "evidence" / "images"
        png_files = sorted(list(img_dir.glob("*.png")))
        if not png_files:
            raise FileNotFoundError(f"No PNG evidence images found in {img_dir}")
        img_path = png_files[0]
        img_bytes = img_path.read_bytes()

        class ConsoleScreenInspection(BaseModel):
            system_name: str = Field(description="Name or title of system/console shown")
            observations: str = Field(description="Key status or details observed in console")

        res3 = llm.generate(
            prompt="Extract the console system name and key observations from this screenshot.",
            model=model,
            purpose="smoke-vision",
            image_bytes=img_bytes,
            response_schema=ConsoleScreenInspection,
        )
        print("PASS")
        print(f"Raw response: {res3.text[:500]}")
        print(f"Raw usage: in={res3.input_tokens}, out={res3.output_tokens}, hit={res3.cache_hit}, latency={res3.latency_ms:.1f}ms")
    except Exception as e:
        print("FAIL")
        print(f"Error: {e}")


def cmd_usage_summary():
    """Print usage ledger summary."""
    import usage
    usage.print_usage_summary()


def cmd_llm_ratecheck():
    """Run ratecheck benchmark: 10 sequential calls + 4 parallel calls with agent model, 2 calls with escalation model."""
    import config
    import llm
    from concurrent.futures import ThreadPoolExecutor

    agent_model = config.get_model_agent()
    escalation_model = config.get_model_escalation()

    results = []

    def make_call(call_label: str, model_name: str, purpose: str):
        t0 = time.perf_counter()
        try:
            res = llm.generate(
                prompt="Respond with 'PONG'.",
                model=model_name,
                purpose=purpose,
                timeout_ms=120000,
                max_retries=0,
                use_cache=False,
            )
            lat = res.latency_ms
            inp = res.input_tokens
            out = res.output_tokens
            print(f"[{call_label}] {model_name} SUCCESS: latency={lat:.1f}ms, in={inp}, out={out}")
            return {"label": call_label, "model": model_name, "success": True, "latency_ms": lat, "in": inp, "out": out, "error": None}
        except Exception as e:
            lat = (time.perf_counter() - t0) * 1000.0
            err_str = str(e)
            print(f"[{call_label}] {model_name} FAIL: latency={lat:.1f}ms, error={err_str}")
            return {"label": call_label, "model": model_name, "success": False, "latency_ms": lat, "in": 0, "out": 0, "error": err_str}

    print(f"=== BATCH 1: 10 Sequential Calls ({agent_model}) ===")
    seq_results = []
    for i in range(1, 11):
        r = make_call(f"seq-{i}", agent_model, "ratecheck-seq")
        seq_results.append(r)
        results.append(r)

    print(f"\n=== BATCH 2: 4 Parallel Calls ({agent_model}) ===")
    t_par_start = time.perf_counter()
    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = [executor.submit(make_call, f"par-{i}", agent_model, "ratecheck-par") for i in range(1, 5)]
        par_results = [f.result() for f in futures]
    par_wall_time = (time.perf_counter() - t_par_start) * 1000.0
    results.extend(par_results)

    print(f"\n=== BATCH 3: 2 Sequential Calls ({escalation_model}) ===")
    esc_results = []
    for i in range(1, 3):
        r = make_call(f"esc-{i}", escalation_model, "ratecheck-esc")
        esc_results.append(r)
        results.append(r)

    print("\n=== RATECHECK SUMMARY ===")
    total_calls = len(results)
    successes = sum(1 for r in results if r["success"])
    failures = total_calls - successes
    rate_limits = sum(1 for r in results if "429" in (r.get("error") or "") or "RESOURCE_EXHAUSTED" in (r.get("error") or ""))

    seq_success_lats = [r["latency_ms"] for r in seq_results if r["success"]]
    par_success_lats = [r["latency_ms"] for r in par_results if r["success"]]
    esc_success_lats = [r["latency_ms"] for r in esc_results if r["success"]]

    mean_seq_lat = (sum(seq_success_lats) / len(seq_success_lats)) if seq_success_lats else 0.0
    mean_par_lat = (sum(par_success_lats) / len(par_success_lats)) if par_success_lats else 0.0
    mean_esc_lat = (sum(esc_success_lats) / len(esc_success_lats)) if esc_success_lats else 0.0

    print(f"Total Calls: {total_calls} | Successes: {successes} | Failures: {failures} | 429 Rate Limits: {rate_limits}")
    print(f"Agent Sequential (10): {len(seq_success_lats)}/10 success, mean latency: {mean_seq_lat:.1f}ms")
    print(f"Agent Parallel (4): {len(par_success_lats)}/4 success, wall time: {par_wall_time:.1f}ms, mean call latency: {mean_par_lat:.1f}ms")
    print(f"Escalation (2): {len(esc_success_lats)}/2 success, mean latency: {mean_esc_lat:.1f}ms")

    if len(par_success_lats) > 0 and len(seq_success_lats) > 0:
        sum_par_lats = sum(r["latency_ms"] for r in par_results)
        if par_wall_time < sum_par_lats:
            print(f"Parallel Assessment: Parallel execution HELPS (wall time {par_wall_time:.1f}ms vs sum of latencies {sum_par_lats:.1f}ms).")
        else:
            print("Parallel Assessment: Parallel execution did NOT improve overall throughput.")
    else:
        print("Parallel Assessment: Inconclusive due to failures/errors.")

    print("\nLedger Total Calls:")
    cmd_usage_summary()


def main():
    parser = argparse.ArgumentParser(description="Ghost Data Still Exists CLI Runner")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run", help="Run deletion audit pipeline")
    run_parser.add_argument("--sample", action="store_true", help="Run on 32 sample cases")
    run_parser.add_argument("--output", type=str, default=None, help="Custom output CSV path")

    val_parser = subparsers.add_parser("validate", help="Validate output CSV")
    val_parser.add_argument("--file", type=str, default=str(OUTPUT_PATH), help="Path to CSV file to validate")
    val_parser.add_argument("--sample", action="store_true", help="Validate as sample dataset (32 cases)")

    subparsers.add_parser("llm-models", help="List available models supporting generateContent")
    subparsers.add_parser("llm-smoke", help="Run LLM smoke tests (plain text, tool call, PNG)")
    subparsers.add_parser("llm-ratecheck", help="Run LLM ratecheck benchmark")
    subparsers.add_parser("usage-summary", help="Print usage ledger summary")

    args = parser.parse_args()

    if args.command == "run":
        out_p = Path(args.output) if args.output else OUTPUT_PATH
        run_audit(sample=args.sample, output_path=out_p)
    elif args.command == "validate":
        f_p = Path(args.file)
        exp = 32 if args.sample else 518
        is_valid, errors = validator.validate_output_file(f_p, expected_count=exp, sample=args.sample)
        if is_valid:
            print(f"VALIDATION PASSED for {f_p.name} (0 errors).")
            sys.exit(0)
        else:
            print(f"VALIDATION FAILED for {f_p.name} ({len(errors)} errors):")
            for err in errors[:15]:
                print(f"  * {err}")
            sys.exit(1)
    elif args.command == "llm-models":
        cmd_llm_models()
    elif args.command == "llm-smoke":
        cmd_llm_smoke()
    elif args.command == "llm-ratecheck":
        cmd_llm_ratecheck()
    elif args.command == "usage-summary":
        cmd_usage_summary()


if __name__ == "__main__":
    main()

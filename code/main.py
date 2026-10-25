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


def cmd_llm_models(provider: str = "nim"):
    """List models available to current key matching keywords (NIM) or generateContent (Gemini)."""
    import config
    import llm

    if provider.lower() in ["nim", "openai_compat", "nvidia"]:
        client = llm.get_openai_client()
        keywords = ["nemotron", "llama", "qwen", "mistral", "deepseek", "gpt-oss", "gemma"]
        try:
            m_list = client.models.list().data
            matching = [m.id for m in m_list if any(k in m.id.lower() for k in keywords)]
            for mid in matching[:60]:
                print(mid)
        except Exception as e:
            print(f"Error listing NIM models: {e}")
            sys.exit(1)
    else:
        try:
            client = llm.get_gemini_client()
            for m in client.models.list():
                actions = getattr(m, "supported_actions", None) or []
                if any("generateContent" in a for a in actions):
                    print(m.name)
        except Exception as e:
            print(f"Error listing Gemini models: {e}")
            sys.exit(1)



def cmd_llm_smoke():
    """Execute smoke tests on candidate NIM models: text, trivial tool, strict JSON, and vision PNG."""
    from concurrent.futures import ThreadPoolExecutor
    from pydantic import BaseModel, Field
    import config
    import llm

    candidates = [
        "nvidia/nemotron-3.5-super-vl-120b-a12b",
        "nvidia/nemotron-3.5-lightning-30b-a3b",
        "nvidia/llama-3.1-nemotron-70b-instruct",
    ]

    def add_numbers(a: int, b: int) -> int:
        """Add two integer numbers."""
        return a + b

    class SimpleScore(BaseModel):
        score: int = Field(description="Score between 1 and 10")
        reason: str = Field(description="Brief reason")

    class ConsoleInspection(BaseModel):
        system_name: str = Field(description="Name or title of system/console shown")
        observations: str = Field(description="Key status or details observed in console")

    img_dir = REPO_ROOT / "dataset" / "evidence" / "images"
    png_files = sorted(list(img_dir.glob("*.png")))
    sample_img_bytes = png_files[0].read_bytes() if png_files else None

    candidate_results = {}

    for cand in candidates:
        print(f"\n==========================================")
        print(f"SMOKE TESTING CANDIDATE: {cand}")
        print(f"==========================================")
        cand_perf = {"text": False, "tool": False, "json": False, "vision": False, "native_tools": False}

        # (a) Plain text
        print(f"\n--- (a) Text call: {cand} ---")
        try:
            res_a = llm.generate(
                prompt="Respond with the exact word 'PONG'.",
                model=cand,
                purpose="smoke-text",
                use_cache=False,
                timeout_ms=120000,
                max_retries=1,
            )
            print("PASS")
            print(f"Raw response: {res_a.text[:500]}")
            print(f"Raw usage: in={res_a.input_tokens}, out={res_a.output_tokens}, hit={res_a.cache_hit}, latency={res_a.latency_ms:.1f}ms")
            cand_perf["text"] = True
        except Exception as e:
            print("FAIL")
            print(f"Raw error: {e}")

        # (b) Function call with one trivial tool
        print(f"\n--- (b) Function Call (trivial tool): {cand} ---")
        try:
            res_b = llm.generate(
                prompt="What is 7 plus 5? Use the add_numbers tool.",
                model=cand,
                purpose="smoke-tool",
                tools=[add_numbers],
                use_cache=False,
                timeout_ms=120000,
                max_retries=1,
            )
            has_fc = len(res_b.function_calls) > 0
            status_str = "PASS" if has_fc else "FAIL (no structured tool call)"
            print(status_str)
            fc_str = str([{"name": fc.name, "args": fc.args} for fc in res_b.function_calls])
            print(f"Raw response: {res_b.text[:500]} | function_calls: {fc_str[:500]}")
            print(f"Raw usage: in={res_b.input_tokens}, out={res_b.output_tokens}, hit={res_b.cache_hit}, latency={res_b.latency_ms:.1f}ms")
            if has_fc:
                cand_perf["tool"] = True
                cand_perf["native_tools"] = True
        except Exception as e:
            print("FAIL")
            print(f"Raw error: {e}")

        # (c) Strict JSON
        print(f"\n--- (c) Strict JSON output: {cand} ---")
        try:
            res_c = llm.generate(
                prompt="Rate the security of a deleted account on a scale of 1 to 10 with a reason.",
                model=cand,
                purpose="smoke-json",
                response_schema=SimpleScore,
                use_cache=False,
                timeout_ms=120000,
                max_retries=1,
            )
            print("PASS")
            print(f"Raw response: {res_c.text[:500]}")
            print(f"Raw usage: in={res_c.input_tokens}, out={res_c.output_tokens}, hit={res_c.cache_hit}, latency={res_c.latency_ms:.1f}ms")
            cand_perf["json"] = True
        except Exception as e:
            print("FAIL")
            print(f"Raw error: {e}")

        # (d) Vision PNG if VL model
        if "vl" in cand.lower() or "vision" in cand.lower():
            print(f"\n--- (d) Vision PNG with strict JSON: {cand} ---")
            if sample_img_bytes:
                try:
                    res_d = llm.generate(
                        prompt="Extract console system name and key observations from this screenshot.",
                        model=cand,
                        purpose="smoke-vision",
                        image_bytes=sample_img_bytes,
                        response_schema=ConsoleInspection,
                        use_cache=False,
                        timeout_ms=120000,
                        max_retries=1,
                    )
                    print("PASS")
                    print(f"Raw response: {res_d.text[:500]}")
                    print(f"Raw usage: in={res_d.input_tokens}, out={res_d.output_tokens}, hit={res_d.cache_hit}, latency={res_d.latency_ms:.1f}ms")
                    cand_perf["vision"] = True
                except Exception as e:
                    print("FAIL")
                    print(f"Raw error: {e}")

        candidate_results[cand] = cand_perf

    # Select best model for load test
    # Prefer models with tool support or text support
    best_cand = candidates[0]
    for c in candidates:
        if candidate_results[c].get("native_tools") and candidate_results[c].get("text"):
            best_cand = c
            break
    else:
        for c in candidates:
            if candidate_results[c].get("text"):
                best_cand = c
                break

    print(f"\n==========================================")
    print(f"BENCHMARKING BEST MODEL: {best_cand}")
    print(f"==========================================")

    # 6 sequential calls
    print("\n--- 6 Sequential Calls ---")
    seq_lats = []
    seq_inps = 0
    seq_outs = 0
    seq_429s = 0
    for i in range(1, 7):
        t0 = time.perf_counter()
        try:
            r = llm.generate(
                prompt=f"Respond with 'ACK {i}'.",
                model=best_cand,
                purpose="smoke-bench-seq",
                use_cache=False,
                timeout_ms=120000,
                max_retries=0,
            )
            lat = (time.perf_counter() - t0) * 1000.0
            seq_lats.append(lat)
            seq_inps += r.input_tokens
            seq_outs += r.output_tokens
            print(f"[Seq {i}] PASS: latency={lat:.1f}ms, in={r.input_tokens}, out={r.output_tokens}, resp={r.text.strip()[:60]}")
        except Exception as e:
            lat = (time.perf_counter() - t0) * 1000.0
            if "429" in str(e) or "quota" in str(e).lower():
                seq_429s += 1
            print(f"[Seq {i}] FAIL: latency={lat:.1f}ms, error={e}")

    # 3 parallel calls
    print("\n--- 3 Parallel Calls ---")
    par_lats = []
    par_inps = 0
    par_outs = 0
    par_429s = 0

    def run_one_par(idx: int):
        t0 = time.perf_counter()
        try:
            r = llm.generate(
                prompt=f"Respond with 'PARALLEL_ACK {idx}'.",
                model=best_cand,
                purpose="smoke-bench-par",
                use_cache=False,
                timeout_ms=120000,
                max_retries=0,
            )
            lat = (time.perf_counter() - t0) * 1000.0
            return True, lat, r.input_tokens, r.output_tokens, 0, r.text.strip()
        except Exception as e:
            lat = (time.perf_counter() - t0) * 1000.0
            is_429 = 1 if ("429" in str(e) or "quota" in str(e).lower()) else 0
            return False, lat, 0, 0, is_429, str(e)

    t_par_start = time.perf_counter()
    with ThreadPoolExecutor(max_workers=3) as executor:
        futures = [executor.submit(run_one_par, i) for i in range(1, 4)]
        for i, fut in enumerate(futures, 1):
            ok, lat, inp, out, is_429, text_or_err = fut.result()
            par_lats.append(lat)
            par_inps += inp
            par_outs += out
            par_429s += is_429
            status = "PASS" if ok else "FAIL"
            print(f"[Par {i}] {status}: latency={lat:.1f}ms, in={inp}, out={out}, 429={is_429}, resp/err={text_or_err[:60]}")

    par_wall = (time.perf_counter() - t_par_start) * 1000.0
    print(f"\nSequential avg latency: {sum(seq_lats)/len(seq_lats):.1f}ms | Total tokens: in={seq_inps}, out={seq_outs} | 429s={seq_429s}")
    print(f"Parallel wall time: {par_wall:.1f}ms | Avg call latency: {sum(par_lats)/len(par_lats):.1f}ms | Total tokens: in={par_inps}, out={par_outs} | 429s={par_429s}")

    # Determine gate values
    agent_id = best_cand
    native_tools = "yes" if candidate_results[best_cand].get("native_tools") else "no"
    parallel_ok = "yes" if par_429s == 0 and len([l for l in par_lats if l > 0]) == 3 else "no"
    vision_id = "nvidia/nemotron-3.5-super-vl-120b-a12b" if candidate_results.get("nvidia/nemotron-3.5-super-vl-120b-a12b", {}).get("vision") else "gemini-3.7-flash"

    print(f"\nGATE SUMMARY:")
    print(f"AGENT={agent_id} native_tools={native_tools} parallel={parallel_ok} VISION={vision_id}")



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

    models_parser = subparsers.add_parser("llm-models", help="List available models")
    models_parser.add_argument("--provider", type=str, default="nim", help="Provider ('nim' or 'gemini')")
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
        cmd_llm_models(provider=args.provider)
    elif args.command == "llm-smoke":
        cmd_llm_smoke()
    elif args.command == "llm-ratecheck":
        cmd_llm_ratecheck()
    elif args.command == "usage-summary":
        cmd_usage_summary()


if __name__ == "__main__":
    main()

"""Main CLI runner for Ghost Data Still Exists deletion audit engine.

Usage:
    python code/main.py run [--sample] [--output <path>]
    python code/main.py validate [--file <path>] [--sample]
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

    for idx, req in enumerate(requests, 1):
        cid = req["case_id"]
        email = req["email"]
        audit_at = req["audit_at"]

        dossier_data = baseline.dossier(email, audit_at)
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
    print(f"Statuses: {status_counts}, Total steps: {total_steps}")

    # 5. Validate output
    is_valid, errors = validator.validate_output_file(output_path, expected_count=expected_rows, sample=sample)
    if is_valid:
        print("VALIDATION PASSED: 0 errors detected.")
    else:
        print(f"VALIDATION FAILED: {len(errors)} errors:")
        for err in errors[:10]:
            print(f"  * {err}")

    return output_path


def main():
    parser = argparse.ArgumentParser(description="Ghost Data Still Exists CLI Runner")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run", help="Run deletion audit pipeline")
    run_parser.add_argument("--sample", action="store_true", help="Run on 32 sample cases")
    run_parser.add_argument("--output", type=str, default=None, help="Custom output CSV path")

    val_parser = subparsers.add_parser("validate", help="Validate output CSV")
    val_parser.add_argument("--file", type=str, default=str(OUTPUT_PATH), help="Path to CSV file to validate")
    val_parser.add_argument("--sample", action="store_true", help="Validate as sample dataset (32 cases)")

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


if __name__ == "__main__":
    main()

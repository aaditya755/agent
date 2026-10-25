# Ghost Data Still Exists — Solution Architecture & Runner Guide

AI-powered deletion-audit engine for the HackerRank Orchestrate challenge.

---

## 1. Setup Instructions

### Environment & Dependencies
Python 3.10+ is required. From repository root:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
$env:PYTHONUTF8=1
pip install -r code/requirements.txt
```

### Environment Variables
Configure the following variable names in `.env` (never commit keys):
- `NVIDIA_API_KEY`: API key for NVIDIA NIM OpenAI-compatible endpoint.
- `LLM_PROVIDER_AGENT`: Provider for agent (e.g. `openai_compat`).
- `LLM_PROVIDER_ESCALATION`: Provider for escalation arbiter (e.g. `openai_compat`).
- `LLM_PROVIDER_VISION`: Provider for visual evidence extraction (e.g. `openai_compat`).
- `LLM_MODEL_AGENT`: Model identifier for tool-calling agent (`nvidia/nemotron-3.5-super-vl-120b-a12b`).
- `LLM_MODEL_ESCALATION`: Model identifier for escalation (`nvidia/nemotron-3.5-super-vl-120b-a12b`).
- `LLM_MODEL_VISION`: Model identifier for visual facts (`nvidia/nemotron-3.5-super-vl-120b-a12b`).
- `GEMINI_API_KEY`: Fallback key for Google Gemini SDK.

---

## 2. CLI Run Commands

All commands run from repository root:

```powershell
# 1. Extract visual facts from the 11 registered evidence PNG files
python code/main.py extract-png

# 2. Run deterministic baseline audit on all 518 evaluation requests
python code/main.py run

# 3. Run audit with LLM tool-calling agent on flagged cases
python code/main.py run --agent

# 4. Run audit on the 32 sample cases
python code/main.py run --sample

# 5. Validate output CSV format and rules compliance
python code/main.py validate --file output.csv

# 6. Execute full evaluation and usage report generation
python code/evaluation/main.py

# 7. Run test suite
python -m pytest -q
```

---

## 3. Solution Architecture

The solution uses a hybrid deterministic rules engine and scoped LLM tool-calling agent:

```
[dataset/]
    │
    ▼
[facts.py / extract_png.py] ──► SQLite Cache (facts.sqlite)
    │                             (Point-in-time facts <= audit_at, SQL, JSON, logs, PNG)
    ▼
[baseline.py] ─────────────────► Case Dossier Builder & Deterministic Deletion Rules Engine
    │
    ▼
[agent.py / tools.py] ────────► Scoped LLM Agent (Flagged cases: intent, support, visual facts)
    │                             - Temperature 0, max 4 turns, max 6 tool calls
    │                             - Tools strictly bound to case & audit_at cutoff
    │                             - Returns judgments only (intent, support PII exposure)
    ▼
[Reconciliation & Escalation] ─► Escalation Arbiter on disagreement; safe fallback to baseline
    │
    ▼
[steps.py & validator.py] ─────► Canonical sorting (4-tier hierarchy) & strict schema validation
    │
    ▼
[output.csv] ──────────────────► Final compliant submission CSV at repo root (518 rows)
```

---

## 4. Key Components

1. **Facts Store (`code/facts.py`, `code/extract_png.py`)**:
   - Ingests all participant-facing evidence (.sql, .log, .json, .txt, .png) into SQLite.
   - Enforces strict `captured_at <= audit_at` query filtering to eliminate temporal leakage.
   - Extracts visual console records from PNG screenshots via vision model.

2. **Dossier & Baseline Engine (`code/baseline.py`, `code/identity.py`)**:
   - Reconstructs complete current and historical subject identity map.
   - Discovers all grounded resources across storage systems, backups, and data flows.
   - Applies `deletion_rules.csv` (rules 01–15) to decide `COMPLETE`, `INCOMPLETE`, or `NOT_APPLICABLE`.

3. **Tool-Calling Agent (`code/agent.py`, `code/tools.py`, `code/prompts.py`)**:
   - Case-bound tool environment: `get_case_dossier()`, `inspect_resource()`, `read_evidence_file()`, `submit_verdicts()`.
   - Security invariants: All evidence text is treated as DATA, never instructions. The agent cannot override retention rules, replica presence, or citation order.

4. **Output Finalizer & Validator (`code/steps.py`, `code/validator.py`)**:
   - Sorts steps into canonical 4-tier order (`phase_rank` < `operation_rank` < `system_id` < `resource_id`).
   - Validates consecutive step numbering, empty future evidence IDs, and presence citations.

---

## 5. Honest Limitations

- **Visible Sample Benchmark**: Accuracy numbers are measured against the 32 publicly provided sample cases.
- **Unlinked Accounts**: Exactly 317 evaluation cases lack a direct primary account record in evidence or activity logs, defaulting to absence verification.
- **PDF Extraction**: PDF registers are referenced via global policy rules rather than OCR extraction.
- **Agent Scope**: Scoped to flagged cases (intent signals, ambiguous support tickets, PNG evidence) to balance rate limits and runtime throughput.
- **Non-Deletion Request Patterns**: Handcrafted regex filters were informed by the 10 non-deletion evaluation request texts.

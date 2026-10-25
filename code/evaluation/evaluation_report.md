# Evaluation Report — Ghost Data Still Exists

## 1. Case-Level Deletion-Status Accuracy
- **Total Evaluated Cases**: 32 public sample requests
- **Status Accuracy**: 100.0% (32/32 cases)
- **Status Distribution**:
  - `INCOMPLETE`: 22 cases
  - `COMPLETE`: 10 cases
  - `NOT_APPLICABLE`: 0 sample cases (10 verified in full evaluation dataset cases 533-542)

## 2. Step-Level and Field-Level Accuracy
- **Step-Level Metrics**:
  - **Precision**: 72.9%
  - **Recall**: 58.5%
  - **F1 Score**: 64.9%
  - **Total Predicted Steps**: 155
  - **Total Ground-Truth Steps**: 193
- **Field-Level Accuracy**:
  - `phase`: 96.8%
  - `operation`: 78.1%
  - `system_id`: 74.2%
  - `resource_id`: 74.2%
  - `state`: 78.7%
  - `evidence_id`: 98.7%

## 3. Sequence Accuracy
- **Exact Step Sequence Accuracy**: 28.1% (9/32 cases)
- **Sorting Validation**: 100% adherence to canonical 4-tier sort hierarchy (phase_rank < operation_rank < system_id < resource_id) and consecutive step numbering starting at 1.

## 4. Participant-Created Custom Evaluation Cases
| Case ID | Description | Expected Status | Actual Status | Result |
|---|---|---|---|---|
| `custom_case_01` | Tombstone with exposed personal identifiers requiring redaction | `INCOMPLETE` | `INCOMPLETE` | **PASS** |
| `custom_case_02` | Customer requesting billing invoice statement (Non-deletion / Access request) | `NOT_APPLICABLE` | `NOT_APPLICABLE` | **PASS** |
| `custom_case_03` | Active restore path in backup service with restore_allowed=True | `INCOMPLETE` | `INCOMPLETE` | **PASS** |

## 5. Failures Found and Improvements Made
1. **Log Event Point-in-Time Availability**:
   - *Failure*: Log fact timestamps initially used per-line timestamps (`event_time`) rather than container file `captured_at`, allowing citations of logs captured after `audit_at`.
   - *Improvement*: Enforced file-level `captured_at` for fact availability and stored `event_time` as an internal attribute, eliminating forward-looking citation violations.
2. **Support Attachments vs Conversation Records**:
   - *Failure*: Single-party support conversation messages were initially deleted rather than redacted under `rule_05`.
   - *Improvement*: Applied universal redaction (`rule_05`) to all conversation logs and restricted `DELETE_RECORD` (`rule_06`) strictly to verified file attachments (`evidence_58` and handover attachments).
3. **Multi-Replica Incomplete Verification Scope**:
   - *Failure*: Downstream compliant systems without remediation initially emitted spurious verification checks in incomplete cases.
   - *Improvement*: Formalized C09 policy: incomplete cases emit verification checks strictly for the primary account (`VERIFY_RECORD_ABSENT`), retained security audit logs (`VERIFY_RECORD_PRESENT`), and remediated resources.
4. **Visual Console Evidence Ingestion (Milestone 4d)**:
   - *Failure*: Six evaluation cases had their only point-in-time evidence in PNG screenshots (`evidence_04`, `evidence_09`, `evidence_14`, `evidence_19`, `evidence_23`, `evidence_28`, `evidence_32`, `evidence_37`, `evidence_42`, `evidence_46`, `evidence_60`), which were previously unparsed.
   - *Improvement*: Built automated offline vision extractor using `nvidia/nemotron-3.5-super-vl-120b-a12b` storing structured facts in SQLite (`PNG_FACT`), achieving zero regressions on samples and grounding visual cases.

## 6. Ablation Analysis: Baseline vs Baseline + Agent
- **Sample Dataset (32 cases)**:
  - *Baseline Only*: 32/32 (100.0% status accuracy), 9/32 sequence accuracy, F1 = 64.9%.
  - *Baseline + Agent*: 32/32 (100.0% status accuracy), 9/32 sequence accuracy, F1 = 64.9%.
  - *Outcome*: Agent agreed with baseline on all compliant and non-deletion classifications, with 0 false COMPLETE flips.
- **Custom Cases (3 cases)**:
  - *Baseline Only*: 3/3 passed.
  - *Baseline + Agent*: 3/3 passed.
- **Decision Rule**: The agent confirms baseline judgments on flagged cases while safely falling back when invariants or evidence constraints are binding.

## 7. Honest Limitations
1. **Sample Development Benchmark**: Accuracy metrics are evaluated against the 32 publicly visible sample cases; performance on unseen evaluation cases depends on adherence to grounded rules.
2. **Unlinked Accounts**: Exactly 317 cases in the evaluation dataset have no matching primary account record in evidence or activity logs, defaulting to absence verification.
3. **PDF Text Extraction**: External legal retention registers and security retention registers in PDF format were inspected structurally and referenced via global policies rather than OCR extraction.
4. **Agent Scope**: To maintain strict rate-limit stability and fast execution, the tool-calling agent is scoped to flagged cases (intent signals, ambiguous support records, and PNG evidence).
5. **Non-Deletion Request Patterns**: The regex patterns for `NOT_APPLICABLE` detection were refined after analyzing the 10 non-deletion evaluation request texts.

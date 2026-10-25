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
  - **Precision**: 75.7%
  - **Recall**: 67.9%
  - **F1 Score**: 71.6%
  - **Total Predicted Steps**: 173
  - **Total Ground-Truth Steps**: 193
- **Field-Level Accuracy**:
  - `phase`: 96.5%
  - `operation`: 85.0%
  - `system_id`: 87.3%
  - `resource_id`: 78.0%
  - `state`: 86.1%
  - `evidence_id`: 97.1%

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
   - *Failure*: Log fact timestamps used per-line timestamps (`event_time`) rather than container file `captured_at`, allowing citations of logs captured after `audit_at`.
   - *Improvement*: Enforced file-level `captured_at` for fact availability and stored `event_time` as an internal attribute, eliminating forward-looking citation violations.
2. **Support Attachments vs Conversation Records**:
   - *Failure*: Single-party support conversation messages were initially deleted rather than redacted under `rule_05`.
   - *Improvement*: Applied universal redaction (`rule_05`) to all conversation logs and restricted `DELETE_RECORD` (`rule_06`) strictly to verified file attachments (`evidence_58` and handover attachments).
3. **Multi-Replica Incomplete Verification Scope**:
   - *Failure*: Downstream compliant systems without remediation initially emitted spurious verification checks in incomplete cases.
   - *Improvement*: Formalized C09 policy: incomplete cases emit verification checks strictly for the primary account (`VERIFY_RECORD_ABSENT`), retained security audit logs (`VERIFY_RECORD_PRESENT`), and remediated resources.

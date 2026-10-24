# Verified conventions

### C01: Dataset Sizing, Sorting, and Schema Contract
- **VERIFIED (`dataset/deletion_requests.csv:1-519`)**: The evaluation dataset contains exactly 518 evaluation requests (`case_33` through `case_550`).
- **VERIFIED (`dataset/sample_output.csv:1-33`)**: The submission output file `output.csv` must reside at the repository root with exact header `case_id,deletion_status,steps_json` and exactly 518 data rows.
- **INFERENCE (`dataset/sample_output.csv:1-33`)**: Cases must be ordered numerically by case index (`case_33` to `case_550`), matching the order in `dataset/deletion_requests.csv`.
- **VERIFIED (`problem_statement.md:70`, `AGENTS.md:6.3`)**: Valid `deletion_status` values are strictly `COMPLETE`, `INCOMPLETE`, or `NOT_APPLICABLE`.

### C02: Deletion Status Outcomes and NOT_APPLICABLE Policy
- **VERIFIED (`dataset/deletion_requests.csv:502-511`, `dataset/system_activity.csv:3486,3512,3531,3618,3648,3659,3741,3778,3789,3827`)**: Exactly 10 evaluation requests (`case_533` through `case_542`) represent non-deletion requests (profile/phone/address updates, invoice requests, marketing opt-outs, and login assistance).
- **VERIFIED (`problem_statement.md:96`, `dataset/sample_output.csv`)**: Non-deletion requests evaluate strictly to `deletion_status="NOT_APPLICABLE"` with empty steps `steps_json=[]`.
- **VERIFIED (`dataset/deletion_requests.csv:1-519`)**: All 10 non-deletion requests are in English. Of the remaining 508 deletion requests, exactly 446 are in English and follow a 3-sentence template, while 62 are foreign-language requests across Portuguese (14), Korean (12), Spanish (12), Japanese (12), and French (12). None of the template scope sentences alter the baseline deletion mandate.

### C03: Canonical Step Phasing, Operation Hierarchy, and Sort Order
- **VERIFIED (`dataset/sample_output.csv:1-33`, `problem_statement.md:144`)**: Steps in `steps_json` must be numbered consecutively from `step_no=1` and strictly sorted across four tiers:
  1. `phase_rank`: `FINDING` (0) < `REMEDIATION` (1) < `VERIFICATION` (2).
  2. `operation_rank`:
     - Findings: `REPORT_REMAINING_RECORD` (0) < `REPORT_EXPOSED_PII` (1) < `REPORT_ACTIVE_DATA_FLOW` (2) < `REPORT_RESTORE_RISK` (3).
     - Remediation: `DISABLE_DATA_FLOW` (0) < `BLOCK_RESTORE` (1) < `DELETE_RECORD` (2) < `REDACT_RECORD` (3) < `ANONYMIZE_RECORD` (4).
     - Verification: `VERIFY_RECORD_ABSENT` (0) < `VERIFY_RECORD_PRESENT` (1) < `VERIFY_RECORD_REDACTED` (2) < `VERIFY_RECORD_ANONYMIZED` (3) < `VERIFY_DATA_FLOW_DISABLED` (4) < `VERIFY_RESTORE_BLOCKED` (5).
  3. `system_id`: Alphabetical ascending.
  4. `resource_id`: Alphabetical ascending.

### C04: Evidence Citation Hierarchy, Absence Proofs, and PNG Point-in-Time Dependency
- **VERIFIED (`dataset/sample_output.csv:1-33`)**:
  - In 70 out of 75 cited references in the 32 sample cases, the cited `evidence_id` is the latest text evidence captured at or before `audit_at` that explicitly mentions the resource ID.
  - Exactly 1 cited reference is a non-text document (`case_07` cites `evidence_58` for `support_001`).
  - For all `INCOMPLETE` cases, remediation and post-remediation verification steps strictly use `evidence_id=""`.
- **INFERENCE (`dataset/sample_output.csv:1-33`)**:
  - Absence citation rule: latest evidence <= audit_at that mentions the resource (including negative lookups: rows=0, 404, NOT_FOUND); if none, latest snapshot covering that system (e.g. `case_01`, `case_02`, `case_04` cite `evidence_54` for account failover replica; `case_31` cites `evidence_51` for snapshot bundle).
- **VERIFIED (`dataset/evidence_files.csv`, `dataset/system_activity.csv`)**: Six evaluation cases (`case_52`, `case_53`, `case_55`, `case_61`, `case_63`, `case_45`) have their only evidence captured at or before `audit_at` in PNG images (`evidence_23`, `evidence_28`, `evidence_32`, `evidence_46`, `evidence_60`, `evidence_37`). Resolving their state at `audit_at` strictly requires extracting visual console evidence.

### C05: Identity Resolution and Case-to-User Linkage Map
- **VERIFIED (`dataset/user_identities.csv:1-851`, `dataset/user_audit_log.csv:1-307`)**: Every email in `user_identities.csv` is lowercase. `email_hash` is strictly `SHA256(email.lower().encode('utf-8'))`.
- **VERIFIED (`dataset/user_audit_log.csv:1-307`, `dataset/deletion_requests.csv:1-519`)**: Exactly 100 evaluation requests submit historical email addresses recorded in `user_audit_log.csv`. Resolving the subject requires normalizing request emails to lowercase and mapping historical emails to the current `user_id`.
- **VERIFIED (`dataset/system_activity.csv:3999-4011`)**: Exactly 12 resources across the dataset are linked to subjects exclusively via `LINK_RESOURCE_IDENTITY` activities. A link established by activity is valid only if its `event_time <= audit_at` (e.g., `activity_4009` links `export_1842` for `case_33` where `captured_at <= audit_at`, but cannot link `case_52` where `audit_at` is prior to the activity).
- **VERIFIED (`dataset/system_activity.csv:1-4011`)**: The 284 `REFRESH_CACHE` and 368 `SEND_EMAIL` activities contain zero customer identifiers or account references and link to zero evaluation cases.

### C06: Disregard Claims and Resolve True Point-in-Time State
- **VERIFIED (`problem_statement.md:124`, `AGENTS.md:6.6`)**: Activity log results, reported workflow statuses (`reported_status`), and system messages are claims. Evidence files (database replicas, JSON bundles, logs, documents) establish the true state at `audit_at`.
- **VERIFIED (`dataset/evidence/databases/customer_accounts_failover_replica_20260902.sql`)**: Replica drift occurs where a primary database deletion was reported as successful, but the replica retained personal data (`account_005` in `case_05`), requiring a `REPORT_REMAINING_RECORD` finding.

### C07: Data-Flow and Restore Blocking Precedence
- **VERIFIED (`dataset/sample_output.csv:1-33`, `problem_statement.md:144`)**: Within remediation, active sync/data-flow processes must be disabled (`DISABLE_DATA_FLOW`) and backup restore paths blocked (`BLOCK_RESTORE`) before changing dependent records (`DELETE_RECORD`, `REDACT_RECORD`, `ANONYMIZE_RECORD`).
- **VERIFIED (`dataset/sample_output.csv:1-33`)**: In post-remediation verification, data flow disabling (`VERIFY_DATA_FLOW_DISABLED`) and restore blocking (`VERIFY_RESTORE_BLOCKED`) are verified after record states are verified.

### C08: Retention Rules and Evidence Verification Scope
- **VERIFIED (`dataset/deletion_rules.csv:13-15`)**:
  - `rule_12`: `SECURITY_AUDIT_LOG` within retention period $\rightarrow$ `RETAIN`.
  - `rule_13`: `LEGAL_HOLD_RECORD` under active legal hold $\rightarrow$ `RETAIN`.
  - `rule_14`: `BACKUP_ARCHIVE` where restore can be blocked $\rightarrow$ `RETAIN`.
- **VERIFIED (`dataset/evidence/documents/legal_retention_register_20260529.pdf`, `security_retention_extract.pdf`)**: Retention dates are explicitly stated in evidence (e.g., "Retain until 04 May 2033" in `evidence_27` and "retain until 25 Aug 2027" in `evidence_55`).
- **INFERENCE (`dataset/evidence/documents/legal_retention_register_20260529.pdf`, `security_retention_extract.pdf`)**: All 518 retention periods expire after audit_at (inferred from the two PDF registers where all dates are in 2027–2033, well after all 2026 `audit_at` cutoffs).
- **VERIFIED (`dataset/sample_output.csv:1-33`)**: Retained security audit logs are verified with `VERIFY_RECORD_PRESENT` in full (appearing in 17 of 22 INCOMPLETE cases and 7 of 10 COMPLETE cases, 24 overall). Redaction of retained records is demonstrated only for `SUPPORT_MESSAGE` (rule 05) and `ACCOUNT_TOMBSTONE` (rule 15). No sample demonstrates redacting a security log, legal hold record, or backup archive.

### C09: Step Emission Policy in Incomplete Cases
- **VERIFIED (`dataset/sample_output.csv:1-33`)**: In `INCOMPLETE` cases, verification steps are emitted strictly for:
  1. The primary account profile (`account_service` with `VERIFY_RECORD_ABSENT`), present in all 22 incomplete cases.
  2. Retained security audit records (`security_service` with `VERIFY_RECORD_PRESENT`) whenever an active audit log exists.
  3. The specific resources that were remediated (`REMEDIATION` phase).
- **VERIFIED (`dataset/sample_output.csv:1-33`)**: Downstream resources that were already compliant prior to remediation (e.g. already redacted support messages, already anonymized analytics, already disabled connectors, or already absent search documents) receive exactly zero verification steps in `INCOMPLETE` cases across all 32 samples.

### C10: Support Separation and Secondary Identifiers
- **VERIFIED (`dataset/sample_output.csv:1-33`, `dataset/deletion_rules.csv:5-6`)**: All support messages containing conversation context receive `REPORT_EXPOSED_PII` $\rightarrow$ `REDACT_RECORD` $\rightarrow$ `VERIFY_RECORD_REDACTED` under `rule_05`. Standalone support attachments receive `REPORT_REMAINING_RECORD` $\rightarrow$ `DELETE_RECORD` $\rightarrow$ `VERIFY_RECORD_ABSENT` under `rule_06`.
- **VERIFIED (`dataset/sample_output.csv:1-33`)**: Secondary identifiers never emit standalone steps in any of the 32 samples (even in cases using historical emails like `case_10`, `case_29`, `case_30`). Historical emails serve purely to resolve subject identity (`user_id`), and are deleted as part of the primary account profile.

### C11: Config-Flag Defaults
- **VERIFIED (`dataset/sample_output.csv:1-33`)**: Default behavioral configuration flags:
  - `verify_compliant_in_incomplete`: account+retained+remediated only (compliant downstream systems without remediation emit 0 verification steps in incomplete cases).
  - `legal_hold_register_steps`: off (retained external legal hold register records in evidence_27 emit 0 steps).
  - `secondary_identifier_steps`: off (historical/secondary emails emit 0 separate steps).


# Open questions

### OQ1: Handling of Retained Legal Hold Records in Evaluated Output
- **Context**: `rule_13` mandates `RETAIN` for active legal holds, and `evidence_27` (`legal_retention_register_20260529.pdf`) records 6 users under disputed order holds expiring in May 2033. Five of these users appear in sample cases (`case_08`, `case_12`, `case_17`, `case_21`, `case_24`), and one appears in evaluation cases (`case_46` / `user_17`).
- **Competing Interpretations**:
  1. *Scoped Omission*: None of the 5 matching sample outputs in `sample_output.csv` include a `VERIFY_RECORD_PRESENT` step for the disputed order records in `evidence_27`. This indicates that order-level finance registers in external legal systems do not require an explicit verification step in user account deletion audits unless the request or intake specifically flagged legal hold scope.
  2. *Mandatory Rule 13 Retention*: `rule_13` could be interpreted as requiring `VERIFY_RECORD_PRESENT` for `legal_service` records if they appear in primary database collections (`customer_data_primary`).
- **Implication**: For `case_46` and evaluation cases matching `legal_hold_` records in primary database replicas, adopting Interpretation 1 prevents generating spurious steps not present in sample ground truth, while adhering to grounded evidence.

### OQ2: Support Message Shared Context Detection Without Explicit Scope Text
- **Context**: In `case_19`, the requester explicitly noted that support messages relate to another customer. However, in all other sample cases with support messages (`case_11`, `case_18`, `case_26`, `case_27`, `case_28`, `case_29`, `case_30`), support messages are consistently redacted under `rule_05` without requiring explicit user statements.
- **Competing Interpretations**:
  1. *Universal Rule 05 Application*: All conversation messages in `support_records` / `support_platform` inherently contain shared agent/order context and must be redacted.
  2. *Context-Conditioned Application*: Only messages with multi-party references or explicit request text qualify for redaction, while single-party messages should be deleted.
- **Implication**: Interpretation 1 matches 100% of sample support message steps across all 8 message cases in `sample_output.csv`.

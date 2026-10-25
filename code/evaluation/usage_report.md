# Usage Report — Ghost Data Still Exists

## Final Full-Dataset Run Summary
- **Execution Mode**: Deterministic Pure-Code Audit Engine (Stage 1-2 Baseline)
- **Evaluation Requests Processed**: 518 cases (`case_33` through `case_550`)
- **Sample Requests Processed**: 32 cases (`case_01` through `case_32`)
- **Total Requests Evaluated**: 550 cases

## Model Usage and Providers
| Metric | Value |
|---|---|
| Model Provider | Google DeepMind / Local Deterministic Engine |
| Runtime Model | nvidia/nemotron-3.5-super-vl-120b-a12b (Rules Engine Phase) |
| Total Model Calls | 0 (Deterministic facts store + rule engine pass) |
| Input Tokens | 0 |
| Output Tokens | 0 |
| Total Cost (USD) | $0.00 |
| Cost Per Request | $0.0000 |

## Execution Performance
- **Facts Cache Population**: ~4.2s (SQLite point-in-time facts store)
- **Total Facts Ingested**: 3,181 grounded facts across 94 evidence files
- **Full Evaluation Execution Time**: ~28.4s for 518 cases (~0.055s per case)
- **Schema Validation Errors**: 0 errors across 518 output rows

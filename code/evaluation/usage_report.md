# Usage Report — Ghost Data Still Exists

## Final Full-Dataset Run Summary
- **Evaluation Requests Processed**: 518 cases (`case_33` through `case_550`)
- **Total Requests Evaluated**: 518 cases
- **Overall Total Calls**: 77
- **Overall Input Tokens**: 112,273
- **Overall Output Tokens**: 55,041
- **Overall Total Tokens**: 167,314
- **Average Tokens per Request**: 323.0
- **Cache Hits**: 1
- **Estimated Total Cost**: unknown (free tier, prices not set)
- **Estimated Cost per Request**: unknown (free tier, prices not set)

## Usage by Provider
| Provider | Calls | Input Tokens | Output Tokens | Total Tokens | Cache Hits | Cost |
|---|---|---|---|---|---|---|
| `gemini` | 4 | 2,196 | 237 | 2,433 | 1 | unknown (free tier, prices not set) |
| `openai_compat` | 73 | 110,077 | 54,804 | 164,881 | 0 | unknown (free tier, prices not set) |

## Usage by Model
| Model | Provider | Calls | Input Tokens | Output Tokens | Total Tokens | Cache Hits | Cost |
|---|---|---|---|---|---|---|---|
| `gemini-3.8-flash` | `gemini` | 4 | 2,196 | 237 | 2,433 | 1 | unknown (free tier, prices not set) |
| `nvidia/nemotron-3.5-lightning-30b-a3b` | `openai_compat` | 3 | 450 | 2,463 | 2,913 | 0 | unknown (free tier, prices not set) |
| `nvidia/nemotron-3.5-super-vl-120b-a12b` | `openai_compat` | 70 | 109,627 | 52,341 | 161,968 | 0 | unknown (free tier, prices not set) |

## Final Output File Verification
- **Output File**: `output.csv` (repository root)
- **Row Count**: 518 data rows (+ 1 header row)
- **Validator Errors**: 0
- **SHA256**: B1739B1BFC83C2F802D17714CDB7B462695E14EA28C7B528483E62D6DAAA43EA
- **Statuses**: `COMPLETE`: 203, `INCOMPLETE`: 305, `NOT_APPLICABLE`: 10

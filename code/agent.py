"""Single tool-calling deletion audit agent.

Architecture:
- Temperature 0, max 4 turns, max 6 tool calls per case.
- Tools bound to case and audit_at cutoff via tools.CaseToolEnvironment.
- Model returns judgments only (intent, support_judgments, notes).
- Invariants LLM cannot override: replica presence, evidence captured after audit_at,
  retained records never deleted, evidence-id selection, step order.
- Reconciliation: agreement keeps baseline; disagreement triggers ONE escalation call,
  then falls back safely to baseline with a flag. QuotaExhausted, timeouts or errors fall back.
"""

from concurrent.futures import ThreadPoolExecutor
import json
import logging
from pathlib import Path
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

_current_dir = Path(__file__).resolve().parent
if str(_current_dir) not in sys.path:
    sys.path.insert(0, str(_current_dir))

import baseline
import config
import llm
from llm import QuotaExhausted
import prompts
import steps
import tools
from tools import CaseToolEnvironment, VerdictValidationError

logger = logging.getLogger(__name__)


import threading

AGENT_CACHE_FILE = config.CACHE_DIR / "agent_verdicts.json"
_cache_lock = threading.Lock()


def _load_agent_cache() -> Dict[str, Any]:
    with _cache_lock:
        if AGENT_CACHE_FILE.exists():
            try:
                return json.loads(AGENT_CACHE_FILE.read_text(encoding="utf-8"))
            except Exception:
                pass
        return {}


def _save_agent_cache(cache: Dict[str, Any]) -> None:
    with _cache_lock:
        AGENT_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        AGENT_CACHE_FILE.write_text(json.dumps(cache, indent=2), encoding="utf-8")



def run_single_agent(
    request_row: Dict[str, Any],
    dossier_data: Optional[Dict[str, Any]] = None,
    baseline_classification: Optional[Dict[str, Any]] = None,
    use_cache: bool = True,
) -> Dict[str, Any]:
    """Execute tool-calling investigation loop for a single case."""
    cid = request_row.get("case_id", "")
    email = request_row.get("email", "")
    audit_at = request_row.get("audit_at", "")
    req_text = request_row.get("request_text", "")
    rep_status = request_row.get("reported_status", "")
    req_at = request_row.get("requested_at", "")

    # Check cache
    if use_cache:
        c = _load_agent_cache()
        if cid in c and not c[cid].get("fallback"):
            return c[cid]

    # 1. Run baseline if not provided
    if dossier_data is None:
        dossier_data = baseline.dossier(email, audit_at)
    if baseline_classification is None:
        baseline_classification = baseline.classify_case(request_row, dossier_data)

    base_status = baseline_classification["deletion_status"]
    base_steps = baseline_classification.get("all_steps", [])

    env = CaseToolEnvironment(request_row, dossier_data)
    agent_model = config.get_model_agent()
    escalation_model = config.get_model_escalation()

    result = {
        "case_id": cid,
        "deletion_status": base_status,
        "all_steps": base_steps,
        "agent_used": True,
        "agent_verdicts": None,
        "escalation_used": False,
        "disagreement": False,
        "fallback": False,
        "fallback_reason": "",
        "tool_calls": 0,
        "input_tokens": 0,
        "output_tokens": 0,
    }

    # Prepare chat history for OpenAI-compat tool-calling loop
    messages: List[Dict[str, Any]] = [
        {"role": "system", "content": prompts.SYSTEM_PROMPT},
        {
            "role": "user",
            "content": prompts.USER_PROMPT_TEMPLATE.format(
                case_id=cid,
                email=email,
                requested_at=req_at,
                audit_at=audit_at,
                reported_status=rep_status,
                request_text=req_text,
            ),
        },
    ]

    tool_defs = env.get_tool_definitions()
    max_turns = 4
    max_tools = 6
    total_in = 0
    total_out = 0

    try:
        openai_client = llm.get_openai_client()

        for turn in range(max_turns):
            if env.verdicts is not None or env.tool_call_count >= max_tools:
                break

            # Execute model call
            t_call = time.perf_counter()
            resp = openai_client.chat.completions.create(
                model=agent_model,
                messages=messages,
                tools=tool_defs,
                temperature=0.0,
                timeout=120.0,
            )
            lat_ms = (time.perf_counter() - t_call) * 1000.0

            msg = resp.choices[0].message
            inp_tok = 0
            out_tok = 0
            if resp.usage:
                inp_tok = resp.usage.prompt_tokens or 0
                out_tok = resp.usage.completion_tokens or 0
                total_in += inp_tok
                total_out += out_tok

            import usage
            usage.log_usage(
                purpose="agent-turn",
                model=agent_model,
                input_tokens=inp_tok,
                output_tokens=out_tok,
                cache_hit=False,
                latency_ms=lat_ms,
                provider="openai_compat",
            )

            # Append assistant message to history
            assistant_dict: Dict[str, Any] = {"role": "assistant", "content": msg.content or ""}
            if msg.tool_calls:
                assistant_dict["tool_calls"] = [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {"name": tc.function.name, "arguments": tc.function.arguments},
                    }
                    for tc in msg.tool_calls
                ]
            messages.append(assistant_dict)

            # If no tool calls emitted, break
            if not msg.tool_calls:
                break

            # Execute tool calls
            for tc in msg.tool_calls:
                fn_name = tc.function.name
                call_id = tc.id
                raw_args = tc.function.arguments or "{}"
                try:
                    args = json.loads(raw_args)
                except Exception:
                    args = {}

                tool_result = None
                try:
                    if fn_name == "get_case_dossier":
                        tool_result = env.get_case_dossier()
                    elif fn_name == "inspect_resource":
                        tool_result = env.inspect_resource(args.get("resource_id", ""))
                    elif fn_name == "read_evidence_file":
                        tool_result = env.read_evidence_file(
                            args.get("file_id", ""), max_chars=args.get("max_chars", 4000)
                        )
                    elif fn_name == "submit_verdicts":
                        tool_result = env.submit_verdicts(args.get("verdicts", {}))
                    else:
                        tool_result = {"error": f"Unknown tool: {fn_name}"}
                except VerdictValidationError as ve:
                    tool_result = {"error": f"Verdict validation failed: {ve}"}
                except Exception as ex:
                    tool_result = {"error": f"Tool execution failed: {ex}"}

                messages.append({
                    "role": "tool",
                    "tool_call_id": call_id,
                    "content": json.dumps(tool_result),
                })

                if env.verdicts is not None:
                    break

        result["tool_calls"] = env.tool_call_count
        result["input_tokens"] = total_in
        result["output_tokens"] = total_out
        result["agent_verdicts"] = env.verdicts

        # 3. Reconcile agent judgments with baseline
        if env.verdicts is not None:
            agent_intent = env.verdicts.get("intent")

            # Check intent agreement
            if agent_intent == "NON_DELETION" and base_status != "NOT_APPLICABLE":
                # Disagreement on intent: request text intent
                result["disagreement"] = True
                result["escalation_used"] = True

                # Escalate to escalation model
                esc_prompt = prompts.ESCALATION_PROMPT_TEMPLATE.format(
                    case_id=cid,
                    email=email,
                    audit_at=audit_at,
                    request_text=req_text,
                    baseline_status=base_status,
                    agent_verdict=json.dumps(env.verdicts),
                )
                try:
                    esc_res = llm.generate(
                        prompt=esc_prompt,
                        model=escalation_model,
                        purpose="agent-escalation",
                        use_cache=True,
                        timeout_ms=120000,
                    )
                    esc_data = json.loads(esc_res.text) if "{" in esc_res.text else {}
                    esc_intent = esc_data.get("intent")
                    if esc_intent == "NON_DELETION":
                        # Both agent and escalation agree on NON_DELETION
                        result["deletion_status"] = "NOT_APPLICABLE"
                        result["all_steps"] = []
                    else:
                        # Fallback to baseline
                        result["fallback"] = True
                        result["fallback_reason"] = "Escalation rejected non-deletion intent; kept baseline."
                except Exception as ex:
                    result["fallback"] = True
                    result["fallback_reason"] = f"Escalation call failed ({ex}); kept baseline."

            elif agent_intent == "DELETION" and base_status == "NOT_APPLICABLE":
                # Baseline says NOT_APPLICABLE, agent says DELETION
                result["disagreement"] = True
                result["fallback"] = True
                result["fallback_reason"] = "Baseline pattern classified as NOT_APPLICABLE; kept baseline."

            # Check support judgments
            for sj in env.verdicts.get("support_judgments", []):
                s_rid = sj.get("resource_id", "")
                exposes_pii = sj.get("exposes_direct_identifier", False)
                # If agent finds direct identifiers exposed in a support message, verify it has REDACT finding
                if exposes_pii:
                    # Baseline already handles rule_05 (REDACT support message)
                    pass

        else:
            result["fallback"] = True
            result["fallback_reason"] = "Agent did not submit verdicts within turn limit; kept baseline."

        # Save successful verdict to cache
        if use_cache and not result.get("fallback"):
            c = _load_agent_cache()
            c[cid] = result
            _save_agent_cache(c)

    except QuotaExhausted as qe:
        result["fallback"] = True
        result["fallback_reason"] = f"QuotaExhausted: {qe}; kept baseline."
    except Exception as e:
        result["fallback"] = True
        result["fallback_reason"] = f"Agent execution exception ({e}); kept baseline."

    return result

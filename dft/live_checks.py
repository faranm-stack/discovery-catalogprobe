"""F2-F5: deterministic checks on a captured Discovery response.

Input is the OpenAI *Responses* object returned by the workspace data plane
(the same object PR #100 captures): ``output[]`` holds ``function_call``,
``function_call_output`` and assistant ``message`` items.

These checks don't use an LLM judge. They answer "did it work at all?":
- F2 capability: non-empty answer, no "I don't know how" refusal
- F3 tool used: the agent's own tool actually ran (not just the LLM reasoning)
- F3b tool ok: the tool run didn't error
- F4 output: kit/curated expected terms appear in the answer
Quality/safety scoring stays with PR #100's Foundry evaluators.
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from typing import Any

from .findings import Finding
from .plan import TestCase

_STRONG_REFUSAL = [
    r"\bi\s*(?:do not|don't|dont)\s+know how to\b",
    r"\bi(?:'m| am)\s+(?:not able|unable)\s+to\s+(?:do|perform|run|complete|execute|help with|carry out)\b",
    r"\bi\s*(?:cannot|can't|can not)\s+(?:do|perform|run|complete|execute|help with|carry out)\s+(?:this|that|the)\b",
    r"\b(?:i\s+)?(?:do not|don't)\s+have\s+(?:a|any|the)\s+(?:tool|capabilit\w*|abilit\w*)\s+(?:to|for|that)\b",
    r"\bno\s+(?:suitable\s+)?tools?\s+(?:is|are)\s+available\b",
    r"\b(?:tool|function|action)\s+['`\"]?[\w\-]+['`\"]?\s+(?:does not|doesn't)\s+exist\b",
]
_WEAK_REFUSAL = [r"\bunable to\b", r"\bcannot\b", r"\bcan't\b", r"\bnot able to\b", r"\bnot supported\b"]
_TOOL_ERROR = re.compile(
    r'"status"\s*:\s*"(?:failed|error)"|Traceback \(most recent call last\)|'
    r'exit[_ ]?code["\']?\s*[:=]\s*[1-9]|\b(?:ModuleNotFoundError|ImportError|TypeError|AttributeError|'
    r'ImagePullBackOff|ErrImagePull|AuthorizationFailed|Forbidden)\b')
_PLATFORM_HINT = re.compile(r"\b(?:401|403|forbidden|unauthori[sz]ed|authorizationfailed|quota|throttl|"
                            r"imagepull|errimagepull|no such host|connection reset)\b", re.I)
_DEPLOY_HINT = re.compile(r"\bagent\b.*\b(?:not found|does not exist|doesn't exist)\b|\b404\b", re.I)


@dataclass
class CaseResult:
    case_id: str
    subject: str
    agent: str
    origin: str
    status: str  # PASS | WARN | FAIL
    findings: list[dict[str, Any]]
    tool_calls: list[str] = field(default_factory=list)
    own_tool_calls: int = 0
    answer_excerpt: str = ""
    latency_s: float | None = None
    conversation: str = ""
    response_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def final_text(response: dict[str, Any]) -> str:
    parts = []
    for item in response.get("output") or []:
        if item.get("type") == "message" and item.get("role", "assistant") == "assistant":
            content = item.get("content")
            if isinstance(content, str):
                parts.append(content)
            elif isinstance(content, list):
                parts.extend(c.get("text", "") for c in content
                             if isinstance(c, dict) and c.get("type") in ("output_text", "text"))
    if not parts and isinstance(response.get("output_text"), str):
        parts.append(response["output_text"])
    return "\n".join(p for p in parts if p).strip()


def tool_calls(response: dict[str, Any]) -> list[tuple[str, str, str]]:
    """(name, call_id, output_text) for each function_call, paired by call_id."""
    outputs = {}
    for item in response.get("output") or []:
        if item.get("type") == "function_call_output":
            out = item.get("output")
            outputs[item.get("call_id")] = out if isinstance(out, str) else json.dumps(out, default=str)
    return [(str(i.get("name", "")), str(i.get("call_id", "")), outputs.get(i.get("call_id"), ""))
            for i in response.get("output") or [] if i.get("type") == "function_call"]


def is_own_tool(call_name: str, tool_names: list[str]) -> bool:
    n = _norm(call_name)
    return any(_norm(t) and _norm(t) in n for t in tool_names)


def detect_refusal(text: str) -> tuple[str, str] | None:
    lowered = text.lower()
    for pat in _STRONG_REFUSAL:
        m = re.search(pat, lowered)
        if m:
            return "error", m.group(0)
    head = lowered[:300]
    for pat in _WEAK_REFUSAL:
        m = re.search(pat, head)
        if m:
            return "warning", m.group(0)
    return None


def _term_found(term: str, text: str) -> bool:
    if len(term) <= 3:  # units such as "pa", "k", "n.m"
        return re.search(rf"(?<![a-z]){re.escape(term)}(?![a-z])", text) is not None
    return all(w in text for w in term.split())


def classify_exception(exc: BaseException) -> tuple[str, str]:
    msg = str(exc)
    if isinstance(exc, TimeoutError) or "timed out" in msg.lower():
        return "timeout", msg
    # PR #100's client raises "HTTP <code> on <METHOD> <url>\n<body>\n(If 401/403 ... If 404 ...)",
    # so use the leading status code, not any number that appears in the hint text.
    status = re.match(r"\s*HTTP (\d{3})\b", msg)
    if status:
        return ("deploy" if status.group(1) == "404" else "platform"), msg
    if _DEPLOY_HINT.search(msg):
        return "deploy", msg
    return "platform", msg


def evaluate(case: TestCase, response: dict[str, Any] | None, *, error: BaseException | None = None,
             latency_s: float | None = None, conversation: str = "") -> CaseResult:
    findings: list[Finding] = []
    subj = case.subject

    def add(check: str, sev: str, cat: str, msg: str, key: str, **ev: Any) -> None:
        findings.append(Finding(check, sev, cat, subj, msg, evidence={"key": key, "case": case.id, **ev}))

    if error is not None or response is None:
        cat, msg = classify_exception(error) if error else ("platform", "no response captured")
        add("F1.invoke", "error", cat, f"Invocation failed: {msg[:400]}", f"invoke:{cat}")
        return _result(case, findings, [], 0, "", latency_s, conversation, "")

    status = str(response.get("status", ""))
    text = final_text(response)
    calls = tool_calls(response)
    own = [c for c in calls if is_own_tool(c[0], case.tool_names)]

    if status and status not in ("completed", "succeeded"):
        err = json.dumps(response.get("error") or response.get("incomplete_details") or {}, default=str)
        cat = "deploy" if _DEPLOY_HINT.search(err) else ("timeout" if "timeout" in err.lower() else "platform")
        add("F1.invoke", "error", cat, f"Response ended with status '{status}': {err[:300]}", f"status:{status}")

    if not text:
        add("F2.capability", "error", "capability", "Empty final answer.", "empty-answer")
    else:
        refusal = detect_refusal(text)
        if refusal:
            sev, phrase = refusal
            add("F2.capability", sev, "capability",
                f"Answer reads like a refusal/inability ('{phrase}').", "refusal", phrase=phrase)

    if case.requires_tool:
        if not own:
            add("F3.tool-used", "error", "tool-not-used",
                "Agent answered without running its own tool" +
                (f" (called only: {', '.join(sorted({c[0] for c in calls}))})." if calls else "."),
                "no-own-tool-call", calls=[c[0] for c in calls])
        else:
            failed = [c for c in own if _TOOL_ERROR.search(c[2] or "")]
            if failed:
                all_failed = len(failed) == len(own)
                sample = (failed[-1][2] or "")[:300]
                cat = "platform" if _PLATFORM_HINT.search(sample) else "tool-error"
                add("F3.tool-ok", "error" if all_failed else "warning", cat,
                    f"{len(failed)}/{len(own)} own-tool call(s) returned an error: {sample}",
                    "tool-error", failed=[c[0] for c in failed])

    if case.expect and text:
        lowered = text.lower()
        missing = [t for t in case.expect if not _term_found(t, lowered)]
        found = len(case.expect) - len(missing)
        if found / len(case.expect) < 0.5:
            add("F4.output", "warning", "wrong-output",
                f"Answer covers {found}/{len(case.expect)} expected terms; missing: {', '.join(missing)}.",
                "expected-terms", missing=missing)

    return _result(case, findings, [c[0] for c in calls], len(own), text, latency_s, conversation,
                   str(response.get("id", "")))


def _result(case: TestCase, findings: list[Finding], calls: list[str], own: int, text: str,
            latency_s: float | None, conversation: str, response_id: str) -> CaseResult:
    sev = {f.severity for f in findings}
    status = "FAIL" if "error" in sev else ("WARN" if "warning" in sev else "PASS")
    return CaseResult(case.id, case.subject, case.agent, case.origin, status,
                      [f.to_dict() for f in findings], calls, own, text[:600], latency_s,
                      conversation, response_id)

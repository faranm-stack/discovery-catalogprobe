"""Build functional test cases (what to ask each agent / kit).

Sources, in priority order:
1. starter-kit samplePrompts (the kit author's own contract, with expectedOutput)
2. curated smoke files: smoke/<agent>.yaml
3. auto-generated smoke prompts from the agent's tool definition (origin="auto")

Auto prompts only prove "it runs and uses its tool". They are marked so a
failure on an auto case is triaged before blaming the agent.
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable

import yaml

from .catalog import Agent, Catalog, Kit


@dataclass
class TestCase:
    __test__ = False  # not a pytest class

    id: str
    subject: str
    agent: str
    prompt: str
    origin: str  # kit | curated | auto
    requires_tool: bool
    expect: list[str] = field(default_factory=list)  # substrings/terms the answer should contain
    kit_agents: list[str] = field(default_factory=list)
    tool_names: list[str] = field(default_factory=list)  # names of the agent's own tools/actions
    routing: str = ""  # kits only: curated (smoke/kits/<kit>.yaml), named (prompt names it), primary

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def expected_terms(expected_output: str) -> list[str]:
    """Turn a kit's free-text expectedOutput into checkable terms.

    "Pressure drop [Pa], inlet/outlet pressures." -> ["pressure drop", "pa", "inlet", "outlet"]
    Units in brackets are kept; filler words are dropped.
    """
    stop = {"and", "or", "the", "a", "an", "of", "if", "applicable", "with", "for", "to", "in",
            "per", "e.g", "etc", "from", "on", "by", "summary", "values", "value", "results"}
    terms: list[str] = []
    for chunk in re.split(r"[,;/()]|\band\b", expected_output):
        chunk = chunk.strip().strip(".").strip()
        if not chunk:
            continue
        for unit in re.findall(r"\[([^\]]+)\]", chunk):
            terms.append(unit.strip().lower())
        words = re.sub(r"\[[^\]]*\]", "", chunk).strip().lower()
        if words and words not in stop and len(words) > 2:
            terms.append(words)
    seen, out = set(), []
    for t in terms:
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out


def _tool_names(agent: Agent) -> list[str]:
    names = {t.name for t in agent.tools} | agent.action_names
    return sorted(n for n in names if n)


def _auto_case(agent: Agent) -> TestCase:
    if agent.action_names:
        action = sorted(agent.action_names)[0]
        prompt = (f"Smoke test: run your `{action}` action once with its default parameters on a "
                  "minimal example (create any tiny input you need). Then report the key values "
                  "from final_results.json and the exact action you invoked.")
    elif agent.tools:
        prompt = ("Smoke test: using your compute tool (not your own reasoning), run a minimal script "
                  "that imports your main helper module and writes final_results.json containing "
                  '{"status": "ok"} and the versions of your main packages. Report what you ran and '
                  "the versions it printed.")
    else:
        prompt = ("In three sentences, say what kinds of tasks you can do, then complete one small "
                  "example task from your own domain.")
    return TestCase(f"{agent.name}#auto", agent.subject, agent.name, prompt, "auto",
                    requires_tool=bool(agent.tools), tool_names=_tool_names(agent))


def _curated_cases(agent: Agent, smoke_dir: Path) -> list[TestCase]:
    path = smoke_dir / f"{agent.name}.yaml"
    if not path.exists():
        return []
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    cases = []
    for i, c in enumerate(data.get("cases") or [], 1):
        cases.append(TestCase(f"{agent.name}#{c.get('id', i)}", agent.subject, agent.name, c["prompt"],
                              "curated", bool(c.get("requires_tool", bool(agent.tools))),
                              [str(e).lower() for e in c.get("expect") or []],
                              tool_names=_tool_names(agent)))
    return cases


def route_kit_prompt(prompt: str, refs: list[str], catalog: Catalog, primary: str) -> tuple[str, str]:
    """Pick the kit agent a sample prompt is meant for.

    Services deploy a kit as individual agents, so someone has to choose which agent gets each
    sample prompt. Use the agent the prompt names first ("Dock ... with AutoDock" -> autodock);
    otherwise fall back to the kit's primary agent.
    """
    text = _normalize(prompt)
    best: tuple[int, str] | None = None
    for ref in refs:
        agent = catalog.agents.get(ref)
        if not agent:
            continue
        labels = {ref, str(agent.doc.get("displayName") or ""), str(agent.doc.get("name") or "")}
        for label in filter(None, (_normalize(x) for x in labels)):
            if len(label) < 4:
                continue
            pos = text.find(label)
            if pos >= 0 and (best is None or pos < best[0]):
                best = (pos, ref)
    return (best[1], "named") if best else (primary, "primary")


def _normalize(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def kit_cases(kit: Kit, catalog: Catalog, smoke_dir: Path | None = None) -> list[TestCase]:
    refs = [str(r.get("ref", "")).removeprefix("agents/") for r in kit.agent_refs]
    primary = next((str(r["ref"]).removeprefix("agents/") for r in kit.agent_refs
                    if r.get("role") == "primary"), refs[0] if refs else "")
    overrides: dict[str, str] = {}
    route_file = smoke_dir / "kits" / f"{kit.name}.yaml" if smoke_dir else None
    if route_file and route_file.exists():
        overrides = (yaml.safe_load(route_file.read_text(encoding="utf-8")) or {}).get("routes") or {}
    cases = []
    for sp in kit.sample_prompts:
        sp_id = str(sp.get("id"))
        if sp_id in overrides:
            target, routing = str(overrides[sp_id]), "curated"
        else:
            target, routing = route_kit_prompt(sp.get("prompt", ""), refs, catalog, primary)
        agent = catalog.agents.get(target)
        cases.append(TestCase(f"{kit.name}#{sp.get('id')}", f"{kit.subject}#{sp.get('id')}", target,
                              sp.get("prompt", ""), "kit", requires_tool=bool(agent and agent.tools),
                              expect=expected_terms(sp.get("expectedOutput", "")), kit_agents=refs,
                              tool_names=_tool_names(agent) if agent else [], routing=routing))
    return cases


def build_plan(catalog: Catalog, *, agents: Iterable[str] | None = None, kits: Iterable[str] | None = None,
               smoke_dir: str | Path = "smoke", include_auto: bool = True) -> list[TestCase]:
    smoke_dir = Path(smoke_dir)
    plan: list[TestCase] = []
    for name in sorted(set(agents) if agents is not None else set()):
        agent = catalog.agents.get(name)
        if not agent:
            continue
        curated = _curated_cases(agent, smoke_dir)
        plan.extend(curated or ([_auto_case(agent)] if include_auto else []))
    for name in sorted(set(kits) if kits is not None else set()):
        if name in catalog.kits:
            plan.extend(kit_cases(catalog.kits[name], catalog, smoke_dir))
    return plan


# PR #100 (utilities/agent-evaluation) reads "<suite>-evaluators.json" files.
PR100_EVALUATORS_TOOL = ["builtin.task_completion", "builtin.intent_resolution",
                         "builtin.tool_call_success", "builtin.tool_call_accuracy"]
PR100_EVALUATORS_CHAT = ["builtin.task_completion", "builtin.intent_resolution", "builtin.coherence"]


def export_pr100(plan: list[TestCase], out_dir: str | Path) -> list[Path]:
    """Write one PR #100 dataset per agent so the same prompts can be scored by Foundry evaluators."""
    out_dir = Path(out_dir)
    by_agent: dict[str, list[TestCase]] = {}
    for case in plan:
        by_agent.setdefault(case.agent, []).append(case)
    written = []
    for agent, cases in sorted(by_agent.items()):
        uses_tools = any(c.requires_tool for c in cases)
        doc = {
            "name": "dft-smoke-evaluators",
            "_comment": "Generated by discovery-functional-tests `dft plan --emit-pr100`. Same prompts "
                        "as the functional run, scored by the PR #100 agent-evaluation pipeline.",
            "evaluators": PR100_EVALUATORS_TOOL if uses_tools else PR100_EVALUATORS_CHAT,
            "data": [{"query": c.prompt, "_case_id": c.id, "_origin": c.origin} for c in cases],
        }
        path = out_dir / agent / "dft-smoke-evaluators.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
        written.append(path)
    return written

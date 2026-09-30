"""F0: static-semantic checks.

These go beyond the repo's schema/policy validators (#132). They ask whether
an agent's *instructions* agree with the tool it ships:

- F0.actions   - actions/parameters described in instructions exist in tool.yaml
- F0.imports   - helper modules/names the instructions tell the LLM to import exist
- F0.calls     - example calls in instructions match the real Python signatures
- F0.bindings  - tool placeholders can be bound to a shipped tool
- F0.model     - model id is the deploy-time placeholder
- F0.deprecated- instructions/tool definitions avoid known deprecated identifiers
- F0.kit       - starter-kit agentRefs resolve and sample prompts are testable

Everything here is offline and read-only, so it is safe to run on any runner.
"""
from __future__ import annotations

import ast
import re
import textwrap
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator

import yaml

from .catalog import Agent, Catalog, Kit, Signature
from .findings import Finding

_ACTION_HEAD = re.compile(r"^#{2,4}\s*Action:\s*`?([A-Za-z0-9_\-]+)`?", re.M)
_ANY_HEAD = re.compile(r"^#{1,4}\s", re.M)
_PARAM_BULLET = re.compile(r"^\s*[-*]\s*`([A-Za-z_][A-Za-z0-9_]*)`", re.M)
_FENCE = re.compile(r"```[A-Za-z0-9_+-]*[ \t]*\n(.*?)```", re.S)
_INLINE = re.compile(r"`([^`\n]*\([^`\n]*\))`")
_FROM_IMPORT = re.compile(r"^[ \t]*from[ \t]+([A-Za-z_][\w.]*)[ \t]+import[ \t]+(\([^)]*\)|[^\n]+)", re.M)
_PLAIN_IMPORT = re.compile(r"^[ \t]*import[ \t]+([A-Za-z_][\w]*)", re.M)
_DOC_SIG = re.compile(r"^[ \t]*[-*][ \t]*`?([A-Za-z_][A-Za-z0-9_]*)\(([^()`]*)\)", re.M)
_HELPER_SUFFIX = "_utils"
_GENERIC_DIRS = {"input_directory", "output_directory"}


@dataclass
class Deprecation:
    pattern: str
    reason: str
    replacement: str = ""


def load_deprecations(path: str | Path | None) -> list[Deprecation]:
    if not path or not Path(path).exists():
        return []
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    return [Deprecation(**d) for d in data.get("deprecated") or []]


def _line_of(raw_lines: list[str], *needles: str) -> int | None:
    for i, line in enumerate(raw_lines, 1):
        if all(n in line for n in needles):
            return i
    return None


def _line_of_call(raw_lines: list[str], fn: str, bad_kw: str | None) -> int | None:
    """Line of the offending keyword inside a (possibly multi-line) call to fn."""
    if not bad_kw:
        return _line_of(raw_lines, f"{fn}(")
    for i, line in enumerate(raw_lines):
        if f"{fn}(" in line:
            for j in range(i, min(i + 6, len(raw_lines))):
                if f"{bad_kw}=" in raw_lines[j]:
                    return j + 1
    return _line_of(raw_lines, f"{fn}(")


def _strip_comments(text: str) -> str:
    return "\n".join(line.split("#", 1)[0] for line in text.splitlines())


# --------------------------------------------------------------------------- actions
def check_actions(agent: Agent) -> Iterator[Finding]:
    heads = _ACTION_HEAD.findall(agent.instructions)
    actions = {a: act for t in agent.tools for a, act in t.actions.items()}
    if not heads or not actions:
        return
    path = str(agent.path / "agent.yaml")
    for name in sorted(set(heads) - set(actions)):
        yield Finding("F0.actions", "error", "definition", agent.subject,
                      f"Instructions describe action '{name}' but no tool.yaml defines it; the LLM "
                      "will try to call a function that doesn't exist.",
                      path, evidence={"key": f"missing-action:{name}", "defined": sorted(actions)})
    for name in sorted(set(actions) - set(heads)):
        yield Finding("F0.actions", "warning", "definition", agent.subject,
                      f"tool.yaml action '{name}' is never described in the instructions, so the "
                      "agent is unlikely to use it.",
                      path, evidence={"key": f"undocumented-action:{name}"})

    starts = [(m.group(1), m.end()) for m in _ACTION_HEAD.finditer(agent.instructions)]
    for name, start in starts:
        if name not in actions:
            continue
        nxt = _ANY_HEAD.search(agent.instructions, start)
        section = agent.instructions[start: nxt.start() if nxt else None]
        documented = set(_PARAM_BULLET.findall(section))
        for param in sorted(documented - actions[name].properties):
            yield Finding("F0.actions", "error", "definition", agent.subject,
                          f"Action '{name}' documents parameter '{param}' that its input_schema "
                          "doesn't accept.",
                          path, evidence={"key": f"unknown-param:{name}.{param}",
                                          "accepted": sorted(actions[name].properties)})
        for param in sorted(actions[name].required - _GENERIC_DIRS):
            if param not in agent.instructions:
                yield Finding("F0.actions", "warning", "definition", agent.subject,
                              f"Action '{name}' requires '{param}' but the instructions never "
                              "mention it.",
                              path, evidence={"key": f"undocumented-required:{name}.{param}"})


# --------------------------------------------------------------------------- imports
def _imports(text: str) -> Iterator[tuple[str, list[str], bool]]:
    """(module, imported names, from_inline_prose) for each import statement in the text."""
    for module, names in _FROM_IMPORT.findall(text):
        cleaned = _strip_comments(names.strip("()"))
        items = [n.strip().split(" as ")[0].strip() for n in re.split(r"[,\n]", cleaned)]
        yield module, [n for n in items if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", n or "")], False
    for module in _PLAIN_IMPORT.findall(text):
        yield module, [], False
    # Inline prose such as "e.g. `from molecular_utils import function_name`": the module must
    # exist, but the names are often placeholders, so they aren't checked.
    for span in re.findall(r"`([^`\n]+)`", text):
        m = re.match(r"\s*(?:from\s+([A-Za-z_][\w.]*)\s+import|import\s+([A-Za-z_][\w]*))", span)
        if m:
            yield (m.group(1) or m.group(2)), [], True


def _is_helper(agent: Agent, module: str) -> bool:
    return module in agent.modules or module.endswith(_HELPER_SUFFIX)


def check_imports(agent: Agent) -> Iterator[Finding]:
    text = agent.instructions
    if not text:
        return
    raw = (agent.path / "agent.yaml").read_text(encoding="utf-8").splitlines()
    path = str(agent.path / "agent.yaml")
    seen: set[str] = set()
    for module, names, _inline in _imports(text):
        base = module.split(".")[0]
        if not _is_helper(agent, base):
            continue  # third-party or stdlib; the container decides
        if base not in agent.modules:
            if f"module:{base}" not in seen:
                seen.add(f"module:{base}")
                yield Finding("F0.imports", "error", "definition", agent.subject,
                              f"Instructions import helper module '{base}' but no {base}.py ships "
                              "in the agent's tools/.",
                              path, _line_of(raw, base), {"key": f"missing-module:{base}"})
            continue
        for name in names:
            if name not in agent.modules[base] and f"{base}.{name}" not in seen:
                seen.add(f"{base}.{name}")
                yield Finding("F0.imports", "error", "definition", agent.subject,
                              f"Instructions import '{name}' from {base}, but {base}.py doesn't "
                              "define it (removed or renamed?).",
                              path, _line_of(raw, "import", name), {"key": f"missing-name:{base}.{name}"})
    for base, name in re.findall(r"\b([a-z][a-z0-9_]*)\.([A-Za-z_][A-Za-z0-9_]*)\b(?!\.)",
                                 _strip_comments(text)):
        if base in agent.modules and name != "py" and name not in agent.modules[base] \
                and f"{base}.{name}" not in seen:
            seen.add(f"{base}.{name}")
            yield Finding("F0.imports", "error", "definition", agent.subject,
                          f"Instructions reference {base}.{name}, which {base}.py doesn't define.",
                          path, _line_of(raw, f"{base}.{name}"), {"key": f"missing-name:{base}.{name}"})


# --------------------------------------------------------------------------- calls
def _snippets(text: str) -> Iterator[ast.AST]:
    for block in _FENCE.findall(text):
        for candidate in (block, textwrap.dedent(block)):
            try:
                yield ast.parse(candidate)
                break
            except SyntaxError:
                continue
    for span in _INLINE.findall(text):
        try:
            yield ast.parse(span.strip())
        except SyntaxError:
            continue


def _accepts(sig: Signature, n_pos: int, kwargs: list[str]) -> tuple[bool, str]:
    if n_pos > sig.positional and not sig.has_varargs:
        return False, f"{n_pos} positional args but it takes {sig.positional}"
    unknown = [k for k in kwargs if k not in sig.params]
    if unknown and not sig.has_kwargs:
        return False, f"unknown keyword(s) {', '.join(unknown)}"
    return True, ""


def _callable_names(agent: Agent) -> set[str]:
    names = {n for n, sigs in agent.functions.items() if sigs}
    return names


def check_calls(agent: Agent) -> Iterator[Finding]:
    text = agent.instructions
    known = _callable_names(agent)
    if not text or not known:
        return
    raw = (agent.path / "agent.yaml").read_text(encoding="utf-8").splitlines()
    path = str(agent.path / "agent.yaml")
    reported: set[str] = set()

    for tree in _snippets(text):
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if isinstance(node.func, ast.Name):
                fn = node.func.id
            elif isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name) \
                    and node.func.value.id in agent.modules:
                fn = node.func.attr
            else:
                continue
            if fn not in known:
                continue
            if any(isinstance(a, ast.Starred) for a in node.args) or any(k.arg is None for k in node.keywords):
                continue
            if any(isinstance(a, ast.Constant) and a.value is Ellipsis for a in node.args):
                continue  # "f(a, b, ...)" is prose shorthand, not a call
            kwargs = [k.arg for k in node.keywords if k.arg]
            results = [_accepts(s, len(node.args), kwargs) for s in agent.functions[fn]]
            if any(ok for ok, _ in results):
                continue
            sig = agent.functions[fn][0]
            why = results[0][1]
            key = f"bad-call:{fn}:{why}"
            if key in reported:
                continue
            reported.add(key)
            bad_kw = next((k for k in kwargs if k not in sig.params), None)
            line = _line_of_call(raw, fn, bad_kw)
            yield Finding("F0.calls", "error", "definition", agent.subject,
                          f"Example call {fn}(...) in the instructions would fail: {why}. "
                          f"Real signature: {fn}({', '.join(sig.params)}) in "
                          f"{Path(sig.path).name}:{sig.line}.",
                          path, line, {"key": key, "signature": list(sig.params)})

    # Signature summaries such as "- plot_x(data, labels, title, output_path) -- ...".
    # Only keyword-style names (`name=` or bare identifiers) are compared; a renamed
    # parameter here is what an LLM later passes as a keyword.
    for fn, arglist in _DOC_SIG.findall(text):
        if fn not in known:
            continue
        documented = _doc_params(arglist)
        sigs = agent.functions[fn]
        if not documented or any(s.has_kwargs or all(d in s.params for d in documented) for s in sigs):
            continue
        sig = sigs[0]
        missing = [d for d in documented if d not in sig.params]
        key = f"doc-sig:{fn}"
        if key in reported:
            continue
        reported.add(key)
        yield Finding("F0.calls", "warning", "definition", agent.subject,
                      f"Instructions document {fn}({', '.join(documented)}) but the real signature is "
                      f"{fn}({', '.join(sig.params)}); unknown name(s): {', '.join(missing)}.",
                      path, _line_of(raw, f"{fn}("), {"key": key, "unknown": missing})


def _split_top_level(arglist: str) -> list[str]:
    parts, depth, quote, cur = [], 0, "", []
    for ch in arglist:
        if quote:
            cur.append(ch)
            if ch == quote:
                quote = ""
            continue
        if ch in "'\"":
            quote = ch
        elif ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        elif ch == "," and depth == 0:
            parts.append("".join(cur))
            cur = []
            continue
        cur.append(ch)
    parts.append("".join(cur))
    return parts


def _doc_params(arglist: str) -> list[str]:
    names = []
    for token in _split_top_level(arglist):
        name = token.split("=")[0].split(":")[0].strip()
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) and name not in ("self", "cls"):
            names.append(name)
    return names


# --------------------------------------------------------------------------- bindings/model
def check_bindings(agent: Agent, catalog: Catalog) -> Iterator[Finding]:
    path = str(agent.path / "agent.yaml")
    bindings = agent.tool_bindings
    placeholders = [b for b in bindings if str(b.get("toolId", "")).startswith("{{")]
    assoc = [p for p in agent.metadata.get("associated_tools") or [] if isinstance(p, str)]
    resolvable = {p for p in assoc if (catalog.root / p / "tool.yaml").exists()}
    resolvable |= {str(t.path.relative_to(catalog.root)).replace("\\", "/") for t in agent.tools}
    if len(placeholders) > len(resolvable):
        yield Finding("F0.bindings", "error", "definition", agent.subject,
                      f"{len(placeholders)} tool placeholder(s) but only {len(resolvable)} shipped/"
                      "associated tool(s); at least one binding can't be resolved at deploy time.",
                      path, evidence={"key": "unbound-placeholder"})
    if agent.tools and not bindings:
        yield Finding("F0.bindings", "warning", "definition", agent.subject,
                      "Agent ships a tool but binds none in discoveryExtensions.tools, so it can't call it.",
                      path, evidence={"key": "tool-not-bound"})
    for b in bindings:
        tid = str(b.get("toolId", ""))
        if "/subscriptions/" in tid.lower():
            yield Finding("F0.bindings", "warning", "definition", agent.subject,
                          "toolId is a hard-coded resource id from a specific subscription; use a "
                          "{{...ToolId}} placeholder.", path, evidence={"key": "hardcoded-toolid"})


def check_model(agent: Agent) -> Iterator[Finding]:
    model = agent.doc.get("model") or {}
    mid = str(model.get("id", "")) if isinstance(model, dict) else ""
    if mid and mid != "{{CHAT-MODEL}}":
        yield Finding("F0.model", "warning", "definition", agent.subject,
                      f"model.id is hard-coded to '{mid}' instead of {{{{CHAT-MODEL}}}}; it breaks "
                      "when that deployment doesn't exist in the target workspace.",
                      str(agent.path / "agent.yaml"), evidence={"key": "hardcoded-model"})


def check_deprecated(agent: Agent, deprecations: list[Deprecation]) -> Iterator[Finding]:
    if not deprecations:
        return
    sources = [agent.path / "agent.yaml", *[t.path / "tool.yaml" for t in agent.tools]]
    for src in sources:
        lines = src.read_text(encoding="utf-8").splitlines()
        for dep in deprecations:
            rx = re.compile(dep.pattern)
            for i, line in enumerate(lines, 1):
                if rx.search(line):
                    hint = f" Use {dep.replacement}." if dep.replacement else ""
                    yield Finding("F0.deprecated", "error", "definition", agent.subject,
                                  f"Uses deprecated '{dep.pattern}': {dep.reason}.{hint}",
                                  str(src), i, {"key": f"deprecated:{dep.pattern}"})
                    break


# --------------------------------------------------------------------------- kits
def check_kit(kit: Kit, catalog: Catalog) -> Iterator[Finding]:
    path = str(kit.path / "kit.json")
    for ref in kit.agent_refs:
        name = str(ref.get("ref", "")).removeprefix("agents/")
        if name not in catalog.agents:
            yield Finding("F0.kit", "error" if ref.get("required") else "warning", "definition",
                          kit.subject, f"agentRef '{ref.get('ref')}' doesn't resolve to agents/<name>/agent.yaml.",
                          path, evidence={"key": f"missing-agent:{name}"})
    if not kit.sample_prompts:
        yield Finding("F0.kit", "warning", "definition", kit.subject,
                      "No samplePrompts, so the kit can't be functionally tested.", path,
                      evidence={"key": "no-sample-prompts"})
    for sp in kit.sample_prompts:
        if not sp.get("expectedOutput"):
            yield Finding("F0.kit", "warning", "definition", kit.subject,
                          f"samplePrompt '{sp.get('id')}' has no expectedOutput, so output can't be checked.",
                          path, evidence={"key": f"no-expected:{sp.get('id')}"})


# --------------------------------------------------------------------------- driver
def run_static(catalog: Catalog, *, agents: Iterable[str] | None = None, kits: Iterable[str] | None = None,
               deprecations: list[Deprecation] | None = None) -> list[Finding]:
    wanted_agents = set(agents) if agents is not None else set(catalog.agents)
    wanted_kits = set(kits) if kits is not None else set(catalog.kits)
    findings: list[Finding] = []
    for subject, err in catalog.load_errors:
        findings.append(Finding("F0.load", "error", "definition", subject, f"Could not parse: {err}"))
    for name in sorted(wanted_agents & set(catalog.agents)):
        agent = catalog.agents[name]
        for check in (check_actions(agent), check_imports(agent), check_calls(agent),
                      check_bindings(agent, catalog), check_model(agent),
                      check_deprecated(agent, deprecations or [])):
            findings.extend(check)
    for name in sorted(wanted_kits & set(catalog.kits)):
        findings.extend(check_kit(catalog.kits[name], catalog))
    for f in findings:  # repo-relative paths: portable across machines and linkable on GitHub
        if f.path:
            try:
                f.path = Path(f.path).resolve().relative_to(catalog.root.resolve()).as_posix()
            except ValueError:
                pass
    return findings

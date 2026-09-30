"""Load agents, tools and starter kits from a microsoft/discovery checkout.

Parsing only: nothing from the catalog is imported or executed. Tool Python
files are read with ``ast.parse`` to learn function signatures.
"""
from __future__ import annotations

import ast
import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True)
class Signature:
    module: str
    params: tuple[str, ...]
    positional: int  # number of parameters that can be passed positionally
    has_varargs: bool
    has_kwargs: bool
    path: str
    line: int


@dataclass
class Action:
    name: str
    properties: set[str]
    required: set[str]


@dataclass
class Tool:
    name: str
    path: Path
    doc: dict[str, Any]
    actions: dict[str, Action]


@dataclass
class Agent:
    name: str
    path: Path
    doc: dict[str, Any]
    metadata: dict[str, Any]
    tools: list[Tool]
    functions: dict[str, list[Signature]] = field(default_factory=dict)
    # module stem -> top-level names it defines (functions, classes, constants)
    modules: dict[str, set[str]] = field(default_factory=dict)

    @property
    def subject(self) -> str:
        return f"agents/{self.name}"

    @property
    def instructions(self) -> str:
        return self.doc.get("instructions") or ""

    @property
    def tool_bindings(self) -> list[dict[str, Any]]:
        ext = self.doc.get("discoveryExtensions") or {}
        return [t for t in (ext.get("tools") or []) if isinstance(t, dict)]

    @property
    def action_names(self) -> set[str]:
        return {a for t in self.tools for a in t.actions}


@dataclass
class Kit:
    name: str
    path: Path
    doc: dict[str, Any]

    @property
    def subject(self) -> str:
        return f"starter-kits/{self.name}"

    @property
    def agent_refs(self) -> list[dict[str, Any]]:
        return [r for r in self.doc.get("agentRefs") or [] if isinstance(r, dict)]

    @property
    def sample_prompts(self) -> list[dict[str, Any]]:
        return [p for p in self.doc.get("samplePrompts") or [] if isinstance(p, dict)]


@dataclass
class Catalog:
    root: Path
    commit: str
    agents: dict[str, Agent]
    kits: dict[str, Kit]
    load_errors: list[tuple[str, str]]


def _read_yaml(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else {}


def _signature(module: str, node: ast.FunctionDef | ast.AsyncFunctionDef, path: Path,
               bound: bool = False) -> Signature:
    a = node.args
    positional = a.posonlyargs + a.args
    if bound and positional:
        positional = positional[1:]  # drop self/cls
    params = tuple(x.arg for x in positional + a.kwonlyargs)
    return Signature(module, params, len(positional), a.vararg is not None, a.kwarg is not None,
                     str(path), node.lineno)


def _is_static(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    return any(isinstance(d, ast.Name) and d.id == "staticmethod" for d in node.decorator_list)


def _scan_python(tool_dirs: list[Path]) -> tuple[dict[str, list[Signature]], dict[str, set[str]]]:
    functions: dict[str, list[Signature]] = {}
    modules: dict[str, set[str]] = {}
    for tdir in tool_dirs:
        for py in sorted(tdir.rglob("*.py")):
            names = modules.setdefault(py.stem, set())
            try:
                tree = ast.parse(py.read_text(encoding="utf-8", errors="replace"))
            except SyntaxError:
                continue
            for node in tree.body:
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    names.add(node.name)
                    functions.setdefault(node.name, []).append(_signature(py.stem, node, py))
                elif isinstance(node, ast.ClassDef):
                    names.add(node.name)
                    # Methods are registered too, so `obj.method(...)` docs and same-named
                    # module functions don't produce false "unknown argument" findings.
                    for item in node.body:
                        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                                and not item.name.startswith("__"):
                            functions.setdefault(item.name, []).append(
                                _signature(py.stem, item, py, bound=not _is_static(item)))
                elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                    names.update(t.id for t in targets if isinstance(t, ast.Name))
                elif isinstance(node, (ast.Import, ast.ImportFrom)):
                    # re-exports: `from x import y` makes `y` importable from this module
                    names.update((a.asname or a.name).split(".")[0] for a in node.names if a.name != "*")
    return functions, modules


def _load_tool(tool_yaml: Path) -> Tool:
    doc = _read_yaml(tool_yaml)
    actions = {}
    for act in doc.get("actions") or []:
        if not isinstance(act, dict) or not act.get("name"):
            continue
        schema = act.get("input_schema") or {}
        props = schema.get("properties") or {}
        actions[act["name"]] = Action(act["name"], set(props), set(schema.get("required") or []))
    return Tool(doc.get("name") or tool_yaml.parent.name, tool_yaml.parent, doc, actions)


def _git_commit(root: Path) -> str:
    try:
        out = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True,
                             text=True, timeout=20, check=False)
        return out.stdout.strip() or "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def load_catalog(root: str | Path, commit: str | None = None) -> Catalog:
    root = Path(root)
    if not (root / "agents").is_dir():
        raise FileNotFoundError(f"{root} does not look like a microsoft/discovery checkout (no agents/)")
    agents: dict[str, Agent] = {}
    kits: dict[str, Kit] = {}
    errors: list[tuple[str, str]] = []

    for agent_yaml in sorted(root.glob("agents/*/agent.yaml")):
        adir = agent_yaml.parent
        try:
            doc = _read_yaml(agent_yaml)
            meta_path = adir / "metadata.yaml"
            metadata = _read_yaml(meta_path) if meta_path.exists() else {}
            tools = [_load_tool(t) for t in sorted(adir.glob("tools/*/tool.yaml"))]
            functions, modules = _scan_python([adir / "tools"] if (adir / "tools").is_dir() else [])
            agents[adir.name] = Agent(adir.name, adir, doc, metadata, tools, functions, modules)
        except (yaml.YAMLError, OSError) as exc:
            errors.append((f"agents/{adir.name}", str(exc)))

    for kit_json in sorted(root.glob("starter-kits/*/kit.json")):
        try:
            kits[kit_json.parent.name] = Kit(kit_json.parent.name, kit_json.parent,
                                             json.loads(kit_json.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError) as exc:
            errors.append((f"starter-kits/{kit_json.parent.name}", str(exc)))

    return Catalog(root, commit or _git_commit(root), agents, kits, errors)

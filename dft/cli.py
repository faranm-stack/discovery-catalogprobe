"""dft command line.

  dft static  --catalog PATH                  offline F0 checks (safe anywhere, no credentials)
  dft plan    --catalog PATH --agents/--kits  build test cases; optionally export PR #100 datasets
  dft live    --catalog PATH ... --yes        run cases against a Discovery project (costs money)
  dft diff    BASE.json NEW.json              regression / back-compat report
  dft report  RESULTS.json                    Markdown report
"""
from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
from pathlib import Path

from .catalog import Catalog, load_catalog
from .findings import log_event
from .plan import TestCase, build_plan, export_pr100
from .results import diff, make_results, read_json, regressions, render_diff_markdown, render_markdown, write_json
from .static_checks import load_deprecations, run_static

HERE = Path(__file__).resolve().parent.parent


def _csv(value: str | None) -> list[str] | None:
    if value is None:
        return None
    if value.strip().lower() == "all":
        return None
    return [v.strip().removeprefix("agents/").removeprefix("starter-kits/").strip("/")
            for v in value.split(",") if v.strip()]


def changed_subjects(catalog: Catalog, base_ref: str) -> tuple[set[str], set[str]]:
    """Agents/kits touched since base_ref, plus kits that reference a touched agent."""
    out = subprocess.run(["git", "-C", str(catalog.root), "diff", "--name-only", f"{base_ref}...HEAD"],
                         capture_output=True, text=True, check=True)
    agents, kits = set(), set()
    for path in out.stdout.splitlines():
        parts = path.split("/")
        if len(parts) > 1 and parts[0] == "agents":
            agents.add(parts[1])
        elif len(parts) > 1 and parts[0] == "starter-kits":
            kits.add(parts[1])
    for kit in catalog.kits.values():
        if any(str(r.get("ref", "")).removeprefix("agents/") in agents for r in kit.agent_refs):
            kits.add(kit.name)
    return agents & set(catalog.agents), kits & set(catalog.kits)


def _scope(catalog: Catalog, args: argparse.Namespace) -> tuple[list[str], list[str], str]:
    if getattr(args, "changed_from", None):
        agents, kits = changed_subjects(catalog, args.changed_from)
        return sorted(agents), sorted(kits), f"changed since {args.changed_from}"
    agents = _csv(args.agents)
    kits = _csv(args.kits)
    a = sorted(catalog.agents) if agents is None else agents
    k = sorted(catalog.kits) if kits is None else kits
    if args.agents is not None and args.kits is None and getattr(args, "cmd", "") != "static":
        k = []  # asking for specific agents only
    if args.kits is not None and args.agents is None and getattr(args, "cmd", "") != "static":
        a = []
    return a, k, f"agents={args.agents or 'all'} kits={args.kits or 'all'}"


def _add_scope(p: argparse.ArgumentParser) -> None:
    p.add_argument("--catalog", required=True, help="path to a microsoft/discovery checkout")
    p.add_argument("--agents", help="comma list of agent folder names, or 'all'")
    p.add_argument("--kits", help="comma list of starter-kit folder names, or 'all'")
    p.add_argument("--changed-from", help="only agents/kits changed since this git ref (e.g. origin/main)")
    p.add_argument("--commit", help="override the catalog commit recorded in results")


def cmd_static(args: argparse.Namespace) -> int:
    catalog = load_catalog(args.catalog, args.commit)
    agents, kits, scope = _scope(catalog, args)
    deps = load_deprecations(args.deprecated)
    findings = run_static(catalog, agents=agents, kits=kits, deprecations=deps)
    results = make_results("static", catalog_commit=catalog.commit, service_version=args.service_version or "",
                           findings=[f.to_dict() for f in findings],
                           extra={"scope": scope, "agents_checked": len(agents), "kits_checked": len(kits),
                                  "deprecations_loaded": len(deps)})
    out = Path(args.out)
    write_json(results, out / "static-results.json")
    md = render_markdown(results)
    (out / "static-report.md").write_text(md, encoding="utf-8")
    errors = sum(f.severity == "error" for f in findings)
    warnings = sum(f.severity == "warning" for f in findings)
    log_event("static.done", agents=len(agents), kits=len(kits), errors=errors, warnings=warnings, out=str(out))
    print(md if args.print else f"{len(agents)} agents, {len(kits)} kits: {errors} errors, {warnings} warnings "
          f"-> {out / 'static-report.md'}")
    if args.fail_on == "error":
        return 1 if errors else 0
    if args.fail_on == "warning":
        return 1 if errors or warnings else 0
    return 0


def _plan_from_args(args: argparse.Namespace) -> tuple[Catalog, list[TestCase], str]:
    catalog = load_catalog(args.catalog, args.commit)
    agents, kits, scope = _scope(catalog, args)
    plan = build_plan(catalog, agents=agents, kits=kits, smoke_dir=args.smoke_dir,
                      include_auto=not args.no_auto)
    return catalog, plan, scope


def cmd_plan(args: argparse.Namespace) -> int:
    catalog, plan, scope = _plan_from_args(args)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"catalog_commit": catalog.commit, "scope": scope,
                               "cases": [c.to_dict() for c in plan]}, indent=2) + "\n", encoding="utf-8")
    print(f"{len(plan)} case(s) -> {out}")
    for c in plan:
        route = f" [{c.routing}]" if c.routing else ""
        print(f"  {c.id:34s} -> {c.agent:22s}{route:10s} {c.origin:8s} tool={'yes' if c.requires_tool else 'no '} "
              f"{c.prompt[:60]}")
    if args.emit_pr100:
        for path in export_pr100(plan, args.emit_pr100):
            print(f"  PR #100 dataset -> {path}")
    return 0


def cmd_live(args: argparse.Namespace) -> int:
    from .runner import load_agent_map, load_pr100_client, run_live

    if args.plan:
        data = json.loads(Path(args.plan).read_text(encoding="utf-8"))
        plan = [TestCase(**c) for c in data["cases"]]
        commit = data.get("catalog_commit", "unknown")
        scope = data.get("scope", args.plan)
    else:
        catalog, plan, scope = _plan_from_args(args)
        commit = catalog.commit
    if args.max_cases:
        plan = plan[: args.max_cases]
    if not plan:
        print("Nothing to run.")
        return 0
    print(f"{len(plan)} case(s) against project '{args.project}' at {args.workspace_api_url}")
    if not args.yes:
        print("Live runs invoke real agents and compute (model tokens + node time). Re-run with --yes to proceed.")
        return 2
    out = Path(args.out)
    client = load_pr100_client(args.client_dir, args.workspace_api_url)
    results = asyncio.run(run_live(plan, client, project=args.project, out_dir=out,
                                   agent_map=load_agent_map(args.agent_map), concurrency=args.concurrency,
                                   timeout_s=args.timeout))
    data = make_results("live", catalog_commit=commit, service_version=args.service_version or "",
                        cases=[r.to_dict() for r in results],
                        extra={"scope": scope, "project": args.project})
    write_json(data, out / "live-results.json")
    md = render_markdown(data)
    (out / "live-report.md").write_text(md, encoding="utf-8")
    print(md)
    return 1 if any(r.status == "FAIL" for r in results) and args.fail_on == "error" else 0


def cmd_diff(args: argparse.Namespace) -> int:
    d = diff(read_json(args.base), read_json(args.new))
    md = render_diff_markdown(d)
    if args.out:
        Path(args.out).write_text(md, encoding="utf-8")
    print(md)
    return 1 if regressions(d) else 0


def cmd_report(args: argparse.Namespace) -> int:
    md = render_markdown(read_json(args.results), show_warnings=not args.errors_only)
    if args.out:
        Path(args.out).write_text(md, encoding="utf-8")
    print(md)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="dft", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("static", help="offline F0 checks")
    _add_scope(s)
    s.add_argument("--deprecated", default=str(HERE / "config" / "deprecated.yaml"))
    s.add_argument("--service-version", default="")
    s.add_argument("--out", default="out")
    s.add_argument("--fail-on", choices=["error", "warning", "none"], default="error")
    s.add_argument("--print", action="store_true", help="print the Markdown report")
    s.set_defaults(fn=cmd_static)

    pl = sub.add_parser("plan", help="build functional test cases")
    _add_scope(pl)
    pl.add_argument("--smoke-dir", default=str(HERE / "smoke"))
    pl.add_argument("--no-auto", action="store_true", help="skip auto-generated prompts")
    pl.add_argument("--out", default="out/plan.json")
    pl.add_argument("--emit-pr100", help="also write PR #100 <suite>-evaluators.json datasets here")
    pl.set_defaults(fn=cmd_plan)

    lv = sub.add_parser("live", help="run cases against deployed agents")
    _add_scope(lv)
    lv.add_argument("--plan", help="plan.json from `dft plan` (otherwise built from --agents/--kits)")
    lv.add_argument("--smoke-dir", default=str(HERE / "smoke"))
    lv.add_argument("--no-auto", action="store_true")
    lv.add_argument("--workspace-api-url", required=True)
    lv.add_argument("--project", required=True)
    lv.add_argument("--client-dir", required=True,
                    help="folder with PR #100's discovery_client.py (utilities/agent-evaluation/evaluators)")
    lv.add_argument("--agent-map", help="file of 'catalog-name = deployed-name' lines")
    lv.add_argument("--service-version", default="")
    lv.add_argument("--concurrency", type=int, default=2)
    lv.add_argument("--timeout", type=int, default=900)
    lv.add_argument("--max-cases", type=int, default=0)
    lv.add_argument("--out", default="out/live")
    lv.add_argument("--fail-on", choices=["error", "none"], default="error")
    lv.add_argument("--yes", action="store_true", help="confirm a paid live run")
    lv.set_defaults(fn=cmd_live)

    d = sub.add_parser("diff", help="compare two results files")
    d.add_argument("base")
    d.add_argument("new")
    d.add_argument("--out")
    d.set_defaults(fn=cmd_diff)

    r = sub.add_parser("report", help="render a results file as Markdown")
    r.add_argument("results")
    r.add_argument("--out")
    r.add_argument("--errors-only", action="store_true")
    r.set_defaults(fn=cmd_report)
    return p


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")  # Windows consoles can't encode every report character
    args = build_parser().parse_args(argv)
    try:
        return args.fn(args)
    except (FileNotFoundError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

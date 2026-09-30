"""Results files, baseline diffs (regression / back-compat) and Markdown reports."""
from __future__ import annotations

import json
import time
from collections import Counter
from pathlib import Path
from typing import Any

from . import __version__


def make_results(kind: str, *, catalog_commit: str, service_version: str = "", findings: list[dict] | None = None,
                 cases: list[dict] | None = None, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "meta": {"kind": kind, "catalog_commit": catalog_commit, "service_version": service_version,
                 "created_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                 "dft_version": __version__, **(extra or {})},
        "findings": findings or [],
        "cases": cases or [],
    }


def write_json(data: dict[str, Any], path: str | Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, default=str) + "\n", encoding="utf-8")
    return path


def read_json(path: str | Path) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _fkey(f: dict[str, Any]) -> str:
    ev = f.get("evidence") or {}
    return f"{f['check']}|{f['subject']}|{ev.get('case', '')}|{ev.get('key', '')}"


def all_findings(run: dict[str, Any]) -> list[dict[str, Any]]:
    """Static findings plus the findings attached to each live case."""
    return list(run.get("findings", [])) + [f for c in run.get("cases", []) for f in c.get("findings", [])]


def diff(base: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
    """What changed between two runs: only this goes in the regression report."""
    def keyed(run: dict[str, Any]) -> dict[str, dict]:
        return {_fkey(f): f for f in all_findings(run) if f.get("severity") in ("error", "warning")}

    b, n = keyed(base), keyed(new)
    bc = {c["case_id"]: c for c in base.get("cases", [])}
    nc = {c["case_id"]: c for c in new.get("cases", [])}
    rank = {"PASS": 0, "WARN": 1, "FAIL": 2}
    status_changes = []
    for cid in sorted(set(bc) & set(nc)):
        if bc[cid]["status"] != nc[cid]["status"]:
            worse = rank.get(nc[cid]["status"], 2) > rank.get(bc[cid]["status"], 2)
            status_changes.append({"case_id": cid, "from": bc[cid]["status"], "to": nc[cid]["status"],
                                   "regression": worse})
    return {
        "base": base.get("meta", {}), "new": new.get("meta", {}),
        "new_findings": [n[k] for k in sorted(set(n) - set(b))],
        "fixed_findings": [b[k] for k in sorted(set(b) - set(n))],
        "unchanged": len(set(b) & set(n)),
        "case_status_changes": status_changes,
        "cases_added": sorted(set(nc) - set(bc)),
        "cases_removed": sorted(set(bc) - set(nc)),
    }


def regressions(d: dict[str, Any]) -> int:
    return (sum(1 for f in d["new_findings"] if f["severity"] == "error")
            + sum(1 for c in d["case_status_changes"] if c["regression"]))


def _loc(f: dict[str, Any], repo_url: str, commit: str) -> str:
    rel = (f.get("path") or "").replace("\\", "/")
    if not rel.startswith(("agents/", "starter-kits/")):
        return ""
    line = f.get("line")
    return f"[{rel}{':' + str(line) if line else ''}]({repo_url}/blob/{commit}/{rel}{'#L' + str(line) if line else ''})"


def render_markdown(results: dict[str, Any], *, repo_url: str = "https://github.com/microsoft/discovery",
                    show_warnings: bool = True) -> str:
    meta = results["meta"]
    commit = meta.get("catalog_commit", "main")
    findings = all_findings(results)
    cases = results.get("cases", [])
    sev = Counter(f["severity"] for f in findings)
    lines = [f"# Discovery functional test report ({meta['kind']})", "",
             f"- Catalog commit: `{commit[:12]}`" + (f" · Service version: `{meta['service_version']}`"
                                                     if meta.get("service_version") else ""),
             f"- Created: {meta['created_utc']} · dft {meta['dft_version']}"]
    if meta.get("scope"):
        lines.append(f"- Scope: {meta['scope']}")
    lines.append("")
    if cases:
        st = Counter(c["status"] for c in cases)
        lines += [f"**Cases:** {st.get('PASS', 0)} pass · {st.get('WARN', 0)} warn · {st.get('FAIL', 0)} fail "
                  f"(of {len(cases)})", "",
                  "| Case | Agent | Origin | Status | Own tool calls | Categories | Latency (s) |",
                  "|---|---|---|---|---|---|---|"]
        for c in sorted(cases, key=lambda c: ({"FAIL": 0, "WARN": 1, "PASS": 2}[c["status"]], c["case_id"])):
            cats = ", ".join(sorted({f["category"] for f in c["findings"]})) or "-"
            lines.append(f"| {c['case_id']} | {c['agent']} | {c['origin']} | {c['status']} | "
                         f"{c['own_tool_calls']} | {cats} | {c.get('latency_s') or '-'} |")
        lines.append("")
    lines += [f"**Findings:** {sev.get('error', 0)} errors · {sev.get('warning', 0)} warnings", ""]
    errors = [f for f in findings if f["severity"] == "error"]
    if errors:
        lines += ["## Errors (will break at runtime)", "", "| Subject | Check | Category | Finding | Where |",
                  "|---|---|---|---|---|"]
        for f in sorted(errors, key=lambda f: (f["subject"], f["check"])):
            msg = f["message"].replace("|", "\\|")
            lines.append(f"| {f['subject']} | {f['check']} | {f['category']} | {msg} | {_loc(f, repo_url, commit)} |")
        lines.append("")
    warns = [f for f in findings if f["severity"] == "warning"]
    if warns and show_warnings:
        lines += ["<details><summary>Warnings ({})</summary>".format(len(warns)), "",
                  "| Subject | Check | Finding | Where |", "|---|---|---|---|"]
        for f in sorted(warns, key=lambda f: (f["subject"], f["check"])):
            msg = f["message"].replace("|", "\\|")
            lines.append(f"| {f['subject']} | {f['check']} | {msg} | {_loc(f, repo_url, commit)} |")
        lines += ["", "</details>", ""]
    return "\n".join(lines) + "\n"


def render_diff_markdown(d: dict[str, Any]) -> str:
    lines = ["# Regression diff", "",
             f"- Base: catalog `{d['base'].get('catalog_commit', '?')[:12]}` · service "
             f"`{d['base'].get('service_version') or '?'}` · {d['base'].get('created_utc', '')}",
             f"- New:  catalog `{d['new'].get('catalog_commit', '?')[:12]}` · service "
             f"`{d['new'].get('service_version') or '?'}` · {d['new'].get('created_utc', '')}",
             f"- **Regressions: {regressions(d)}** · new findings {len(d['new_findings'])} · fixed "
             f"{len(d['fixed_findings'])} · unchanged {d['unchanged']}", ""]
    if d["case_status_changes"]:
        lines += ["| Case | From | To |", "|---|---|---|"]
        lines += [f"| {c['case_id']} | {c['from']} | {c['to']}{' ⚠' if c['regression'] else ''} |"
                  for c in d["case_status_changes"]]
        lines.append("")
    for title, key in (("New findings", "new_findings"), ("Fixed", "fixed_findings")):
        if d[key]:
            lines += [f"## {title}", ""]
            lines += [f"- **{f['severity']}** {f['subject']} ({f['check']}): {f['message']}" for f in d[key]]
            lines.append("")
    return "\n".join(lines) + "\n"

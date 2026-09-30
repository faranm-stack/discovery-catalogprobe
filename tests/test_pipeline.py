import asyncio
import json
from pathlib import Path

from dft.catalog import load_catalog
from dft.cli import main
from dft.plan import build_plan, export_pr100
from dft.results import diff, make_results, regressions
from dft.runner import load_agent_map, run_live


class FakeClient:
    """Stands in for PR #100's DiscoveryAgentClient."""

    def __init__(self, behaviour):
        self.behaviour = behaviour
        self.investigations = []
        self.closed = False

    async def create_investigation(self, project, investigation, **kw):
        self.investigations.append((project, investigation))

    async def invoke(self, project, investigation, agent, query, **kw):
        b = self.behaviour[agent]
        if isinstance(b, BaseException):
            raise b
        return b, f"conv-{agent}"

    async def aclose(self):
        self.closed = True


def _ok(text, tool):
    return {"id": "r1", "status": "completed", "output": [
        {"type": "function_call", "call_id": "1", "name": tool, "arguments": "{}"},
        {"type": "function_call_output", "call_id": "1", "output": '{"status": "ok"}'},
        {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": text}]}]}


def test_plan_sources(fake_catalog: Path, tmp_path: Path):
    catalog = load_catalog(fake_catalog, "test")
    smoke = tmp_path / "smoke"
    smoke.mkdir()
    (smoke / "good.yaml").write_text("cases:\n  - id: c1\n    prompt: hi\n    expect: [x]\n")
    plan = build_plan(catalog, agents=["good", "bad"], kits=["demo-kit"], smoke_dir=smoke)
    origins = {c.id: c.origin for c in plan}
    assert origins == {"good#c1": "curated", "bad#auto": "auto", "demo-kit#sp-1": "kit", "demo-kit#sp-2": "kit"}
    kit_case = next(c for c in plan if c.id == "demo-kit#sp-1")
    assert kit_case.agent == "good" and "pa" in kit_case.expect
    written = export_pr100(plan, tmp_path / "pr100")
    doc = json.loads((tmp_path / "pr100" / "good" / "dft-smoke-evaluators.json").read_text())
    assert len(written) == 2 and doc["evaluators"] and {r["_case_id"] for r in doc["data"]} >= {"good#c1"}


def test_kit_prompt_routing(fake_catalog: Path):
    from dft.plan import route_kit_prompt

    catalog = load_catalog(fake_catalog, "test")
    assert route_kit_prompt("Ask the good agent", ["bad", "good"], catalog, "bad") == ("good", "named")
    assert route_kit_prompt("Run it at 300 K.", ["good", "bad"], catalog, "good") == ("good", "primary")


def test_kit_route_override(fake_catalog: Path, tmp_path: Path):
    catalog = load_catalog(fake_catalog, "test")
    (tmp_path / "kits").mkdir()
    (tmp_path / "kits" / "demo-kit.yaml").write_text("routes:\n  sp-1: bad\n")
    plan = build_plan(catalog, kits=["demo-kit"], smoke_dir=tmp_path)
    sp1 = next(c for c in plan if c.id == "demo-kit#sp-1")
    assert (sp1.agent, sp1.routing) == ("bad", "curated")


def test_run_live_with_fake_client(fake_catalog: Path, tmp_path: Path):
    catalog = load_catalog(fake_catalog, "test")
    plan = build_plan(catalog, agents=["good", "bad"], kits=[], smoke_dir=tmp_path)
    client = FakeClient({"Good-Deployed": _ok("done", "good-run"), "bad": SystemExit("Timed out after 1s")})
    results = asyncio.run(run_live(plan, client, project="P", out_dir=tmp_path / "live",
                                   agent_map={"good": "Good-Deployed"}, run_id="t1"))
    by_id = {r.case_id: r for r in results}
    assert by_id["good#auto"].status == "PASS"
    assert by_id["bad#auto"].status == "FAIL"
    assert by_id["bad#auto"].findings[0]["category"] == "timeout"
    assert client.investigations == [("P", "dft-t1")] and client.closed
    assert (tmp_path / "live" / "raw" / "good_auto.json").exists()


def test_investigation_failure_is_reported_not_crashing(fake_catalog: Path, tmp_path: Path):
    import pytest

    class NoProject(FakeClient):
        async def create_investigation(self, project, investigation, **kw):
            raise SystemExit("HTTP 404 on PUT .../projects/Nope/investigations/x")

    catalog = load_catalog(fake_catalog, "test")
    plan = build_plan(catalog, agents=["good"], kits=[], smoke_dir=tmp_path)
    client = NoProject({})
    with pytest.raises(RuntimeError, match="could not create investigation"):
        asyncio.run(run_live(plan, client, project="Nope", out_dir=tmp_path / "o"))
    assert client.closed


def test_diff_flags_regressions_only():
    f_old = {"check": "F0.calls", "severity": "error", "category": "definition", "subject": "agents/a",
             "message": "m", "evidence": {"key": "k1"}}
    f_new = dict(f_old, evidence={"key": "k2"})
    base = make_results("static", catalog_commit="aaa", findings=[f_old],
                        cases=[{"case_id": "c", "status": "PASS", "findings": []}])
    new = make_results("static", catalog_commit="bbb", findings=[f_new],
                       cases=[{"case_id": "c", "status": "FAIL", "findings": []}])
    d = diff(base, new)
    assert [f["evidence"]["key"] for f in d["new_findings"]] == ["k2"]
    assert [f["evidence"]["key"] for f in d["fixed_findings"]] == ["k1"]
    assert regressions(d) == 2
    assert regressions(diff(base, base)) == 0


def test_agent_map(tmp_path: Path):
    p = tmp_path / "m.map"
    p.write_text("# comment\ncore-python-agent = CorePythonAgent\n\nxyce=sim-xyce  # inline\n")
    assert load_agent_map(p) == {"core-python-agent": "CorePythonAgent", "xyce": "sim-xyce"}


def test_cli_static_exit_codes(fake_catalog: Path, tmp_path: Path):
    assert main(["static", "--catalog", str(fake_catalog), "--agents", "good", "--kits", "",
                 "--out", str(tmp_path / "a")]) == 0
    assert main(["static", "--catalog", str(fake_catalog), "--agents", "bad",
                 "--out", str(tmp_path / "b")]) == 1
    data = json.loads((tmp_path / "b" / "static-results.json").read_text())
    assert data["meta"]["kind"] == "static" and data["findings"]
    assert (tmp_path / "b" / "static-report.md").read_text().startswith("# Discovery functional test report")


def test_cli_live_requires_confirmation(fake_catalog: Path, tmp_path: Path):
    rc = main(["live", "--catalog", str(fake_catalog), "--agents", "good", "--workspace-api-url", "https://x",
               "--project", "P", "--client-dir", str(tmp_path), "--out", str(tmp_path / "o")])
    assert rc == 2

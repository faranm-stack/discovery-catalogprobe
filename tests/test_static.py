from pathlib import Path

from dft.catalog import load_catalog
from dft.static_checks import Deprecation, run_static


def _keys(findings, subject):
    return {(f.check, f.severity, f.evidence.get("key")) for f in findings if f.subject == subject}


def test_good_agent_is_clean(fake_catalog: Path):
    findings = run_static(load_catalog(fake_catalog, "test"), agents=["good"], kits=[])
    assert findings == []


def test_bad_agent_findings(fake_catalog: Path):
    catalog = load_catalog(fake_catalog, "test")
    keys = _keys(run_static(catalog, agents=["bad"], kits=[]), "agents/bad")
    assert ("F0.actions", "error", "missing-action:simulate") in keys
    assert ("F0.actions", "error", "unknown-param:run.temprature") in keys
    assert ("F0.imports", "error", "missing-name:bad_utils.vanished_function") in keys
    assert ("F0.imports", "error", "missing-name:bad_utils.old_helper") in keys
    assert ("F0.imports", "error", "missing-module:missing_utils") in keys
    assert ("F0.calls", "error", "bad-call:plot_thing:unknown keyword(s) output_path") in keys
    assert ("F0.calls", "warning", "doc-sig:plot_thing") in keys
    assert ("F0.bindings", "error", "unbound-placeholder") in keys
    assert ("F0.model", "warning", "hardcoded-model") in keys


def test_bad_call_points_at_the_offending_line(fake_catalog: Path):
    findings = run_static(load_catalog(fake_catalog, "test"), agents=["bad"], kits=[])
    call = next(f for f in findings if f.evidence.get("key", "").startswith("bad-call:"))
    line = (fake_catalog / "agents/bad/agent.yaml").read_text().splitlines()[call.line - 1]
    assert "output_path=" in line


def test_deprecations(fake_catalog: Path):
    deps = [Deprecation(pattern=r"\bPreviewThingV1\b", reason="removed", replacement="PreviewThing")]
    findings = run_static(load_catalog(fake_catalog, "test"), agents=["bad", "good"], kits=[], deprecations=deps)
    hits = [f for f in findings if f.check == "F0.deprecated"]
    assert [f.subject for f in hits] == ["agents/bad"]
    assert hits[0].line is not None


def test_kit_checks(fake_catalog: Path):
    keys = _keys(run_static(load_catalog(fake_catalog, "test"), agents=[], kits=["demo-kit"]),
                 "starter-kits/demo-kit")
    assert ("F0.kit", "error", "missing-agent:ghost") in keys
    assert ("F0.kit", "warning", "no-expected:sp-2") in keys

from dft.live_checks import classify_exception, detect_refusal, evaluate
from dft.plan import TestCase, expected_terms


def _case(**kw):
    base = dict(id="a#1", subject="agents/a", agent="a", prompt="p", origin="curated", requires_tool=True,
                expect=[], tool_names=["cantera", "equilibrate"])
    base.update(kw)
    return TestCase(**base)


def _resp(text="", calls=(), status="completed"):
    output = []
    for i, (name, out) in enumerate(calls):
        output.append({"type": "function_call", "call_id": f"c{i}", "name": name, "arguments": "{}"})
        output.append({"type": "function_call_output", "call_id": f"c{i}", "output": out})
    if text:
        output.append({"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": text}]})
    return {"id": "resp_1", "status": status, "output": output}


def _checks(result):
    return {(f["check"], f["severity"], f["category"]) for f in result.findings}


def test_pass_when_own_tool_ran_and_answer_is_complete():
    r = evaluate(_case(expect=["co2", "h2o"]),
                 _resp("CO2 mole fraction 0.095, H2O 0.19", [("cantera-equilibrate", '{"status": "ok"}')]))
    assert r.status == "PASS" and r.own_tool_calls == 1


def test_tool_not_used_is_flagged_even_if_answer_looks_right():
    r = evaluate(_case(), _resp("Aspirin is 180.159 g/mol", [("GetResourceContext", "{}")]))
    assert ("F3.tool-used", "error", "tool-not-used") in _checks(r)
    assert r.status == "FAIL"


def test_refusal_is_capability_failure():
    r = evaluate(_case(requires_tool=False), _resp("I don't know how to run that simulation."))
    assert ("F2.capability", "error", "capability") in _checks(r)


def test_weak_refusal_only_warns():
    assert detect_refusal("I cannot find the file you mentioned, so I used defaults.")[0] == "warning"
    assert detect_refusal("Here are the results. The run completed.") is None


def test_empty_answer():
    assert ("F2.capability", "error", "capability") in _checks(evaluate(_case(requires_tool=False), _resp("")))


def test_tool_error_vs_platform_error():
    r = evaluate(_case(), _resp("It failed.", [("cantera-equilibrate", "Traceback (most recent call last): TypeError")]))
    assert ("F3.tool-ok", "error", "tool-error") in _checks(r)
    r = evaluate(_case(), _resp("It failed.", [("cantera-equilibrate", '{"status": "failed", "error": "ErrImagePull"}')]))
    assert ("F3.tool-ok", "error", "platform") in _checks(r)


def test_expected_terms_from_kit_expected_output():
    assert expected_terms("Pressure drop [Pa], inlet/outlet pressures.") == ["pa", "pressure drop", "inlet",
                                                                             "outlet pressures"]
    r = evaluate(_case(requires_tool=False, expect=["pa", "pressure drop", "inlet", "outlet pressures"]),
                 _resp("The run finished."))
    assert ("F4.output", "warning", "wrong-output") in _checks(r)
    r = evaluate(_case(requires_tool=False, expect=["pa", "pressure drop", "inlet", "outlet pressures"]),
                 _resp("Pressure drop: 1234 Pa. Inlet 101325 Pa, outlet pressures 100091 Pa."))
    assert r.status == "PASS"


def test_invocation_errors_are_categorized():
    assert classify_exception(SystemExit("Timed out after 900s waiting"))[0] == "timeout"
    assert classify_exception(RuntimeError("HTTP 404: agent 'x' not found"))[0] == "deploy"
    assert classify_exception(RuntimeError("HTTP 403 Forbidden"))[0] == "platform"
    # exact message format raised by PR #100's DiscoveryAgentClient._request
    hint = "(If 401/403, pass a token. If 404, check endpoint / project / investigation / api-version.)"
    assert classify_exception(SystemExit(f"HTTP 404 on POST https://w/conversations/c/openai/v1/responses\n{{}}\n{hint}"))[0] == "deploy"
    assert classify_exception(SystemExit(f"HTTP 401 on POST https://w/conversations\n{{}}\n{hint}"))[0] == "platform"
    r = evaluate(_case(), None, error=SystemExit("Timed out after 5s"))
    assert ("F1.invoke", "error", "timeout") in _checks(r)


def test_failed_response_status():
    r = evaluate(_case(requires_tool=False), _resp("partial", status="failed"))
    assert ("F1.invoke", "error", "platform") in _checks(r)

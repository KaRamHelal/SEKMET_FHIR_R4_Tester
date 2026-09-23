"""TestScript engine, TestReport and run export/replay (loopback)."""
from sekmet.fhir.validation import has_errors, structural_issues
from sekmet.scenarios.runner import ScenarioRunner, load_run
from sekmet.scenarios.testscript import TestScriptRunner, build_test_report, export_run

OP = "http://terminology.hl7.org/CodeSystem/testscript-operation-codes"

SCRIPT = {
    "resourceType": "TestScript", "id": "t1", "url": "urn:t1", "name": "T1", "status": "draft",
    "contained": [{"resourceType": "Patient", "id": "pat", "identifier": [{"system": "urn:ts", "value": "TS-1"}],
                   "name": [{"family": "Scripted", "given": ["Tess"]}], "gender": "female"}],
    "fixture": [{"id": "fx-pat", "autocreate": False, "autodelete": False, "resource": {"reference": "#pat"}}],
    "variable": [{"name": "loc", "headerField": "Location", "sourceId": "created"},
                 {"name": "pid", "expression": "Patient.id", "sourceId": "created"},
                 {"name": "family", "path": "Patient/name/family", "sourceId": "fx-pat"}],
    "setup": {"action": [
        {"operation": {"type": {"system": OP, "code": "create"}, "resource": "Patient", "sourceId": "fx-pat",
                       "responseId": "created", "contentType": "xml", "accept": "json"}},
        {"assert": {"response": "created"}},
        {"assert": {"headerField": "Location", "operator": "notEmpty"}}]},
    "test": [{"name": "read-and-search", "action": [
        {"operation": {"type": {"system": OP, "code": "read"}, "resource": "Patient", "targetId": "fx-pat",
                       "accept": "xml"}},
        {"assert": {"response": "okay"}},
        {"assert": {"contentType": "xml"}},
        {"assert": {"resource": "Patient"}},
        {"assert": {"path": "fhir:Patient/fhir:name/fhir:family/@value", "value": "${family}"}},
        {"assert": {"compareToSourceId": "fx-pat", "compareToSourceExpression": "Patient.name.given.first()",
                    "operator": "equals"}},
        {"assert": {"minimumId": "fx-pat"}},
        {"assert": {"validateProfileId": "p"}},
        {"operation": {"type": {"system": OP, "code": "search"}, "resource": "Patient",
                       "params": "?identifier=urn:ts|TS-1"}},
        {"assert": {"expression": "Bundle.total >= 1"}},
        {"assert": {"requestURL": "identifier="}},
        {"assert": {"navigationLinks": True}},
        {"operation": {"type": {"system": OP, "code": "read"}, "url": "${loc}"}},
        {"assert": {"response": "okay", "warningOnly": True}},
        {"assert": {"expression": "Patient.gender", "value": "male", "warningOnly": True}},
    ]}],
    "teardown": {"action": [{"operation": {"type": {"system": OP, "code": "delete"}, "resource": "Patient",
                                           "params": "/${pid}"}}]},
}


def test_testscript_engine_features(live):
    r = TestScriptRunner(live, ["self"], fixture_base_url=None).run(SCRIPT)
    by_status = {s.name: s.status for s in r.steps}
    bad = {n: s.message for n, s in ((s.name, s) for s in r.steps) if s.status in ("failed", "error")}
    assert not bad, bad
    assert r.status == "warning"  # the deliberate warningOnly gender check
    assert list(by_status.values()).count("warning") == 1
    rep = build_test_report(r, SCRIPT["url"])
    assert not has_errors(structural_issues(rep)), structural_issues(rep)
    assert rep["result"] == "pass" and rep["setup"]["action"] and rep["test"][0]["name"] == "read-and-search"


def test_failed_assert_and_missing_variable(live):
    bad = {**SCRIPT, "test": [{"name": "t", "action": [
        {"operation": {"type": {"system": OP, "code": "read"}, "resource": "Patient", "params": "/nope"}},
        {"assert": {"response": "okay"}},
        {"operation": {"type": {"system": OP, "code": "read"}, "resource": "Patient", "params": "/${undefinedVar}"}}]}]}
    r = TestScriptRunner(live, ["self"], fixture_base_url=None).run(bad)
    statuses = [s.status for s in r.steps if s.name.startswith("t:")]
    assert statuses[:2] == ["passed", "failed"] and statuses[2] == "skipped"
    assert build_test_report(r)["result"] == "fail"


def test_export_run_and_replay(live):
    run = ScenarioRunner(live, "self").run("lab_order_to_result")
    assert run.status == "passed"
    ts = export_run(live, load_run(live.store, run.run_id), live.settings.base_url)
    assert not has_errors(structural_issues(ts)), structural_issues(ts)[:3]
    assert any(v["name"].startswith("id") for v in ts["variable"])
    replay = TestScriptRunner(live, ["self"], fixture_base_url=None).run(ts)
    failures = [f"{s.name}: {s.message}" for s in replay.steps if s.status in ("failed", "error")]
    assert not failures, failures[:5]

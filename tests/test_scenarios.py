import pytest

from sekmet.scenarios.runner import ScenarioRunner, list_scenarios

SCENARIOS = [s["id"] for s in list_scenarios()]


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_library_scenario_passes_on_loopback(live, scenario):
    result = ScenarioRunner(live, "self").run(scenario)
    failures = [f"{s.index}. {s.name}: {s.message}" for s in result.steps if s.status in ("failed", "error")]
    assert result.status == "passed", "\n".join(failures)


def test_failing_expectation_is_reported(live):
    spec = {"name": "neg", "steps": [
        {"name": "wrong status", "request": {"method": "GET", "path": "Patient/does-not-exist"}, "expect": {"status": 200}},
        {"name": "never runs", "request": {"method": "GET", "path": "metadata"}}]}
    r = ScenarioRunner(live, "self").run(spec)
    assert r.status == "failed"
    assert [s.status for s in r.steps] == ["failed", "skipped"]
    assert "404" in r.steps[0].message


def test_simulator_fills_inbound_order(live):
    """A peer placing an order on us (REST) gets results back from the simulator."""
    spec = {"name": "sim", "simulator": True, "steps": [
        {"action": "adt.register_patient", "save": "reg"},
        {"action": "orders.imaging_order", "args": {"patient": "${reg.patient.id}"}, "save": "o"},
        {"wait_for": {"search": {"type": "DiagnosticReport",
                                 "params": {"based-on": "ServiceRequest/${o.service_request.id}"}}, "timeout": 20}},
        {"wait_for": {"search": {"type": "ImagingStudy",
                                 "params": {"basedon": "ServiceRequest/${o.service_request.id}"}}, "timeout": 5}}]}
    r = ScenarioRunner(live, "self").run(spec)
    assert r.status == "passed", [s.message for s in r.steps]


def test_wait_for_fhirpath_is_applied(live):
    """A wait_for fhirpath that never matches must time out (it used to be ignored)."""
    spec = {"name": "wait", "steps": [
        {"action": "adt.register_patient", "save": "reg"},
        {"wait_for": {"search": {"type": "Patient", "params": {"_id": "${reg.patient.id}"}},
                      "fhirpath": "Patient.gender = 'no-such-gender'", "timeout": 1}}]}
    r = ScenarioRunner(live, "self").run(spec)
    assert r.steps[1].status == "failed" and "Timed out" in r.steps[1].message

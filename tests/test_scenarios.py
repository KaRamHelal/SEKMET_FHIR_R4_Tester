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


def test_backport_topics_advertised_and_status(live):
    import httpx
    base = live.settings.base_url
    cs = httpx.get(f"{base}/metadata").json()
    sub = next(r for r in cs["rest"][0]["resource"] if r["type"] == "Subscription")
    topics = [e["valueCanonical"] for e in sub.get("extension", [])]
    assert "http://sekmet.dev/fhir/SubscriptionTopic/encounter-complete" in topics
    assert any(o["name"] == "status" for o in sub["operation"])
    assert httpx.get(f"{base}/Basic", params={"code": "SubscriptionTopic"}).json()["total"] >= 6


def test_backport_rejects_unknown_topic_and_bad_filter(live):
    import time
    import httpx
    from sekmet.subscriptions.backport import build_subscription
    base = live.settings.base_url
    bad_topic = build_subscription("http://nope/topic", f"{live.settings.root_url}/hooks/self/x")
    s1 = httpx.post(f"{base}/Subscription", json=bad_topic).json()
    bad_filter = build_subscription("http://sekmet.dev/fhir/SubscriptionTopic/encounter-complete",
                                    f"{live.settings.root_url}/hooks/self/x", ["Encounter?status=finished"])
    s2 = httpx.post(f"{base}/Subscription", json=bad_filter).json()
    time.sleep(0.5)
    for s, word in ((s1, "Unknown SubscriptionTopic"), (s2, "not allowed by topic")):
        cur = httpx.get(f"{base}/Subscription/{s['id']}").json()
        assert cur["status"] == "error" and word in cur.get("error", ""), cur


@pytest.fixture(scope="module")
def live_xml(tmp_path_factory):
    """Loopback peer that talks FHIR XML on the wire."""
    from .conftest import free_port, make_settings, start_server
    tmp = tmp_path_factory.mktemp("livexml")
    port = free_port()
    s = make_settings(tmp, port)
    s.peers["self"] = s.peer("self").model_copy(update={"format": "xml"})
    app, server = start_server(s)
    yield app.state.ctx
    server.should_exit = True


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_library_scenario_passes_over_xml(live_xml, scenario):
    result = ScenarioRunner(live_xml, "self").run(scenario)
    failures = [f"{s.index}. {s.name}: {s.message}" for s in result.steps if s.status in ("failed", "error")]
    assert result.status in ("passed", "warning"), "\n".join(failures)
    xml_calls = live_xml.store.query(
        "SELECT COUNT(*) FROM traffic WHERE direction='outbound' AND req_headers LIKE '%application/fhir+xml%'")[0][0]
    assert xml_calls > 0

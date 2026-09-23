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


def test_concurrent_create_then_read_is_consistent(live):
    """Regression: unlocked reads on the shared sqlite connection gave 404s / 500s under parallel load."""
    import concurrent.futures
    import httpx
    base = live.settings.base_url

    def one(i):
        with httpx.Client(timeout=30) as c:
            r = c.post(f"{base}/Patient", json={"resourceType": "Patient", "name": [{"family": f"Conc{i}"}]})
            assert r.status_code == 201, r.text
            pid = r.json()["id"]
            r2 = c.get(f"{base}/Patient/{pid}")
            r3 = c.get(f"{base}/Patient", params={"_id": pid})
            return r2.status_code, r3.json().get("total")

    with concurrent.futures.ThreadPoolExecutor(max_workers=16) as ex:
        results = list(ex.map(one, range(120)))
    assert all(s == 200 and t == 1 for s, t in results), [r for r in results if r != (200, 1)][:5]


def test_load_runner_small(live):
    from sekmet.load.runner import LoadConfig, LoadRunner, endpoint_template
    assert endpoint_template("GET", "http://h/fhir/Patient/1/_history/2", "http://h/fhir") == "GET Patient/{id}/_history/{vid}"
    assert endpoint_template("GET", "http://h/fhir/Encounter?patient=x", "http://h/fhir") == "GET Encounter?search"
    assert endpoint_template("POST", "http://h/fhir/Claim/$submit", "http://h/fhir") == "POST Claim/$submit"
    s = LoadRunner(live, LoadConfig("adt_merge_update", "self", users=4, duration_s=30, iterations=12)).run()
    assert s["iterations"] == 12 and s["iteration_failure_rate_pct"] == 0, s["failures"]
    assert s["server_error_rate_pct"] == 0 and s["requests"] > 0 and s["endpoints"][0]["p95"] > 0


def test_bulk_export_async_flow(live):
    import httpx
    from sekmet.bulk.client import bulk_export
    base = live.settings.base_url
    pid = httpx.post(f"{base}/Patient", json={"resourceType": "Patient", "name": [{"family": "Bulk"}]}).json()["id"]
    httpx.post(f"{base}/Observation", json={"resourceType": "Observation", "status": "final", "code": {"text": "x"},
                                            "subject": {"reference": f"Patient/{pid}"}})
    gid = httpx.post(f"{base}/Group", json={"resourceType": "Group", "type": "person", "actual": True,
                                            "member": [{"entity": {"reference": f"Patient/{pid}"}}]}).json()["id"]
    assert httpx.get(f"{base}/Group/{gid}/$export").status_code == 400  # Prefer: respond-async required
    out = bulk_export(live.peer_client("self"), "group", gid, ["Patient", "Observation"], timeout=30)
    assert out["kickoff_status"] == 202 and out["all_valid"], out
    assert {f["type"]: f["lines"] for f in out["files"]} == {"Patient": 1, "Observation": 1}
    assert out["retry_after_requested"] and out["poll_cap_s"] is None  # followed the server's Retry-After
    # cancel: DELETE on the status URL removes the job
    r = httpx.get(f"{base}/$export", headers={"Prefer": "respond-async"}, params={"_type": "Patient"})
    status = r.headers["content-location"]
    assert httpx.delete(status).status_code == 202 and httpx.get(status).status_code == 404
    assert httpx.get(f"{base}/$export", headers={"Prefer": "respond-async"},
                     params={"_outputFormat": "text/csv"}).status_code == 400

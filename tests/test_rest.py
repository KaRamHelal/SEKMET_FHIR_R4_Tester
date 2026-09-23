PAT = {"resourceType": "Patient", "identifier": [{"system": "urn:t", "value": "1"}],
       "name": [{"family": "Smith", "given": ["Ann"]}], "gender": "female", "birthDate": "1980-05-01"}


def total(c, rtype, **params):
    r = c.get(f"/fhir/{rtype}", params=params)
    assert r.status_code == 200, r.text
    return r.json()["total"]


def test_metadata(client):
    r = client.get("/fhir/metadata")
    assert r.status_code == 200
    cs = r.json()
    assert cs["fhirVersion"] == "4.0.1"
    assert any(res["type"] == "Encounter" for res in cs["rest"][0]["resource"])


def test_crud_versioning_and_conditional(client):
    r = client.post("/fhir/Patient", json=PAT)
    assert r.status_code == 201 and r.headers["etag"] == 'W/"1"' and "/_history/1" in r.headers["location"]
    pid = r.json()["id"]
    r = client.post("/fhir/Patient", json=PAT, headers={"If-None-Exist": "identifier=urn:t|1"})
    assert r.status_code == 200 and r.json()["id"] == pid
    pat = client.get(f"/fhir/Patient/{pid}").json()
    pat["active"] = True
    assert client.put(f"/fhir/Patient/{pid}", json=pat, headers={"If-Match": 'W/"1"'}).status_code == 200
    assert client.put(f"/fhir/Patient/{pid}", json=pat, headers={"If-Match": 'W/"1"'}).status_code == 412
    r = client.get(f"/fhir/Patient/{pid}", headers={"If-None-Match": 'W/"2"'})
    assert r.status_code == 304
    r = client.patch(f"/fhir/Patient/{pid}", json=[{"op": "replace", "path": "/gender", "value": "other"}],
                     headers={"Content-Type": "application/json-patch+json"})
    assert r.status_code == 200 and r.json()["gender"] == "other"
    assert client.get(f"/fhir/Patient/{pid}/_history").json()["total"] == 3
    assert client.get(f"/fhir/Patient/{pid}/_history/1").json()["gender"] == "female"
    assert client.delete(f"/fhir/Patient/{pid}").status_code == 200
    assert client.get(f"/fhir/Patient/{pid}").status_code == 410


def test_validation_and_errors(client):
    r = client.post("/fhir/Patient", json={"resourceType": "Patient", "foo": 1})
    assert r.status_code == 400 and r.json()["resourceType"] == "OperationOutcome"
    assert client.post("/fhir/Patient", json={"resourceType": "Observation"}).status_code == 400
    assert client.get("/fhir/NotAType").status_code == 404
    assert client.get("/fhir/Patient/nope").status_code == 404
    r = client.post("/fhir/Patient", content=b"{bad json", headers={"Content-Type": "application/fhir+json"})
    assert r.status_code == 400
    assert client.get("/fhir/Patient", headers={"Accept": "application/fhir+xml"}).status_code == 200
    assert client.get("/fhir/Patient", headers={"Accept": "text/turtle"}).status_code == 406
    assert client.get("/fhir/Patient", params={"_format": "ttl"}).status_code == 406


def test_search_features(client):
    pid = client.post("/fhir/Patient", json=PAT).json()["id"]
    obs = {"resourceType": "Observation", "status": "final",
           "code": {"coding": [{"system": "http://loinc.org", "code": "718-7", "display": "Hemoglobin"}]},
           "category": [{"coding": [{"system": "http://terminology.hl7.org/CodeSystem/observation-category",
                                     "code": "laboratory"}]}],
           "subject": {"reference": f"Patient/{pid}"}, "effectiveDateTime": "2024-03-01T10:00:00Z",
           "valueQuantity": {"value": 13.2}}
    client.post("/fhir/Observation", json=obs)
    assert total(client, "Patient", name="smi") == 1
    assert total(client, "Patient", **{"name:exact": "smith"}) == 0
    assert total(client, "Patient", **{"name:contains": "mit"}) == 1
    assert total(client, "Patient", birthdate="ge1980") == 1
    assert total(client, "Patient", birthdate="lt1980") == 0
    assert total(client, "Patient", gender="female,male") == 1
    assert total(client, "Patient", **{"gender:not": "female"}) == 0
    assert total(client, "Observation", code="http://loinc.org|718-7") == 1
    assert total(client, "Observation", code="|718-7") == 0
    assert total(client, "Observation", **{"code:text": "hemo"}) == 1
    assert total(client, "Observation", patient=pid) == 1
    assert total(client, "Observation", subject=f"Patient/{pid}") == 1
    assert total(client, "Observation", **{"subject:Patient": pid}) == 1
    assert total(client, "Observation", **{"patient.identifier": "urn:t|1"}) == 1
    assert total(client, "Observation", **{"subject:Patient.name": "smith"}) == 1
    assert total(client, "Patient", **{"_has:Observation:patient:code": "718-7"}) == 1
    assert total(client, "Observation", date="2024-03", **{"value-quantity": "gt13"}) == 1
    assert total(client, "Observation", date="gt2024-03-02") == 0
    assert total(client, "Observation", **{"encounter:missing": "true"}) == 1
    r = client.get("/fhir/Observation", params={"patient": pid, "_include": "Observation:patient"}).json()
    assert [e["search"]["mode"] for e in r["entry"]] == ["match", "include"]
    r = client.get("/fhir/Patient", params={"_id": pid, "_revinclude": "Observation:subject"}).json()
    assert len(r["entry"]) == 2
    r = client.get("/fhir/Patient", params={"_summary": "count"}).json()
    assert r["total"] == 1 and "entry" not in r
    r = client.get("/fhir/Patient", params={"_elements": "name"}).json()
    assert "gender" not in r["entry"][0]["resource"]
    r = client.get("/fhir/Patient", params={"bogus": "1"}).json()
    assert r["entry"][-1]["search"]["mode"] == "outcome"
    assert client.get("/fhir/Patient", params={"bogus": "1"}, headers={"Prefer": "handling=strict"}).status_code == 400
    assert client.get(f"/fhir/Patient/{pid}/Observation").json()["total"] == 1
    assert client.get(f"/fhir/Patient/{pid}/$everything").json()["total"] == 2


def test_sort_and_paging(client):
    for i in range(7):
        client.post("/fhir/Patient", json={"resourceType": "Patient", "name": [{"family": f"P{6 - i}"}]})
    r = client.get("/fhir/Patient", params={"_count": 3, "_sort": "family"}).json()
    assert [e["resource"]["name"][0]["family"] for e in r["entry"]] == ["P0", "P1", "P2"]
    nxt = next(l["url"] for l in r["link"] if l["relation"] == "next")
    r2 = client.get(nxt.replace("http://127.0.0.1:8099", "")).json()
    assert [e["resource"]["name"][0]["family"] for e in r2["entry"]] == ["P3", "P4", "P5"]
    r = client.get("/fhir/Patient", params={"_count": 3, "_sort": "-family"}).json()
    assert r["entry"][0]["resource"]["name"][0]["family"] == "P6"


def test_transaction_and_rollback(client):
    tx = {"resourceType": "Bundle", "type": "transaction", "entry": [
        {"fullUrl": "urn:uuid:p1", "resource": {"resourceType": "Patient", "name": [{"family": "Tx"}]},
         "request": {"method": "POST", "url": "Patient"}},
        {"resource": {"resourceType": "Encounter", "status": "planned",
                      "class": {"system": "http://terminology.hl7.org/CodeSystem/v3-ActCode", "code": "AMB"},
                      "subject": {"reference": "urn:uuid:p1"}},
         "request": {"method": "POST", "url": "Encounter"}},
        {"request": {"method": "GET", "url": "Patient?family=Tx"}}]}
    r = client.post("/fhir", json=tx)
    assert r.status_code == 200, r.text
    entries = r.json()["entry"]
    assert [e["response"]["status"] for e in entries] == ["201 Created", "201 Created", "200 OK"]
    pid = entries[0]["resource"]["id"]
    assert entries[1]["resource"]["subject"]["reference"] == f"Patient/{pid}"
    # conditional reference inside a transaction
    tx2 = {"resourceType": "Bundle", "type": "transaction", "entry": [
        {"resource": {"resourceType": "Encounter", "status": "planned",
                      "class": {"system": "http://terminology.hl7.org/CodeSystem/v3-ActCode", "code": "AMB"},
                      "subject": {"reference": "Patient?family=Tx"}},
         "request": {"method": "POST", "url": "Encounter"}}]}
    r = client.post("/fhir", json=tx2)
    assert r.json()["entry"][0]["resource"]["subject"]["reference"] == f"Patient/{pid}"
    bad = {"resourceType": "Bundle", "type": "transaction", "entry": [
        {"resource": {"resourceType": "Patient", "name": [{"family": "Rollback"}]}, "request": {"method": "POST", "url": "Patient"}},
        {"resource": {"resourceType": "Patient", "bogus": 1}, "request": {"method": "POST", "url": "Patient"}}]}
    assert client.post("/fhir", json=bad).status_code == 400
    assert total(client, "Patient", family="Rollback") == 0
    batch = dict(bad, type="batch")
    r = client.post("/fhir", json=batch).json()
    assert [e["response"]["status"][:3] for e in r["entry"]] == ["201", "400"]


def test_process_message(client):
    msg = {"resourceType": "Bundle", "type": "message", "entry": [
        {"fullUrl": "urn:uuid:h", "resource": {"resourceType": "MessageHeader", "id": "h1",
                                               "eventCoding": {"system": "http://terminology.hl7.org/CodeSystem/v2-0003", "code": "A04"},
                                               "source": {"endpoint": "http://sender"},
                                               "focus": [{"reference": "http://sender/fhir/Patient/abc"}]}},
        {"fullUrl": "http://sender/fhir/Patient/abc", "resource": {"resourceType": "Patient", "id": "abc", "name": [{"family": "Msg"}]}}]}
    r = client.post("/fhir/$process-message", json=msg)
    assert r.status_code == 200
    hdr = r.json()["entry"][0]["resource"]
    assert hdr["response"] == {"identifier": "h1", "code": "ok"}
    assert client.get("/fhir/Patient/abc").json()["name"][0]["family"] == "Msg"
    bad = {"resourceType": "Bundle", "type": "message", "entry": [{"resource": {"resourceType": "Patient"}}]}
    assert client.post("/fhir/$process-message", json=bad).status_code == 400


def test_claim_submit(client):
    pid = client.post("/fhir/Patient", json=PAT).json()["id"]
    claim = {"resourceType": "Claim", "status": "active", "use": "claim",
             "type": {"coding": [{"system": "http://terminology.hl7.org/CodeSystem/claim-type", "code": "professional"}]},
             "patient": {"reference": f"Patient/{pid}"}, "created": "2024-01-01", "provider": {"display": "x"},
             "priority": {"coding": [{"code": "normal"}]}, "insurance": [{"sequence": 1, "focal": True, "coverage": {"display": "c"}}],
             "item": [{"sequence": 1, "productOrService": {"text": "visit"}, "net": {"value": 100, "currency": "USD"}}]}
    r = client.post("/fhir/Claim/$submit", json=claim)
    assert r.status_code == 200 and r.json()["resourceType"] == "ClaimResponse"
    assert r.json()["payment"]["amount"]["value"] == 80


def test_traffic_logged(client):
    client.get("/fhir/metadata")
    rows = client.ctx.store.query("SELECT direction, url, status FROM traffic ORDER BY id DESC LIMIT 1")
    assert rows[0]["url"] == "/fhir/metadata" and rows[0]["status"] == 200


def test_ui_pages_render(client):
    client.post("/fhir/Patient", json=PAT)
    for path in ["/ui", "/ui/resources?type=Patient", "/ui/workflows", "/ui/traffic", "/ui/scenarios", "/ui/peers",
                 "/ui/subscriptions", "/ui/validate", "/ui/simulator", "/ui/testscripts", "/ui/load", "/ui/bulk"]:
        r = client.get(path)
        assert r.status_code == 200, (path, r.text[:500])
    pid = client.get("/fhir/Patient").json()["entry"][0]["resource"]["id"]
    assert client.get(f"/ui/resources/Patient/{pid}").status_code == 200
    r = client.post("/ui/workflows/adt.register_patient", data={"_target": "local"})
    assert r.status_code == 200 and "Done" in r.text


def test_absolute_references_behaviour(tmp_path):
    from fastapi.testclient import TestClient
    from sekmet.main import create_app
    from .conftest import make_settings
    c = TestClient(create_app(make_settings(tmp_path, server_behaviour={"absolute_references": True})))
    pid = c.post("/fhir/Patient", json=PAT).json()["id"]
    obs = {"resourceType": "Observation", "status": "final", "code": {"text": "x"}, "subject": {"reference": f"Patient/{pid}"}}
    r = c.post("/fhir/Observation", json=obs).json()
    assert r["subject"]["reference"] == f"http://127.0.0.1:8099/fhir/Patient/{pid}"
    # stored form stays relative, so search and inbound references keep working
    assert c.get("/fhir/Observation", params={"subject": f"Patient/{pid}"}).json()["total"] == 1
    s = c.get("/fhir/Observation").json()
    assert s["entry"][0]["resource"]["subject"]["reference"].startswith("http://")


XML_PAT = b"""<Patient xmlns="http://hl7.org/fhir"><identifier><system value="urn:x"/><value value="X1"/></identifier>
<name><family value="Xml"/><given value="Ann"/></name><gender value="female"/><birthDate value="1980-02-03"/></Patient>"""


def test_xml_create_read_search_and_errors(client):
    from lxml import etree
    r = client.post("/fhir/Patient", content=XML_PAT, headers={"Content-Type": "application/fhir+xml",
                                                                "Accept": "application/fhir+xml"})
    assert r.status_code == 201 and r.headers["content-type"].startswith("application/fhir+xml")
    root = etree.fromstring(r.content)
    pid = root.find("{http://hl7.org/fhir}id").get("value")
    # JSON view of the same resource
    assert client.get(f"/fhir/Patient/{pid}").json()["name"][0]["family"] == "Xml"
    # _format wins over Accept
    r = client.get(f"/fhir/Patient/{pid}", params={"_format": "xml"})
    assert r.headers["content-type"].startswith("application/fhir+xml") and b"<family value=\"Xml\"/>" in r.content
    r = client.get("/fhir/Patient", params={"family": "xml"}, headers={"Accept": "application/fhir+xml"})
    assert b"<Bundle" in r.content and b'<total value="1"/>' in r.content
    # errors come back as XML OperationOutcome when XML was asked for
    r = client.get("/fhir/Patient/nope", headers={"Accept": "application/fhir+xml"})
    assert r.status_code == 404 and b"<OperationOutcome" in r.content
    r = client.post("/fhir/Patient", content=b"<Patient xmlns='http://hl7.org/fhir'><bogus/></Patient>",
                    headers={"Content-Type": "application/fhir+xml"})
    assert r.status_code == 400
    r = client.post("/fhir/Patient", content=b"<not-xml", headers={"Content-Type": "application/fhir+xml"})
    assert r.status_code == 400
    # XXE is not resolved
    xxe = b'<?xml version="1.0"?><!DOCTYPE p [<!ENTITY x SYSTEM "file:///etc/passwd">]>' \
          b'<Patient xmlns="http://hl7.org/fhir"><name><family value="&x;"/></name></Patient>'
    r = client.post("/fhir/Patient", content=xxe, headers={"Content-Type": "application/fhir+xml"})
    assert b"root:" not in r.content
    assert "xml" in client.get("/fhir/metadata").json()["format"]


def test_cli_version():
    from typer.testing import CliRunner
    from sekmet import __version__
    from sekmet.cli import app
    r = CliRunner().invoke(app, ["--version"])
    assert r.exit_code == 0 and r.output.strip() == f"sekmet-fhir {__version__}"

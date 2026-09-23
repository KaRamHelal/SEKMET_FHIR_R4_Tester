from sekmet.fhir.indexer import date_range, extract, walk


def test_date_precision_ranges():
    lo, hi = date_range("2024")
    assert hi - lo == 366 * 86400  # leap year
    lo, hi = date_range("2024-02")
    assert hi - lo == 29 * 86400
    lo, hi = date_range("2024-02-03")
    assert hi - lo == 86400
    lo, hi = date_range("2024-02-03T10:00:00+02:00")
    assert hi - lo == 1
    assert date_range("2024-02-03T08:00:00Z")[0] == lo
    assert date_range("not a date") is None


def test_walk_filters_contactpoint_system():
    p = {"telecom": [{"system": "phone", "value": "1"}, {"system": "email", "value": "a@b"}]}
    assert [t["value"] for t in walk(p, "telecom[phone]")] == ["1"]


def test_extract_patient_rows():
    p = {"resourceType": "Patient", "id": "x", "identifier": [{"system": "s", "value": "v"}],
         "name": [{"family": "Müller", "given": ["Anna"]}], "birthDate": "1980-01-02", "active": True,
         "managingOrganization": {"reference": "Organization/o1"}}
    rows = {(r.param, r.s, r.sys, r.code, r.rtype, r.rid) for r in extract(p)}
    assert ("identifier", None, "s", "v", None, None) in rows
    assert ("family", "muller", None, "Müller", None, None) in rows
    assert ("active", None, None, "true", None, None) in rows
    assert ("organization", "Organization/o1", None, None, "Organization", "o1") in rows


def test_logical_reference_indexed_for_identifier_modifier():
    o = {"resourceType": "Observation", "id": "o", "status": "final", "code": {"text": "x"},
         "subject": {"identifier": {"system": "urn:mrn", "value": "123"}}}
    rows = [r for r in extract(o) if r.param == "subject"]
    assert rows and rows[0].sys == "urn:mrn" and rows[0].code == "123" and rows[0].rid is None


def test_reference_helpers_accept_absolute_and_versioned_forms():
    from sekmet.fhir.common import ref_type, same_ref
    from sekmet.workflows.scheduling import _actor
    for r in ("Patient/p1", "https://server.fire.ly/r4/Patient/p1", "http://x/fhir/Patient/p1/_history/3"):
        assert ref_type({"reference": r}) == "Patient"
        assert same_ref({"reference": r}, "Patient", "p1")
    assert not same_ref({"reference": "Patient/p2"}, "Patient", "p1")
    assert ref_type({"display": "no literal"}) is None
    appt = {"id": "a", "participant": [{"actor": {"reference": "https://s/r4/Practitioner/d"}},
                                       {"actor": {"reference": "https://s/r4/Patient/p"}}]}
    assert _actor(appt, "Patient")["reference"].endswith("Patient/p")
    assert _actor(appt, "Location", required=False) is None


def test_backport_notification_is_structurally_valid_and_round_trips():
    from sekmet.fhir.validation import has_errors, structural_issues
    from sekmet.subscriptions.backport import build_subscription, notification_bundle, parse_notification
    sub = build_subscription("http://t/topic", "http://x/hook", ["Encounter?patient=Patient/p"], "full-resource")
    sub["id"] = "s1"
    assert not has_errors(structural_issues(sub)), structural_issues(sub)
    enc = {"resourceType": "Encounter", "id": "e1", "status": "finished",
           "class": {"system": "http://terminology.hl7.org/CodeSystem/v3-ActCode", "code": "IMP"}}
    b = notification_bundle(sub, "http://base/fhir", "event-notification", 3,
                            [{"number": 3, "focus": "Encounter/e1", "context": ["Patient/p"]}], [enc])
    assert not has_errors(structural_issues(b)), structural_issues(b)
    parsed = parse_notification(b)
    assert parsed["type"] == "event-notification" and parsed["topic"] == "http://t/topic"
    assert parsed["events"][0]["focus"] == "http://base/fhir/Encounter/e1" and parsed["resources"][0]["id"] == "e1"
    # camelCase variant (other implementations) is understood too
    camel = {"resourceType": "Bundle", "type": "history", "entry": [{"resource": {"resourceType": "Parameters", "parameter": [
        {"name": "type", "valueCode": "event-notification"},
        {"name": "notificationEvent", "part": [{"name": "eventNumber", "valueString": "1"},
                                               {"name": "focus", "valueReference": {"reference": "Encounter/e1"}}]}]}}]}
    assert parse_notification(camel)["events"][0]["focus"] == "Encounter/e1"

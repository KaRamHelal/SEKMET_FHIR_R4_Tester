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

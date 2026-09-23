"""Deterministic synthetic demographics (Faker) for patients and practitioners."""
from __future__ import annotations

import random
from datetime import date

from faker import Faker

_fake = Faker()


def seed(n: int | None) -> None:
    if n is not None:
        Faker.seed(n)
        random.seed(n)


def mrn() -> str:
    return f"MRN{random.randint(10_000_000, 99_999_999)}"


def patient_resource(mrn_system: str, mrn_value: str | None = None, gender: str | None = None) -> dict:
    gender = gender or random.choice(["male", "female"])
    first = _fake.first_name_male() if gender == "male" else _fake.first_name_female()
    last = _fake.last_name()
    dob: date = _fake.date_of_birth(minimum_age=1, maximum_age=90)
    value = mrn_value or mrn()
    return {
        "resourceType": "Patient",
        "identifier": [{
            "use": "usual",
            "type": {"coding": [{"system": "http://terminology.hl7.org/CodeSystem/v2-0203", "code": "MR",
                                 "display": "Medical record number"}], "text": "MRN"},
            "system": mrn_system, "value": value,
        }],
        "active": True,
        "name": [{"use": "official", "family": last, "given": [first], "text": f"{first} {last}"}],
        "telecom": [
            {"system": "phone", "value": _fake.numerify("+1-555-###-####"), "use": "home"},
            {"system": "email", "value": f"{first}.{last}@example.org".lower(), "use": "home"},
        ],
        "gender": gender,
        "birthDate": dob.isoformat(),
        "address": [{
            "use": "home", "type": "physical", "line": [_fake.street_address()], "city": _fake.city(),
            "postalCode": _fake.postcode(), "country": "US",
        }],
        "maritalStatus": {"coding": [{"system": "http://terminology.hl7.org/CodeSystem/v3-MaritalStatus",
                                      "code": random.choice(["M", "S", "W", "D"])}]},
        "communication": [{"language": {"coding": [{"system": "urn:ietf:bcp:47", "code": "en-US",
                                                    "display": "English (United States)"}]}, "preferred": True}],
    }


def person_name(gender: str | None = None) -> tuple[str, str]:
    gender = gender or random.choice(["male", "female"])
    first = _fake.first_name_male() if gender == "male" else _fake.first_name_female()
    return first, _fake.last_name()


def phone() -> str:
    return _fake.numerify("+1-555-###-####")


def street() -> str:
    return _fake.street_address()

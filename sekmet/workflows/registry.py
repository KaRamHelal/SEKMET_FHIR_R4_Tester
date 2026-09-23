"""Workflow registry + per-run context (facility master data, identifier systems, reference helpers)."""
from __future__ import annotations

import random
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from ..fhir.common import codeable, instant, parse_reference, ref
from ..seed import generator
from . import catalog as C
from .target import Target, WorkflowError, make_target


@dataclass
class Param:
    name: str
    label: str = ""
    kind: str = "str"  # str | ref | int | float | bool | choice | json
    default: Any = None
    choices: list[str] = field(default_factory=list)
    required: bool = False
    ref_type: str | None = None


@dataclass
class Workflow:
    key: str
    title: str
    fn: Callable
    params: list[Param]
    description: str = ""

    @property
    def group(self) -> str:
        return self.key.split(".")[0]


REGISTRY: dict[str, Workflow] = {}


def workflow(key: str, title: str, params: list[Param] | None = None, description: str = ""):
    def deco(fn):
        REGISTRY[key] = Workflow(key, title, fn, params or [], description or (fn.__doc__ or "").strip())
        return fn
    return deco


# master data cache: (target description, key) -> (expires, resource)
_md_cache: dict[tuple[str, str], tuple[float, dict]] = {}
_md_lock = threading.Lock()

ORGS = {
    "hospital": ("HOSP-001", "prov", "Healthcare Provider", None),
    "lab": ("LAB-001", "dept", "Hospital Department", "Clinical Laboratory"),
    "radiology": ("RAD-001", "dept", "Hospital Department", "Radiology Department"),
    "pharmacy": ("PHARM-001", "dept", "Hospital Department", "Inpatient Pharmacy"),
    "payer": ("PAYER-001", "pay", "Payer", "Acme Health Insurance"),
}
LOCATIONS = {
    # key: (identifier, name, physical type code, display, parent key, mode)
    "ward-4a": ("W-4A", "Ward 4A - Internal Medicine", "wa", "Ward", None),
    "room-401": ("R-401", "Room 401", "ro", "Room", "ward-4a"),
    "bed-401-1": ("B-401-1", "Bed 401-1", "bd", "Bed", "room-401"),
    "bed-401-2": ("B-401-2", "Bed 401-2", "bd", "Bed", "room-401"),
    "ward-5b": ("W-5B", "Ward 5B - Surgery", "wa", "Ward", None),
    "room-502": ("R-502", "Room 502", "ro", "Room", "ward-5b"),
    "bed-502-1": ("B-502-1", "Bed 502-1", "bd", "Bed", "room-502"),
    "icu": ("ICU-1", "Intensive Care Unit", "wa", "Ward", None),
    "bed-icu-1": ("B-ICU-1", "ICU Bed 1", "bd", "Bed", "icu"),
    "opd-1": ("OPD-1", "Outpatient Clinic 1", "ro", "Room", None),
    "radiology-room": ("XR-1", "Radiology Room 1", "ro", "Room", None),
}
PRACTITIONERS = {
    "attending": ("P-1001", "Gregory", "House", "male", "309343006", "Physician"),
    "surgeon": ("P-1002", "Meredith", "Grey", "female", "304292004", "Surgeon"),
    "radiologist": ("P-1003", "Lisa", "Cuddy", "female", "66862007", "Radiologist"),
    "pharmacist": ("P-1004", "James", "Wilson", "male", "46255001", "Pharmacist"),
    "nurse": ("P-1005", "Carla", "Espinosa", "female", "224535009", "Registered nurse"),
    "lab-tech": ("P-1006", "Robert", "Chase", "male", "159001001", "Laboratory technician"),
}


class WF:
    """Context handed to each workflow function."""

    def __init__(self, app_ctx, target: Target):
        self.ctx = app_ctx
        self.t = target
        self.settings = app_ctx.settings
        self.ids = app_ctx.settings.identifiers
        self.events: list[str] = []

    # ---------- helpers ----------

    def get(self, rtype: str, value: Any) -> dict:
        """Accept an id, 'Type/id', a Reference dict or a resource dict and return the resource."""
        if isinstance(value, dict):
            if value.get("resourceType") == rtype and value.get("id"):
                return value
            if "reference" in value:
                value = value["reference"]
        if not value:
            raise WorkflowError(f"{rtype} is required")
        t, i, _ = parse_reference(str(value))
        return self.t.read(t or rtype, i or str(value))

    def resolve(self, reference: str | dict) -> dict:
        return self.t.resolve(reference)

    def emit(self, event: str, focus: list[dict], related: list[dict] | None = None) -> None:
        self.events.append(event)
        self.t.emit(event, focus, related)

    def ident(self, system: str, value: str, type_code: str | None = None) -> dict:
        i: dict[str, Any] = {"system": system, "value": value}
        if type_code:
            i["type"] = codeable(C.V2_0203, type_code)
        return i

    @staticmethod
    def number(prefix: str) -> str:
        return f"{prefix}{int(time.time() * 1000) % 10**10:010d}{random.randint(10, 99)}"

    def _cached(self, key: str, build: Callable[[], dict]) -> dict:
        ck = (self.t.describe(), key)
        with _md_lock:
            hit = _md_cache.get(ck)
        if hit and hit[0] > time.time():
            return hit[1]
        res = build()
        with _md_lock:
            _md_cache[ck] = (time.time() + 600, res)
        return res

    # ---------- master data ----------

    def org(self, key: str = "hospital") -> dict:
        ident, code, display, name = ORGS[key]
        name = name or self.settings.facility_name

        def build():
            res = {"resourceType": "Organization", "identifier": [self.ident(self.ids.organization, ident)],
                   "active": True, "type": [codeable(C.ORG_TYPE, code, display)], "name": name,
                   "telecom": [{"system": "phone", "value": generator.phone(), "use": "work"}]}
            if key != "hospital" and key != "payer":
                res["partOf"] = ref(self.org("hospital"))
            return self.t.ensure(res, f"identifier={self.ids.organization}|{ident}")
        return self._cached(f"org:{key}", build)

    def location(self, key: str) -> dict:
        ident, name, ptype, pdisplay, parent = LOCATIONS[key]

        def build():
            res = {"resourceType": "Location", "identifier": [self.ident(self.ids.location, ident)],
                   "status": "active", "name": name, "mode": "instance",
                   "physicalType": codeable(C.LOC_PHYSICAL, ptype, pdisplay),
                   "managingOrganization": ref(self.org("hospital"))}
            if ptype == "bd":
                res["operationalStatus"] = {"system": "http://terminology.hl7.org/CodeSystem/v2-0116",
                                            "code": "U", "display": "Unoccupied"}
            if parent:
                res["partOf"] = ref(self.location(parent))
            return self.t.ensure(res, f"identifier={self.ids.location}|{ident}")
        return self._cached(f"loc:{key}", build)

    def practitioner(self, key: str = "attending") -> dict:
        ident, given, family, gender, _, _ = PRACTITIONERS[key]

        def build():
            res = {"resourceType": "Practitioner", "identifier": [self.ident(self.ids.practitioner, ident)],
                   "active": True, "name": [{"use": "official", "family": family, "given": [given], "prefix": ["Dr."]
                                             if key in ("attending", "surgeon", "radiologist") else []}],
                   "gender": gender,
                   "telecom": [{"system": "phone", "value": generator.phone(), "use": "work"}]}
            if not res["name"][0]["prefix"]:
                del res["name"][0]["prefix"]
            return self.t.ensure(res, f"identifier={self.ids.practitioner}|{ident}")
        return self._cached(f"prac:{key}", build)

    def practitioner_role(self, key: str = "attending") -> dict:
        ident, _, _, _, code, display = PRACTITIONERS[key]

        def build():
            res = {"resourceType": "PractitionerRole", "identifier": [self.ident(self.ids.practitioner, f"{ident}-R")],
                   "active": True, "practitioner": ref(self.practitioner(key)), "organization": ref(self.org("hospital")),
                   "code": [codeable(C.SNOMED, code, display)]}
            return self.t.ensure(res, f"identifier={self.ids.practitioner}|{ident}-R")
        return self._cached(f"role:{key}", build)

    def facility(self) -> dict[str, dict]:
        """Ensure all master data exists at the target."""
        out = {f"org:{k}": self.org(k) for k in ORGS}
        out.update({f"location:{k}": self.location(k) for k in LOCATIONS})
        out.update({f"practitioner:{k}": self.practitioner(k) for k in PRACTITIONERS})
        out.update({f"role:{k}": self.practitioner_role(k) for k in PRACTITIONERS})
        return out


def clear_cache() -> None:
    with _md_lock:
        _md_cache.clear()


def coerce(p: Param, value: Any) -> Any:
    if value in (None, ""):
        return p.default
    if p.kind == "int":
        return int(value)
    if p.kind == "float":
        return float(value)
    if p.kind == "bool":
        return value if isinstance(value, bool) else str(value).lower() in ("1", "true", "yes", "on")
    if p.kind == "json" and isinstance(value, str):
        import json
        return json.loads(value)
    return value


def run_workflow(app_ctx, key: str, target: str | Target | None, args: dict | None = None) -> dict:
    wf_def = REGISTRY.get(key)
    if not wf_def:
        raise WorkflowError(f"Unknown workflow '{key}'. Known: {', '.join(sorted(REGISTRY))}")
    tgt = target if isinstance(target, Target) else make_target(app_ctx, target)
    args = dict(args or {})
    kwargs = {}
    for p in wf_def.params:
        v = coerce(p, args.pop(p.name, None))
        if p.required and v in (None, ""):
            raise WorkflowError(f"Workflow {key}: parameter '{p.name}' is required")
        kwargs[p.name] = v
    if args:
        raise WorkflowError(f"Workflow {key}: unknown parameters {sorted(args)}")
    wf = WF(app_ctx, tgt)
    started = instant()
    result = wf_def.fn(wf, **kwargs) or {}
    result.setdefault("_meta", {"workflow": key, "target": tgt.describe(), "started": started,
                                "events": wf.events,
                                "acks": getattr(tgt, "acks", [])})
    return result

"""Extract search-index rows from a resource according to search_params."""
from __future__ import annotations

import calendar
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator

from .common import parse_reference
from .search_params import params_for


@dataclass
class IndexRow:
    param: str
    s: str | None = None  # normalized string / text for :text
    sys: str | None = None
    code: str | None = None  # token code / exact string / uri
    rtype: str | None = None
    rid: str | None = None
    lo: float | None = None
    hi: float | None = None
    num: float | None = None


def norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", str(s))
    return "".join(c for c in s if not unicodedata.combining(c)).lower().strip()


_SEG_RE = re.compile(r"^(?P<key>[A-Za-z_]+)(?:\[(?P<filter>[^\]]+)\])?$")


def walk(node: Any, path: str) -> Iterator[Any]:
    """Walk a dotted path through dicts and lists. `telecom[phone]` filters on .system."""
    items = [node]
    for seg in path.split("."):
        m = _SEG_RE.match(seg)
        if not m:
            return
        key, flt = m.group("key"), m.group("filter")
        nxt = []
        for it in items:
            if not isinstance(it, dict) or key not in it:
                continue
            v = it[key]
            vals = v if isinstance(v, list) else [v]
            if flt:
                vals = [x for x in vals if isinstance(x, dict) and x.get("system") == flt]
            nxt.extend(vals)
        items = nxt
    yield from items


# ---------------- dates ----------------

_DATE_RE = re.compile(
    r"^(?P<y>\d{4})(?:-(?P<m>\d{2})(?:-(?P<d>\d{2})(?:T(?P<H>\d{2}):(?P<M>\d{2})(?::(?P<S>\d{2})(?P<f>\.\d+)?)?"
    r"(?P<tz>Z|[+-]\d{2}:\d{2})?)?)?)?$"
)


def date_range(value: str) -> tuple[float, float] | None:
    """Return [lo, hi) epoch seconds implied by a FHIR date/dateTime/instant's precision."""
    m = _DATE_RE.match(str(value).strip())
    if not m:
        return None
    g = m.groupdict()
    y = int(g["y"])
    tz = timezone.utc
    if g["tz"] and g["tz"] != "Z":
        sign = 1 if g["tz"][0] == "+" else -1
        hh, mm = g["tz"][1:].split(":")
        tz = timezone(sign * timedelta(hours=int(hh), minutes=int(mm)))
    try:
        if not g["m"]:
            lo = datetime(y, 1, 1, tzinfo=tz)
            hi = datetime(y + 1, 1, 1, tzinfo=tz)
        elif not g["d"]:
            mo = int(g["m"])
            lo = datetime(y, mo, 1, tzinfo=tz)
            hi = lo + timedelta(days=calendar.monthrange(y, mo)[1])
        elif not g["H"]:
            lo = datetime(y, int(g["m"]), int(g["d"]), tzinfo=tz)
            hi = lo + timedelta(days=1)
        else:
            sec = int(g["S"] or 0)
            micro = int(float(g["f"]) * 1_000_000) if g["f"] else 0
            lo = datetime(y, int(g["m"]), int(g["d"]), int(g["H"]), int(g["M"]), sec, micro, tzinfo=tz)
            if g["f"]:
                hi = lo + timedelta(milliseconds=1)
            elif g["S"]:
                hi = lo + timedelta(seconds=1)
            else:
                hi = lo + timedelta(minutes=1)
    except ValueError:
        return None
    return lo.timestamp(), hi.timestamp()


MIN_TS, MAX_TS = -62135596800.0, 253402300799.0


# ---------------- extraction ----------------

def _string_rows(p: str, v: Any) -> Iterator[IndexRow]:
    if isinstance(v, str):
        yield IndexRow(p, s=norm(v), code=v)
    elif isinstance(v, dict):
        parts: list[str] = []
        for k in ("text", "family", "prefix", "suffix", "city", "district", "state", "postalCode", "country", "value"):
            if isinstance(v.get(k), str):
                parts.append(v[k])
        for k in ("given", "line"):
            parts.extend(x for x in v.get(k, []) if isinstance(x, str))
        for x in parts:
            yield IndexRow(p, s=norm(x), code=x)


def _token_rows(p: str, v: Any) -> Iterator[IndexRow]:
    if isinstance(v, bool):
        yield IndexRow(p, code=str(v).lower())
    elif isinstance(v, (str, int, float)):
        yield IndexRow(p, code=str(v))
    elif isinstance(v, dict):
        if "coding" in v or ("text" in v and "code" not in v and "value" not in v):
            for c in v.get("coding", []) or []:
                yield IndexRow(p, sys=c.get("system"), code=c.get("code"), s=norm(c.get("display") or v.get("text") or ""))
            if v.get("text"):
                yield IndexRow(p, s=norm(v["text"]))
        elif "value" in v:  # Identifier / ContactPoint
            text = (v.get("type") or {}).get("text") if isinstance(v.get("type"), dict) else None
            sys = v.get("system")
            if sys in ("phone", "email", "fax", "pager", "url", "sms", "other"):
                sys = None  # ContactPoint.system is not a token system
            yield IndexRow(p, sys=sys, code=str(v["value"]), s=norm(text) if text else None)
        elif "code" in v or "system" in v:  # Coding
            yield IndexRow(p, sys=v.get("system"), code=v.get("code"), s=norm(v.get("display") or ""))


def _reference_rows(p: str, v: Any, target: str | None) -> Iterator[IndexRow]:
    if not isinstance(v, dict):
        return
    r = v.get("reference")
    if isinstance(r, str):
        rtype, rid, _ = parse_reference(r)
        if rtype and (target is None or rtype == target):
            yield IndexRow(p, s=r, rtype=rtype, rid=rid)
    ident = v.get("identifier")
    if isinstance(ident, dict) and ident.get("value") and (target is None or v.get("type") in (None, target)):
        # logical reference: searchable via the :identifier modifier
        yield IndexRow(p, sys=ident.get("system"), code=str(ident["value"]), s=r if isinstance(r, str) else None)


def _date_rows(p: str, v: Any) -> Iterator[IndexRow]:
    if isinstance(v, str):
        rng = date_range(v)
        if rng:
            yield IndexRow(p, lo=rng[0], hi=rng[1], code=v)
    elif isinstance(v, dict) and ("start" in v or "end" in v):
        lo = date_range(v["start"])[0] if v.get("start") and date_range(v["start"]) else MIN_TS
        hi = date_range(v["end"])[1] if v.get("end") and date_range(v["end"]) else MAX_TS
        yield IndexRow(p, lo=lo, hi=hi, code=v.get("start") or v.get("end"))


def extract(resource: dict) -> list[IndexRow]:
    rows: list[IndexRow] = []
    rtype = resource["resourceType"]
    for name, sp in params_for(rtype).items():
        for path in sp.paths:
            for v in walk(resource, path):
                if sp.kind == "string":
                    rows.extend(_string_rows(name, v))
                elif sp.kind == "token":
                    rows.extend(_token_rows(name, v))
                elif sp.kind == "reference":
                    rows.extend(_reference_rows(name, v, sp.target))
                elif sp.kind == "date":
                    rows.extend(_date_rows(name, v))
                elif sp.kind == "number" and isinstance(v, (int, float)) and not isinstance(v, bool):
                    rows.append(IndexRow(name, num=float(v)))
                elif sp.kind == "uri" and isinstance(v, str):
                    rows.append(IndexRow(name, code=v))
    return rows

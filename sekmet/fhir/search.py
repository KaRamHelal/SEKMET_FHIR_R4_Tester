"""FHIR search engine over the SQLite index.

Supports: string/token/reference/date/number/uri params, modifiers (:missing :exact :contains :text :not
:identifier :[Type] :below), date/number prefixes, comma OR / repeated AND, chained params, _has reverse
chains, _id, _sort, _count/_offset paging, _include/_revinclude (incl. :iterate), _summary, _elements.
"""
from __future__ import annotations

import itertools
import json
from dataclasses import dataclass, field
from urllib.parse import urlencode

from .common import FhirError, issue, parse_reference
from .indexer import date_range, norm
from .search_params import PARAMS, SPECIAL, get_param

PREFIXES = ("eq", "ne", "gt", "lt", "ge", "le", "sa", "eb", "ap")
DEFAULT_COUNT = 20
MAX_COUNT = 1000
SUBSETTED = {"system": "http://terminology.hl7.org/CodeSystem/v3-ObservationValue", "code": "SUBSETTED",
             "display": "subsetted"}
SUMMARY_ELEMENTS = {"identifier", "status", "name", "code", "subject", "patient", "gender", "birthDate", "active",
                    "category", "class", "period", "intent", "effectiveDateTime", "issued", "start", "end",
                    "authoredOn", "type", "beneficiary", "payor", "created", "outcome", "for", "focus",
                    "schedule", "participant", "criteria", "channel", "eventCoding", "response", "timestamp"}


class Unsupported(Exception):
    pass


@dataclass
class SearchResult:
    total: int
    matches: list[dict]
    included: list[dict] = field(default_factory=list)
    issues: list[dict] = field(default_factory=list)
    count: int = DEFAULT_COUNT
    offset: int = 0
    used_params: list[tuple[str, str]] = field(default_factory=list)
    summary_count: bool = False


def split_unescaped(value: str, sep: str) -> list[str]:
    parts, buf, esc = [], [], False
    for ch in value:
        if esc:
            buf.append(ch if ch in ",|$\\" else "\\" + ch)
            esc = False
        elif ch == "\\":
            esc = True
        elif ch == sep:
            parts.append("".join(buf)); buf = []
        else:
            buf.append(ch)
    parts.append("".join(buf))
    return parts


class _Aliases:
    def __init__(self):
        self._c = itertools.count()

    def __call__(self, p: str) -> str:
        return f"{p}{next(self._c)}"


def _prefix(v: str) -> tuple[str, str]:
    if len(v) > 2 and v[:2] in PREFIXES and (v[2].isdigit() or v[2] in "-+."):
        return v[:2], v[2:]
    return "eq", v


def _date_cond(i: str, value: str) -> tuple[str, list]:
    pre, v = _prefix(value)
    rng = date_range(v)
    if not rng:
        raise FhirError(400, f"Invalid date search value '{value}'", "value")
    lo, hi = rng
    table = {
        "eq": (f"({i}.lo >= ? AND {i}.hi <= ?)", [lo, hi]),
        "ne": (f"NOT ({i}.lo >= ? AND {i}.hi <= ?)", [lo, hi]),
        "gt": (f"{i}.hi > ?", [hi]),
        "lt": (f"{i}.lo < ?", [lo]),
        "ge": (f"{i}.hi > ?", [lo]),
        "le": (f"{i}.lo < ?", [hi]),
        "sa": (f"{i}.lo >= ?", [hi]),
        "eb": (f"{i}.hi <= ?", [lo]),
        "ap": (f"({i}.lo < ? AND {i}.hi > ?)", [hi + 86400, lo - 86400]),
    }
    sql, args = table[pre]
    return f"({i}.lo IS NOT NULL AND {sql})", args


def _number_cond(i: str, value: str) -> tuple[str, list]:
    pre, v = _prefix(value)
    v = v.split("|", 1)[0]  # quantity: number|system|code
    try:
        n = float(v)
    except ValueError:
        raise FhirError(400, f"Invalid number search value '{value}'", "value")
    ops = {"eq": "=", "ne": "!=", "gt": ">", "lt": "<", "ge": ">=", "le": "<=", "sa": ">", "eb": "<", "ap": "="}
    if pre == "ap":
        return f"({i}.num BETWEEN ? AND ?)", [n * 0.9, n * 1.1]
    return f"({i}.num {ops[pre]} ?)", [n]


def _token_cond(i: str, value: str, modifier: str) -> tuple[str, list]:
    if modifier == "text":
        return f"({i}.s LIKE ?)", [norm(value) + "%"]
    parts = split_unescaped(value, "|")
    if len(parts) == 1:
        return f"({i}.code = ?)", [parts[0]]
    system, code = parts[0], "|".join(parts[1:])
    if system == "":
        return f"({i}.sys IS NULL AND {i}.code = ?)", [code]
    if code == "":
        return f"({i}.sys = ?)", [system]
    return f"({i}.sys = ? AND {i}.code = ?)", [system, code]


def _reference_cond(i: str, value: str, modifier: str, sp) -> tuple[str, list]:
    if modifier == "identifier":
        parts = split_unescaped(value, "|")
        if len(parts) == 1:
            return f"({i}.rid IS NULL AND {i}.code = ?)", [parts[0]]
        return f"({i}.rid IS NULL AND {i}.sys = ? AND {i}.code = ?)", [parts[0], "|".join(parts[1:])]
    rtype, rid, _ = parse_reference(value)
    type_restrict = modifier or sp.target
    if rtype:
        if type_restrict and rtype != type_restrict:
            return "(0)", []
        return f"({i}.rtype = ? AND {i}.rid = ?)", [rtype, rid]
    if type_restrict:
        return f"({i}.rtype = ? AND {i}.rid = ?)", [type_restrict, value]
    return f"({i}.rid = ?)", [value]


class SearchEngine:
    def __init__(self, store):
        self.store = store

    # ---------------- WHERE construction ----------------

    def _condition(self, alias: str, rtype: str, key: str, value: str, aliases: _Aliases) -> tuple[str, list]:
        if key.startswith("_has:"):
            return self._has(alias, rtype, key, value, aliases)
        head, dot, child = key.partition(".")
        name, _, modifier = head.partition(":")
        if name == "_id":
            ids = split_unescaped(value, ",")
            op = "NOT IN" if modifier == "not" else "IN"
            return f"({alias}.id {op} ({','.join('?' * len(ids))}))", ids
        sp = get_param(rtype, name)
        if sp is None:
            raise Unsupported(f"Unknown search parameter '{name}' for {rtype}")
        i = aliases("i")
        # index-driven: the candidate ids come from idx (type, param, ...) instead of probing idx once per resource
        base = f"SELECT {i}.id FROM idx {i} WHERE {i}.type=? AND {i}.param=?"
        if dot:
            return self._chain(alias, rtype, sp, modifier, child, value, aliases, i, base)
        if modifier == "missing":
            missing = value.lower() == "true"
            return (f"{alias}.id {'NOT ' if missing else ''}IN ({base})", [rtype, name])
        if modifier and modifier not in ("exact", "contains", "text", "not", "identifier", "below") and not (
                sp.kind == "reference" and modifier[:1].isupper()):
            raise Unsupported(f"Modifier ':{modifier}' is not supported on '{name}'")
        ors, args = [], []
        for v in split_unescaped(value, ","):
            if sp.kind == "string":
                if modifier == "exact":
                    c, a = f"({i}.code = ?)", [v]
                elif modifier == "contains":
                    c, a = f"({i}.s LIKE ?)", ["%" + norm(v) + "%"]
                else:
                    c, a = f"({i}.s LIKE ?)", [norm(v) + "%"]
            elif sp.kind == "token":
                c, a = _token_cond(i, v, modifier if modifier == "text" else "")
            elif sp.kind == "reference":
                c, a = _reference_cond(i, v, modifier, sp)
            elif sp.kind == "date":
                c, a = _date_cond(i, v)
            elif sp.kind == "number":
                c, a = _number_cond(i, v)
            elif sp.kind == "uri":
                c, a = (f"({i}.code LIKE ?)", [v + "%"]) if modifier == "below" else (f"({i}.code = ?)", [v])
            else:
                raise Unsupported(f"Parameter kind {sp.kind} unsupported")
            ors.append(c); args.extend(a)
        sql = f"{alias}.id {'NOT ' if modifier == 'not' else ''}IN ({base} AND ({' OR '.join(ors)}))"
        return sql, [rtype, name, *args]

    def _chain(self, alias, rtype, sp, modifier, child, value, aliases, i, base):
        if sp.kind != "reference":
            raise Unsupported(f"Chaining requires a reference parameter; '{sp.name}' is {sp.kind}")
        child_name = child.split(".")[0].split(":")[0]
        if modifier:
            targets = [modifier]
        elif sp.target:
            targets = [sp.target]
        else:
            targets = [t for t, ps in PARAMS.items() if t != "*" and child_name in ps] or []
            if child_name.startswith("_") or child_name in PARAMS["*"]:
                targets = [t for t in PARAMS if t != "*"]
        if not targets:
            raise Unsupported(f"No target type supports chained parameter '{child_name}'")
        subs, args = [], [rtype, sp.name]
        for t in targets:
            c = aliases("c")
            try:
                inner_sql, inner_args = self._condition(c, t, child, value, aliases)
            except Unsupported:
                continue
            subs.append(f"({i}.rtype = ? AND {i}.rid IN (SELECT {c}.id FROM resources {c} "
                        f"WHERE {c}.type = ? AND {c}.deleted = 0 AND {inner_sql}))")
            args.extend([t, t, *inner_args])
        if not subs:
            raise Unsupported(f"Chained parameter '{child}' not supported on {targets}")
        return f"{alias}.id IN ({base} AND ({' OR '.join(subs)}))", args

    def _has(self, alias, rtype, key, value, aliases):
        parts = key.split(":")
        if len(parts) < 4:
            raise Unsupported(f"Malformed _has parameter '{key}'")
        htype, hparam, child = parts[1], parts[2], ":".join(parts[3:])
        sp = get_param(htype, hparam)
        if sp is None or sp.kind != "reference":
            raise Unsupported(f"_has: '{hparam}' is not a reference parameter of {htype}")
        h, hi = aliases("h"), aliases("i")
        inner_sql, inner_args = self._condition(h, htype, child, value, aliases)
        # ids of `rtype` referenced (via hparam) by htype resources that match the inner condition
        sql = (f"{alias}.id IN (SELECT {hi}.rid FROM idx {hi} JOIN resources {h} ON {h}.type = {hi}.type "
               f"AND {h}.id = {hi}.id WHERE {hi}.type = ? AND {hi}.param = ? AND {hi}.rtype = ? "
               f"AND {h}.deleted = 0 AND {inner_sql})")
        return sql, [htype, hparam, rtype, *inner_args]

    def build_where(self, rtype: str, params: list[tuple[str, str]], strict: bool,
                    issues: list[dict], used: list[tuple[str, str]]) -> tuple[str, list]:
        aliases = _Aliases()
        where, args = ["r.type = ?", "r.deleted = 0"], [rtype]
        for key, value in params:
            base_name = key.split(":")[0].split(".")[0]
            if base_name in SPECIAL and base_name not in ("_id", "_has"):
                continue
            try:
                sql, a = self._condition("r", rtype, key, value, aliases)
            except Unsupported as e:
                if strict:
                    raise FhirError(400, str(e), "not-supported")
                issues.append(issue("warning", "not-supported", f"{e}; parameter ignored"))
                continue
            where.append(sql); args.extend(a); used.append((key, value))
        return " AND ".join(where), args

    # ---------------- main search ----------------

    def search(self, rtype: str, params: list[tuple[str, str]], strict: bool = False,
               extra_where: tuple[str, list] | None = None) -> SearchResult:
        issues: list[dict] = []
        used: list[tuple[str, str]] = []
        where, args = self.build_where(rtype, params, strict, issues, used)
        if extra_where:
            where += " AND " + extra_where[0]
            args += extra_where[1]
        control = {}
        for k, v in params:
            if k.split(":")[0] in SPECIAL:
                control.setdefault(k, []).append(v)
        count = DEFAULT_COUNT
        if "_count" in control:
            try:
                count = max(0, min(MAX_COUNT, int(control["_count"][-1])))
            except ValueError:
                raise FhirError(400, "_count must be an integer", "value")
        offset = int(control.get("_offset", ["0"])[-1] or 0)
        summary = control.get("_summary", [None])[-1]
        order = self._order(rtype, control.get("_sort", []), issues, strict)
        with self.store.lock:
            total = self.store.conn.execute(f"SELECT COUNT(*) FROM resources r WHERE {where}", args).fetchone()[0]
            rows = [] if summary == "count" or count == 0 else self.store.conn.execute(
                f"SELECT r.json FROM resources r WHERE {where} ORDER BY {order} LIMIT ? OFFSET ?",
                [*args, count, offset]).fetchall()
        matches = [json.loads(r[0]) for r in rows]
        included = self._includes(matches, control, issues)
        for k in ("_count", "_sort", "_include", "_revinclude", "_summary", "_elements", "_include:iterate",
                  "_revinclude:iterate", "_total"):
            for v in control.get(k, []):
                used.append((k, v))
        elements = control.get("_elements")
        if summary in ("true", "text", "data") or elements:
            fields = set()
            for e in elements or []:
                fields.update(x.strip() for x in e.split(",") if x.strip())
            matches = [subset(m, summary, fields) for m in matches]
        return SearchResult(total, matches, included, issues, count, offset, used, summary == "count")

    def _order(self, rtype: str, sorts: list[str], issues: list[dict], strict: bool) -> str:
        clauses = []
        for s in ",".join(sorts).split(","):
            s = s.strip()
            if not s:
                continue
            desc = s.startswith("-")
            name = s.lstrip("-")
            direction = "DESC" if desc else "ASC"
            if name == "_id":
                clauses.append(f"r.id {direction}")
                continue
            if name == "_lastUpdated":
                clauses.append(f"r.last_updated {direction}")
                continue
            sp = get_param(rtype, name)
            if sp is None:
                msg = f"Unknown sort parameter '{name}'"
                if strict:
                    raise FhirError(400, msg, "not-supported")
                issues.append(issue("warning", "not-supported", msg + "; ignored"))
                continue
            col = {"date": "lo", "string": "s", "token": "code", "number": "num", "reference": "rid", "uri": "code"}[sp.kind]
            agg = "MAX" if desc and sp.kind == "date" else "MIN"
            name_sql = name.replace("'", "")
            clauses.append(f"(SELECT {agg}(si.{col}) FROM idx si WHERE si.type=r.type AND si.id=r.id "
                           f"AND si.param='{name_sql}') IS NULL, "
                           f"(SELECT {agg}(si.{col}) FROM idx si WHERE si.type=r.type AND si.id=r.id "
                           f"AND si.param='{name_sql}') {direction}")
        clauses.append("r.seq ASC")
        return ", ".join(clauses)

    def _includes(self, matches: list[dict], control: dict, issues: list[dict]) -> list[dict]:
        seen = {(m["resourceType"], m["id"]) for m in matches}
        included: list[dict] = []
        frontier = list(matches)
        specs_inc = [(v, False) for v in control.get("_include", [])] + [(v, True) for v in control.get("_include:iterate", [])]
        specs_rev = [(v, False) for v in control.get("_revinclude", [])] + [(v, True) for v in control.get("_revinclude:iterate", [])]
        for depth in range(4):
            if not frontier or (depth > 0 and not any(it for _, it in specs_inc + specs_rev)):
                break
            new: list[tuple[str, str]] = []
            for spec, iterate in specs_inc:
                if depth > 0 and not iterate:
                    continue
                new += self._include_refs(frontier, spec, issues)
            for spec, iterate in specs_rev:
                if depth > 0 and not iterate:
                    continue
                new += self._revinclude_refs(frontier, spec, issues)
            fresh = []
            for key in dict.fromkeys(new):
                if key not in seen:
                    seen.add(key)
                    fresh.append(key)
            frontier = self.store.load_many(fresh)
            included.extend(frontier)
        return included

    def _include_refs(self, frontier: list[dict], spec: str, issues: list[dict]) -> list[tuple[str, str]]:
        if spec == "*":
            src_type, param, target = None, None, None
        else:
            bits = spec.split(":")
            if len(bits) < 2:
                issues.append(issue("warning", "not-supported", f"Malformed _include '{spec}'"))
                return []
            src_type, param, target = bits[0], bits[1], bits[2] if len(bits) > 2 else None
            if param != "*" and get_param(src_type, param) is None:
                issues.append(issue("warning", "not-supported", f"Unknown _include parameter '{spec}'"))
                return []
        out = []
        with self.store.lock:
            for res in frontier:
                if src_type and res["resourceType"] != src_type:
                    continue
                sql = "SELECT rtype, rid FROM idx WHERE type=? AND id=? AND rid IS NOT NULL"
                args = [res["resourceType"], res["id"]]
                if param and param != "*":
                    sql += " AND param=?"; args.append(param)
                if target:
                    sql += " AND rtype=?"; args.append(target)
                out += [(r[0], r[1]) for r in self.store.conn.execute(sql, args).fetchall()]
        return out

    def _revinclude_refs(self, frontier: list[dict], spec: str, issues: list[dict]) -> list[tuple[str, str]]:
        bits = spec.split(":")
        if len(bits) < 2 or get_param(bits[0], bits[1]) is None:
            issues.append(issue("warning", "not-supported", f"Unsupported _revinclude '{spec}'"))
            return []
        src_type, param = bits[0], bits[1]
        out = []
        with self.store.lock:
            for res in frontier:
                rows = self.store.conn.execute(
                    "SELECT DISTINCT type, id FROM idx WHERE type=? AND param=? AND rtype=? AND rid=?",
                    (src_type, param, res["resourceType"], res["id"])).fetchall()
                out += [(r[0], r[1]) for r in rows]
        return out

    def matches(self, resource: dict, criteria_params: list[tuple[str, str]]) -> bool:
        """Does a stored resource match the given search params? (used by subscriptions)"""
        params = [*criteria_params, ("_id", resource["id"])]
        where, args = self.build_where(resource["resourceType"], params, False, [], [])
        with self.store.lock:
            return self.store.conn.execute(f"SELECT 1 FROM resources r WHERE {where} LIMIT 1", args).fetchone() is not None

    def compartment_keys(self, rtype: str, rid: str) -> list[tuple[str, str]]:
        """All resources that reference rtype/rid through any indexed reference parameter."""
        with self.store.lock:
            rows = self.store.conn.execute(
                "SELECT DISTINCT type, id FROM idx WHERE rtype=? AND rid=?", (rtype, rid)).fetchall()
        return [(r[0], r[1]) for r in rows]


def subset(resource: dict, summary: str | None, fields: set[str]) -> dict:
    keep = {"resourceType", "id", "meta"}
    if summary == "text":
        keep |= {"text"}
    elif summary == "data":
        out = {k: v for k, v in resource.items() if k != "text"}
        return _tag_subsetted(out)
    elif summary == "true":
        keep |= SUMMARY_ELEMENTS
    keep |= fields
    out = {k: v for k, v in resource.items() if k in keep}
    return _tag_subsetted(out)


def _tag_subsetted(res: dict) -> dict:
    meta = dict(res.get("meta") or {})
    meta["tag"] = [*meta.get("tag", []), SUBSETTED]
    res["meta"] = meta
    return res


def search_links(base_url: str, rtype: str, result: SearchResult) -> list[dict]:
    def url(offset: int) -> str:
        q = [(k, v) for k, v in result.used_params if k not in ("_count",)]
        q += [("_count", str(result.count)), ("_offset", str(max(0, offset)))]
        return f"{base_url}/{rtype}?{urlencode(q)}"

    links = [{"relation": "self", "url": url(result.offset)}]
    if result.summary_count or result.count == 0:
        return links
    links.append({"relation": "first", "url": url(0)})
    if result.offset > 0:
        links.append({"relation": "previous", "url": url(result.offset - result.count)})
    if result.offset + result.count < result.total:
        links.append({"relation": "next", "url": url(result.offset + result.count)})
    last = ((result.total - 1) // result.count) * result.count if result.total else 0
    links.append({"relation": "last", "url": url(last)})
    return links


def parse_criteria(criteria: str) -> tuple[str, list[tuple[str, str]]]:
    """'Observation?code=x&status=final' -> ('Observation', [('code','x'),('status','final')])."""
    from urllib.parse import parse_qsl
    rtype, _, q = criteria.partition("?")
    rtype = rtype.rsplit("/", 1)[-1]
    return rtype, parse_qsl(q, keep_blank_values=True)


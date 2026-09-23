"""SQLite resource store: current versions, full history, search index, traffic + notification logs.

All writes go through a single re-entrant lock; `transaction()` makes a group of writes atomic.
Write events are dispatched to listeners after the outermost transaction commits.
"""
from __future__ import annotations

import copy
import json
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator

from .common import FhirError, instant, new_id, ID_RE
from .indexer import extract

SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS resources (
  type TEXT NOT NULL, id TEXT NOT NULL, version INTEGER NOT NULL, last_updated TEXT NOT NULL,
  deleted INTEGER NOT NULL DEFAULT 0, origin TEXT, json TEXT, seq INTEGER,
  PRIMARY KEY (type, id));
CREATE TABLE IF NOT EXISTS versions (
  type TEXT NOT NULL, id TEXT NOT NULL, version INTEGER NOT NULL, last_updated TEXT NOT NULL,
  deleted INTEGER NOT NULL DEFAULT 0, method TEXT, json TEXT, seq INTEGER,
  PRIMARY KEY (type, id, version));
CREATE TABLE IF NOT EXISTS idx (
  type TEXT NOT NULL, id TEXT NOT NULL, param TEXT NOT NULL,
  s TEXT, sys TEXT, code TEXT, rtype TEXT, rid TEXT, lo REAL, hi REAL, num REAL);
CREATE INDEX IF NOT EXISTS idx_res ON idx(type, id);
CREATE INDEX IF NOT EXISTS idx_code ON idx(type, param, code);
CREATE INDEX IF NOT EXISTS idx_ref ON idx(rtype, rid);
CREATE INDEX IF NOT EXISTS idx_s ON idx(type, param, s);
CREATE TABLE IF NOT EXISTS traffic (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, direction TEXT NOT NULL, peer TEXT,
  method TEXT, url TEXT, req_headers TEXT, req_body TEXT, status INTEGER, resp_headers TEXT, resp_body TEXT,
  duration_ms REAL, correlation_id TEXT, run_id TEXT, note TEXT);
CREATE INDEX IF NOT EXISTS traffic_run ON traffic(run_id);
CREATE TABLE IF NOT EXISTS notifications (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, peer TEXT, hook TEXT, method TEXT, path TEXT,
  headers TEXT, body TEXT, resource_type TEXT, resource_id TEXT, traffic_id INTEGER,
  kind TEXT, topic TEXT, subscription TEXT);
CREATE TABLE IF NOT EXISTS runs (
  id TEXT PRIMARY KEY, ts TEXT NOT NULL, scenario TEXT, peer TEXT, status TEXT, summary TEXT, result TEXT);
CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);
"""


@dataclass
class WriteEvent:
    action: str  # create | update | delete
    resource: dict
    origin: str  # internal | external | message | simulator | peer
    previous: dict | None = None
    extra: dict = field(default_factory=dict)


Listener = Callable[[WriteEvent], None]


class Store:
    def __init__(self, path: str):
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None, timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.lock = threading.RLock()
        self._depth = 0
        self._pending: list[WriteEvent] = []
        self.listeners: list[Listener] = []
        self._seq = self.conn.execute("SELECT COALESCE(MAX(seq),0) FROM versions").fetchone()[0]
        self._migrate()

    def _migrate(self) -> None:
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(notifications)")}
        for col in ("kind", "topic", "subscription"):
            if col not in cols:
                self.conn.execute(f"ALTER TABLE notifications ADD COLUMN {col} TEXT")

    # ---------------- transactions & events ----------------

    @contextmanager
    def transaction(self) -> Iterator[None]:
        with self.lock:
            outer = self._depth == 0
            if outer:
                self.conn.execute("BEGIN IMMEDIATE")
                self._pending = []
            self._depth += 1
            try:
                yield
            except BaseException:
                self._depth -= 1
                if outer:
                    self.conn.execute("ROLLBACK")
                    self._pending = []
                raise
            self._depth -= 1
            if not outer:
                return
            self.conn.execute("COMMIT")
            events, self._pending = self._pending, []
        for ev in events:  # dispatched outside the lock
            for fn in list(self.listeners):
                try:
                    fn(ev)
                except Exception as e:  # listeners must never break writes
                    import logging
                    logging.getLogger("sekmet.store").exception("listener failed: %s", e)

    def on_write(self, fn: Listener) -> None:
        self.listeners.append(fn)

    # ---------------- reads ----------------

    def read(self, rtype: str, rid: str) -> dict | None:
        row = self.conn.execute("SELECT json, deleted FROM resources WHERE type=? AND id=?", (rtype, rid)).fetchone()
        if not row:
            return None
        if row["deleted"]:
            raise FhirError(410, f"{rtype}/{rid} has been deleted", "deleted")
        return json.loads(row["json"])

    def exists(self, rtype: str, rid: str) -> bool:
        return self.conn.execute("SELECT 1 FROM resources WHERE type=? AND id=? AND deleted=0", (rtype, rid)).fetchone() is not None

    def vread(self, rtype: str, rid: str, vid: str) -> dict | None:
        row = self.conn.execute("SELECT json, deleted FROM versions WHERE type=? AND id=? AND version=?",
                                (rtype, rid, vid)).fetchone()
        if not row:
            return None
        if row["deleted"]:
            raise FhirError(410, f"{rtype}/{rid}/_history/{vid} is a deletion", "deleted")
        return json.loads(row["json"])

    def history(self, rtype: str | None = None, rid: str | None = None, since: str | None = None,
                limit: int = 100, offset: int = 0) -> tuple[int, list[sqlite3.Row]]:
        where, args = [], []
        if rtype:
            where.append("type=?"); args.append(rtype)
        if rid:
            where.append("id=?"); args.append(rid)
        if since:
            where.append("last_updated>=?"); args.append(since)
        w = ("WHERE " + " AND ".join(where)) if where else ""
        total = self.conn.execute(f"SELECT COUNT(*) FROM versions {w}", args).fetchone()[0]
        rows = self.conn.execute(f"SELECT * FROM versions {w} ORDER BY seq DESC LIMIT ? OFFSET ?",
                                 [*args, limit, offset]).fetchall()
        return total, rows

    def load_many(self, keys: list[tuple[str, str]]) -> list[dict]:
        out = []
        for t, i in keys:
            row = self.conn.execute("SELECT json FROM resources WHERE type=? AND id=? AND deleted=0", (t, i)).fetchone()
            if row:
                out.append(json.loads(row["json"]))
        return out

    def counts(self) -> dict[str, int]:
        rows = self.conn.execute("SELECT type, COUNT(*) c FROM resources WHERE deleted=0 GROUP BY type ORDER BY type")
        return {r["type"]: r["c"] for r in rows}

    def current_version(self, rtype: str, rid: str) -> tuple[int, bool] | None:
        row = self.conn.execute("SELECT version, deleted FROM resources WHERE type=? AND id=?", (rtype, rid)).fetchone()
        return (row["version"], bool(row["deleted"])) if row else None

    # ---------------- writes ----------------

    def _write(self, resource: dict, method: str, origin: str, extra: dict | None = None) -> dict:
        rtype = resource["resourceType"]
        rid = resource.get("id") or new_id()
        if not ID_RE.match(rid):
            raise FhirError(400, f"Invalid resource id '{rid}'", "value")
        with self.transaction():
            cur = self.conn.execute("SELECT version, deleted, json FROM resources WHERE type=? AND id=?",
                                    (rtype, rid)).fetchone()
            previous = json.loads(cur["json"]) if cur and not cur["deleted"] and cur["json"] else None
            version = (cur["version"] + 1) if cur else 1
            ts = instant()
            res = copy.deepcopy(resource)
            res["id"] = rid
            meta = dict(res.get("meta") or {})
            meta["versionId"] = str(version)
            meta["lastUpdated"] = ts
            res["meta"] = meta
            body = json.dumps(res, separators=(",", ":"))
            self._seq += 1
            self.conn.execute(
                "INSERT OR REPLACE INTO resources(type,id,version,last_updated,deleted,origin,json,seq) VALUES (?,?,?,?,0,?,?,?)",
                (rtype, rid, version, ts, origin, body, self._seq))
            self.conn.execute(
                "INSERT INTO versions(type,id,version,last_updated,deleted,method,json,seq) VALUES (?,?,?,?,0,?,?,?)",
                (rtype, rid, version, ts, method, body, self._seq))
            self._reindex(res)
            action = "update" if previous is not None else "create"
            self._pending.append(WriteEvent(action, res, origin, previous, extra or {}))
        return res

    def create(self, resource: dict, origin: str = "internal", keep_id: bool = False, extra: dict | None = None) -> dict:
        res = dict(resource)
        if not keep_id:
            res["id"] = new_id()
        elif res.get("id") and self.exists(res["resourceType"], res["id"]):
            raise FhirError(409, f"{res['resourceType']}/{res['id']} already exists", "duplicate")
        return self._write(res, "POST", origin, extra)

    def update(self, resource: dict, origin: str = "internal", if_match: str | None = None,
               extra: dict | None = None) -> tuple[dict, bool]:
        """Update (or create with client id). Returns (resource, created)."""
        rtype, rid = resource["resourceType"], resource["id"]
        with self.transaction():
            cur = self.current_version(rtype, rid)
            if if_match is not None:
                want = if_match.replace('W/', '').strip('"')
                if not cur or cur[1] or str(cur[0]) != want:
                    raise FhirError(412, f"Version conflict: If-Match {if_match} but current is "
                                         f"{cur[0] if cur else 'none'}", "conflict")
            created = cur is None or cur[1]
            return self._write(resource, "PUT", origin, extra), created

    def delete(self, rtype: str, rid: str, origin: str = "internal") -> bool:
        with self.transaction():
            cur = self.conn.execute("SELECT version, deleted, json FROM resources WHERE type=? AND id=?",
                                    (rtype, rid)).fetchone()
            if not cur or cur["deleted"]:
                return False
            version, ts = cur["version"] + 1, instant()
            self._seq += 1
            self.conn.execute("UPDATE resources SET version=?, last_updated=?, deleted=1, seq=? WHERE type=? AND id=?",
                              (version, ts, self._seq, rtype, rid))
            self.conn.execute(
                "INSERT INTO versions(type,id,version,last_updated,deleted,method,json,seq) VALUES (?,?,?,?,1,'DELETE',NULL,?)",
                (rtype, rid, version, ts, self._seq))
            self.conn.execute("DELETE FROM idx WHERE type=? AND id=?", (rtype, rid))
            prev = json.loads(cur["json"])
            self._pending.append(WriteEvent("delete", prev, origin, prev))
        return True

    def _reindex(self, res: dict) -> None:
        rtype, rid = res["resourceType"], res["id"]
        self.conn.execute("DELETE FROM idx WHERE type=? AND id=?", (rtype, rid))
        rows = [(rtype, rid, r.param, r.s, r.sys, r.code, r.rtype, r.rid, r.lo, r.hi, r.num) for r in extract(res)]
        self.conn.executemany("INSERT INTO idx(type,id,param,s,sys,code,rtype,rid,lo,hi,num) VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows)

    def reindex_all(self) -> int:
        n = 0
        with self.transaction():
            for row in self.conn.execute("SELECT json FROM resources WHERE deleted=0").fetchall():
                self._reindex(json.loads(row["json"]))
                n += 1
        return n

    def reset(self) -> None:
        with self.transaction():
            for t in ("resources", "versions", "idx", "notifications", "traffic", "runs"):
                self.conn.execute(f"DELETE FROM {t}")

    # ---------------- logs ----------------

    def log_traffic(self, **kw: Any) -> int:
        cols = ["ts", "direction", "peer", "method", "url", "req_headers", "req_body", "status", "resp_headers",
                "resp_body", "duration_ms", "correlation_id", "run_id", "note"]
        kw.setdefault("ts", instant())
        vals = [json.dumps(kw[c]) if isinstance(kw.get(c), (dict, list)) else kw.get(c) for c in cols]
        with self.lock:
            cur = self.conn.execute(f"INSERT INTO traffic({','.join(cols)}) VALUES ({','.join('?' * len(cols))})", vals)
            return cur.lastrowid

    def add_traffic_note(self, traffic_id: int, note: str) -> None:
        with self.lock:
            self.conn.execute("UPDATE traffic SET note = COALESCE(note || char(10), '') || ? WHERE id=?", (note, traffic_id))

    def log_notification(self, **kw: Any) -> int:
        cols = ["ts", "peer", "hook", "method", "path", "headers", "body", "resource_type", "resource_id", "traffic_id",
                "kind", "topic", "subscription"]
        kw.setdefault("ts", instant())
        vals = [json.dumps(kw[c]) if isinstance(kw.get(c), (dict, list)) else kw.get(c) for c in cols]
        with self.lock:
            cur = self.conn.execute(f"INSERT INTO notifications({','.join(cols)}) VALUES ({','.join('?' * len(cols))})", vals)
            return cur.lastrowid

    def query(self, sql: str, args: tuple | list = ()) -> list[sqlite3.Row]:
        with self.lock:
            return self.conn.execute(sql, args).fetchall()

    def kv_get(self, k: str) -> str | None:
        row = self.conn.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
        return row["v"] if row else None

    def kv_set(self, k: str, v: str) -> None:
        with self.lock:
            self.conn.execute("INSERT OR REPLACE INTO kv(k,v) VALUES (?,?)", (k, v))

"""SQLite 저장소 (가이드 07장). 무료·파일 하나로 동작한다.

운영 단계에서는 PostgreSQL 등으로 바꿀 수 있도록 SQL을 단순하게 유지한다.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from dataclasses import asdict
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS voices (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    provider TEXT NOT NULL,
    provider_voice_id TEXT,
    sample_path TEXT,
    consent INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    deleted_at REAL
);
CREATE TABLE IF NOT EXISTS templates (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    version INTEGER NOT NULL,
    content TEXT NOT NULL,
    created_at REAL NOT NULL,
    UNIQUE (name, version)
);
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    building_name TEXT NOT NULL,
    building_address TEXT NOT NULL,
    recipient_name TEXT,
    recipient_number TEXT NOT NULL,
    requester_name TEXT NOT NULL,
    inquiry_purpose TEXT NOT NULL,
    template_id TEXT,
    voice_id TEXT,
    status TEXT NOT NULL DEFAULT 'queued',
    attempts INTEGER NOT NULL DEFAULT 0,
    do_not_retry INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS calls (
    id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES jobs(id),
    mode TEXT NOT NULL,
    provider_call_id TEXT UNIQUE,
    template_id TEXT,
    template_version INTEGER,
    interpreter TEXT,
    status TEXT NOT NULL,
    outcome TEXT,
    end_reason TEXT,
    callback_note TEXT,
    review_status TEXT NOT NULL DEFAULT 'pending_review',
    started_at REAL NOT NULL,
    ended_at REAL
);
CREATE TABLE IF NOT EXISTS utterances (
    call_id TEXT NOT NULL REFERENCES calls(id),
    id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    speaker TEXT NOT NULL,
    text TEXT NOT NULL,
    ts REAL NOT NULL,
    response_id TEXT,
    status TEXT,
    kind TEXT,
    intent TEXT,
    source TEXT,
    PRIMARY KEY (call_id, id)
);
CREATE TABLE IF NOT EXISTS contacts (
    call_id TEXT NOT NULL REFERENCES calls(id),
    id TEXT NOT NULL,
    job_id TEXT NOT NULL,
    raw_text TEXT,
    digits TEXT NOT NULL,
    formatted TEXT,
    role TEXT,
    complete INTEGER,
    confirmation_status TEXT,
    reachability_status TEXT,
    provided_in TEXT,
    readback_in TEXT,
    confirmed_in TEXT,
    PRIMARY KEY (call_id, id)
);
CREATE TABLE IF NOT EXISTS provider_events (
    event_key TEXT PRIMARY KEY,
    call_id TEXT,
    payload TEXT,
    created_at REAL NOT NULL
);
"""


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


class Store:
    def __init__(self, path: Path | str):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        with self.lock:
            self.conn.executescript(SCHEMA)
            self.conn.commit()

    # ---------------------------------------------------------------- helpers
    def _exec(self, sql: str, params=()) -> sqlite3.Cursor:
        with self.lock:
            cur = self.conn.execute(sql, params)
            self.conn.commit()
            return cur

    def _all(self, sql: str, params=()) -> list[dict]:
        with self.lock:
            return [dict(r) for r in self.conn.execute(sql, params).fetchall()]

    def _one(self, sql: str, params=()) -> dict | None:
        rows = self._all(sql, params)
        return rows[0] if rows else None

    # ----------------------------------------------------------------- voices
    def add_voice(self, name: str, provider: str, provider_voice_id: str | None, sample_path: str | None, consent: bool) -> dict:
        vid = new_id("voice")
        self._exec(
            "INSERT INTO voices (id, name, provider, provider_voice_id, sample_path, consent, created_at) VALUES (?,?,?,?,?,?,?)",
            (vid, name, provider, provider_voice_id, sample_path, int(consent), time.time()),
        )
        return self.get_voice(vid)

    def get_voice(self, vid: str) -> dict | None:
        return self._one("SELECT * FROM voices WHERE id=? AND deleted_at IS NULL", (vid,))

    def list_voices(self) -> list[dict]:
        return self._all("SELECT * FROM voices WHERE deleted_at IS NULL ORDER BY created_at DESC")

    def delete_voice(self, vid: str) -> None:
        self._exec("UPDATE voices SET deleted_at=? WHERE id=?", (time.time(), vid))

    # -------------------------------------------------------------- templates
    def save_template(self, name: str, content: dict) -> dict:
        with self.lock:
            row = self._one("SELECT MAX(version) AS v FROM templates WHERE name=?", (name,))
            version = (row["v"] or 0) + 1
            tid = new_id("tpl")
            self._exec(
                "INSERT INTO templates (id, name, version, content, created_at) VALUES (?,?,?,?,?)",
                (tid, name, version, json.dumps(content, ensure_ascii=False), time.time()),
            )
        return self.get_template(tid)

    def get_template(self, tid: str) -> dict | None:
        row = self._one("SELECT * FROM templates WHERE id=?", (tid,))
        if row:
            row["content"] = json.loads(row["content"])
        return row

    def latest_template(self, name: str) -> dict | None:
        row = self._one("SELECT id FROM templates WHERE name=? ORDER BY version DESC LIMIT 1", (name,))
        return self.get_template(row["id"]) if row else None

    def list_templates(self) -> list[dict]:
        return self._all("SELECT id, name, version, created_at FROM templates ORDER BY name, version DESC")

    # ------------------------------------------------------------------- jobs
    def create_job(self, data: dict) -> dict:
        jid = new_id("job")
        now = time.time()
        self._exec(
            """INSERT INTO jobs (id, building_name, building_address, recipient_name, recipient_number,
               requester_name, inquiry_purpose, template_id, voice_id, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (jid, data["building_name"], data["building_address"], data.get("recipient_name"),
             data["recipient_number"], data["requester_name"], data["inquiry_purpose"],
             data.get("template_id"), data.get("voice_id"), now, now),
        )
        return self.get_job(jid)

    def get_job(self, jid: str) -> dict | None:
        return self._one("SELECT * FROM jobs WHERE id=?", (jid,))

    def list_jobs(self) -> list[dict]:
        return self._all("SELECT * FROM jobs ORDER BY created_at DESC")

    def update_job(self, jid: str, **fields) -> None:
        if not fields:
            return
        cols = ", ".join(f"{k}=?" for k in fields)
        self._exec(f"UPDATE jobs SET {cols}, updated_at=? WHERE id=?", (*fields.values(), time.time(), jid))

    # ------------------------------------------------------------------ calls
    def create_call(self, job_id: str, mode: str, template: dict, interpreter: str) -> dict:
        cid = new_id("call")
        self._exec(
            """INSERT INTO calls (id, job_id, mode, template_id, template_version, interpreter, status, started_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (cid, job_id, mode, template["id"], template["version"], interpreter, "in_progress", time.time()),
        )
        return self.get_call(cid)

    def get_call(self, cid: str) -> dict | None:
        return self._one("SELECT * FROM calls WHERE id=?", (cid,))

    def get_call_by_provider_id(self, provider_call_id: str) -> dict | None:
        return self._one("SELECT * FROM calls WHERE provider_call_id=?", (provider_call_id,))

    def update_call(self, cid: str, **fields) -> None:
        if not fields:
            return
        cols = ", ".join(f"{k}=?" for k in fields)
        self._exec(f"UPDATE calls SET {cols} WHERE id=?", (*fields.values(), cid))

    def list_calls(self) -> list[dict]:
        return self._all(
            """SELECT c.*, j.building_name, j.building_address, j.recipient_name, j.recipient_number
               FROM calls c JOIN jobs j ON j.id = c.job_id ORDER BY c.started_at DESC"""
        )

    def active_call_for_job(self, job_id: str) -> dict | None:
        return self._one("SELECT * FROM calls WHERE job_id=? AND status='in_progress'", (job_id,))

    # ------------------------------------------------------------- utterances
    def sync_session(self, session, job_id: str) -> None:
        """대화 세션의 현재 상태를 저장한다. 같은 키로 덮어쓰므로 여러 번 호출해도 안전하다."""
        with self.lock:
            for u in session.utterances:
                self.conn.execute(
                    """INSERT INTO utterances (call_id, id, seq, speaker, text, ts, response_id, status, kind, intent, source)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(call_id, id) DO UPDATE SET status=excluded.status, intent=excluded.intent,
                       source=excluded.source""",
                    (session.call_id, u.id, u.seq, u.speaker, u.text, u.ts, u.response_id, u.status, u.kind,
                     u.intent, u.source),
                )
            for c in session.contacts:
                d = asdict(c)
                self.conn.execute(
                    """INSERT INTO contacts (call_id, id, job_id, raw_text, digits, formatted, role, complete,
                       confirmation_status, reachability_status, provided_in, readback_in, confirmed_in)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(call_id, id) DO UPDATE SET role=excluded.role,
                       confirmation_status=excluded.confirmation_status, readback_in=excluded.readback_in,
                       confirmed_in=excluded.confirmed_in, raw_text=excluded.raw_text""",
                    (session.call_id, c.id, job_id, c.raw_text, c.digits, c.formatted, c.role, int(c.complete),
                     c.confirmation_status, c.reachability_status, json.dumps(d["provided_in"]), c.readback_in,
                     c.confirmed_in),
                )
            self.conn.commit()

    def utterances(self, cid: str) -> list[dict]:
        return self._all("SELECT * FROM utterances WHERE call_id=? ORDER BY seq", (cid,))

    def contacts(self, cid: str) -> list[dict]:
        rows = self._all("SELECT * FROM contacts WHERE call_id=? ORDER BY id", (cid,))
        for r in rows:
            r["provided_in"] = json.loads(r["provided_in"] or "[]")
            r["complete"] = bool(r["complete"])
        return rows

    # ---------------------------------------------------------- idempotency
    def record_event_once(self, event_key: str, call_id: str | None, payload: dict) -> bool:
        """공급사 이벤트 중복 방지. 처음 보는 이벤트면 True."""
        try:
            self._exec(
                "INSERT INTO provider_events (event_key, call_id, payload, created_at) VALUES (?,?,?,?)",
                (event_key, call_id, json.dumps(payload, ensure_ascii=False), time.time()),
            )
            return True
        except sqlite3.IntegrityError:
            return False

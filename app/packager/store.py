"""SQLite persistence.

Core fields and the raw extension subtree are stored separately per
revision: ``core_json`` holds the canonical serialization of the editable
core fields, ``ext_raw`` holds the extension members exactly as received
(a JSON array of [key, raw_value_text] pairs, the raw text itself never
re-serialized).  Processed requests are recorded so that a replayed request
id returns the original revision and summary even after a restart.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

_SCHEMA = """
CREATE TABLE IF NOT EXISTS packages (
    id TEXT PRIMARY KEY,
    current_revision INTEGER NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS revisions (
    package_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    core_json TEXT NOT NULL,
    ext_raw TEXT NOT NULL,
    changed_paths TEXT NOT NULL,
    summary TEXT NOT NULL,
    request_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (package_id, revision)
);
CREATE TABLE IF NOT EXISTS requests (
    request_id TEXT PRIMARY KEY,
    package_id TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    response_json TEXT NOT NULL,
    -- "applied": the verdict created/advanced/replayed a revision (created,
    -- applied, merged, noop); "rejected": the verdict was a 409 refusal.  A
    -- rejected request id stays bound to its payload and may never represent
    -- a different update afterwards.
    outcome TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS adjudications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    package_id TEXT NOT NULL,
    request_id TEXT NOT NULL,
    base_revision INTEGER,
    decision TEXT NOT NULL,
    reason TEXT,
    resulting_revision INTEGER,
    created_at TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def encode_ext(ext_pairs: Sequence[Tuple[str, bytes, bytes]]) -> str:
    return json.dumps(
        [[k, m.decode("utf-8"), v.decode("utf-8")] for k, m, v in ext_pairs],
        ensure_ascii=False,
    )


def decode_ext(ext_raw: str) -> List[Tuple[str, bytes, bytes]]:
    return [(k, m.encode("utf-8"), v.encode("utf-8")) for k, m, v in json.loads(ext_raw)]


class Store:
    def __init__(self, data_dir: str):
        os.makedirs(data_dir, exist_ok=True)
        self._conn = sqlite3.connect(
            os.path.join(data_dir, "packager.db"), check_same_thread=False
        )
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock, self._conn:
            self._conn.executescript(_SCHEMA)
            self._migrate()

    def _migrate(self) -> None:
        """Bring older databases up to the current schema."""
        cols = {
            r["name"]
            for r in self._conn.execute("PRAGMA table_info(requests)")
        }
        if "outcome" not in cols:
            # Every request recorded by older builds carried an applied (or
            # replayed) verdict; rejected request ids were never stored.
            self._conn.execute(
                "ALTER TABLE requests ADD COLUMN outcome TEXT NOT NULL"
                " DEFAULT 'applied'"
            )

    # -- packages / revisions ---------------------------------------------

    def create_package(
        self,
        package_id: str,
        core: Dict[str, Any],
        ext_pairs: Sequence[Tuple[str, bytes, bytes]],
        changed_paths: Sequence[str],
        summary: str,
        request_id: str,
    ) -> None:
        now = _now()
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO packages (id, current_revision, created_at) VALUES (?, 1, ?)",
                (package_id, now),
            )
            self._insert_revision(
                package_id, 1, core, ext_pairs, changed_paths, summary, request_id, now
            )

    def _insert_revision(
        self,
        package_id: str,
        revision: int,
        core: Dict[str, Any],
        ext_pairs: Sequence[Tuple[str, bytes, bytes]],
        changed_paths: Sequence[str],
        summary: str,
        request_id: str,
        now: str,
    ) -> None:
        self._conn.execute(
            "INSERT INTO revisions (package_id, revision, core_json, ext_raw,"
            " changed_paths, summary, request_id, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                package_id,
                revision,
                json.dumps(core, ensure_ascii=False, sort_keys=True),
                encode_ext(ext_pairs),
                json.dumps(list(changed_paths)),
                summary,
                request_id,
                now,
            ),
        )

    def append_revision(
        self,
        package_id: str,
        core: Dict[str, Any],
        ext_pairs: Sequence[Tuple[str, bytes, bytes]],
        changed_paths: Sequence[str],
        summary: str,
        request_id: str,
    ) -> int:
        now = _now()
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT current_revision FROM packages WHERE id = ?", (package_id,)
            ).fetchone()
            revision = row["current_revision"] + 1
            self._insert_revision(
                package_id, revision, core, ext_pairs, changed_paths, summary, request_id, now
            )
            self._conn.execute(
                "UPDATE packages SET current_revision = ? WHERE id = ?",
                (revision, package_id),
            )
        return revision

    def get_package(self, package_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM packages WHERE id = ?", (package_id,)
            ).fetchone()
        return dict(row) if row else None

    def list_packages(self) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT p.id, p.current_revision, p.created_at, r.summary"
                " FROM packages p JOIN revisions r"
                "   ON r.package_id = p.id AND r.revision = p.current_revision"
                " ORDER BY p.created_at"
            ).fetchall()
        return [dict(r) for r in rows]

    def get_revision(
        self, package_id: str, revision: int
    ) -> Optional[Tuple[Dict[str, Any], List[Tuple[str, bytes, bytes]], List[str], str]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM revisions WHERE package_id = ? AND revision = ?",
                (package_id, revision),
            ).fetchone()
        if not row:
            return None
        return (
            json.loads(row["core_json"]),
            decode_ext(row["ext_raw"]),
            json.loads(row["changed_paths"]),
            row["summary"],
        )

    def changed_paths_between(self, package_id: str, lo: int, hi: int) -> List[str]:
        """Union of changed canonical paths of revisions lo+1 .. hi."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT changed_paths FROM revisions"
                " WHERE package_id = ? AND revision > ? AND revision <= ?",
                (package_id, lo, hi),
            ).fetchall()
        out: List[str] = []
        for row in rows:
            for path in json.loads(row["changed_paths"]):
                if path not in out:
                    out.append(path)
        return out

    # -- idempotent requests ----------------------------------------------

    def find_request(self, request_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM requests WHERE request_id = ?", (request_id,)
            ).fetchone()
        return dict(row) if row else None

    def record_request(
        self, request_id: str, package_id: str, request_hash: str, response: Dict[str, Any]
    ) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO requests (request_id, package_id, request_hash,"
                " response_json, outcome, created_at) VALUES (?, ?, ?, ?, 'applied', ?)",
                (
                    request_id,
                    package_id,
                    request_hash,
                    json.dumps(response, ensure_ascii=False),
                    _now(),
                ),
            )

    def record_rejected_request(
        self,
        request_id: str,
        package_id: str,
        request_hash: str,
        response: Dict[str, Any],
    ) -> None:
        """Bind a rejected request id to its payload.

        A rejection is a final business verdict: the id is recorded so that
        later reuse with the same payload replays the refusal and reuse with a
        different payload fails with ``request_id_reuse``.
        """
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO requests (request_id, package_id, request_hash,"
                " response_json, outcome, created_at) VALUES (?, ?, ?, ?, 'rejected', ?)",
                (
                    request_id,
                    package_id,
                    request_hash,
                    json.dumps(response, ensure_ascii=False),
                    _now(),
                ),
            )

    # -- adjudications ------------------------------------------------------

    def record_adjudication(
        self,
        package_id: str,
        request_id: str,
        base_revision: Optional[int],
        decision: str,
        reason: Optional[str],
        resulting_revision: Optional[int],
    ) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO adjudications (package_id, request_id, base_revision,"
                " decision, reason, resulting_revision, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    package_id,
                    request_id,
                    base_revision,
                    decision,
                    reason,
                    resulting_revision,
                    _now(),
                ),
            )

    def list_adjudications(self, package_id: str) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM adjudications WHERE package_id = ? ORDER BY id",
                (package_id,),
            ).fetchall()
        return [dict(r) for r in rows]

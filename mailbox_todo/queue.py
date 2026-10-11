"""Persistent queue metadata. Task text stays in Mailbox.org and is never cached here."""

from __future__ import annotations

import contextlib
import os
import sqlite3
import time
from pathlib import Path
from typing import Iterator

from . import ical
from .dav import CalDAV, DavError, Resource

MAX_ATTEMPTS = 3
LEASE_SECONDS = 20 * 60


class QueueStore:
    def __init__(self, db_path: str | Path):
        self.path = Path(db_path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.path.parent, 0o700)
        self._init()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 10000")
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def _init(self) -> None:
        with contextlib.closing(self._connect()) as conn:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS resources (
                    collection TEXT NOT NULL,
                    href TEXT NOT NULL,
                    etag TEXT NOT NULL,
                    present INTEGER NOT NULL DEFAULT 1,
                    PRIMARY KEY (collection, href)
                );
                CREATE TABLE IF NOT EXISTS tasks (
                    collection TEXT NOT NULL,
                    uid TEXT NOT NULL,
                    href TEXT NOT NULL,
                    etag TEXT NOT NULL,
                    state TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    next_try REAL NOT NULL DEFAULT 0,
                    lease_until REAL NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    approval_id TEXT,
                    approval_scope TEXT,
                    PRIMARY KEY (collection, uid)
                );
                CREATE INDEX IF NOT EXISTS tasks_ready
                    ON tasks (collection, state, next_try, created_at);
                """
            )
        os.chmod(self.path, 0o600)

    @contextlib.contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def is_initialized(self, collection: str = "Hermes") -> bool:
        with contextlib.closing(self._connect()) as conn:
            row = conn.execute("SELECT value FROM meta WHERE key=?", (f"baseline:{collection}",)).fetchone()
            return bool(row and row["value"] == "1")

    def get_meta(self, key: str, default: str | None = None) -> str | None:
        with contextlib.closing(self._connect()) as conn:
            row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def set_meta(self, key: str, value: str) -> None:
        with self._transaction() as conn:
            conn.execute(
                "INSERT INTO meta(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )

    def baseline(self, resources: list[Resource], collection: str = "Hermes") -> None:
        """Record existing href/ETag pairs without downloading task bodies."""
        now = time.time()
        with self._transaction() as conn:
            conn.executemany(
                """INSERT INTO resources(collection, href, etag, present)
                   VALUES(?,?,?,1)
                   ON CONFLICT(collection,href) DO UPDATE SET etag=excluded.etag,present=1""",
                [(collection, r.href, r.etag) for r in resources],
            )
            conn.execute(
                "INSERT INTO meta(key,value) VALUES(?, '1') ON CONFLICT(key) DO UPDATE SET value='1'",
                (f"baseline:{collection}",),
            )
            conn.execute(
                "INSERT INTO meta(key,value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (f"last_scan:{collection}", str(now)),
            )

    def _task(self, collection: str, uid: str):
        with contextlib.closing(self._connect()) as conn:
            return conn.execute(
                "SELECT * FROM tasks WHERE collection=? AND uid=?", (collection, uid)
            ).fetchone()

    def scan(self, dav: CalDAV, collection: str = "Hermes") -> dict[str, int | bool]:
        """Index only metadata and fetch bodies for new resources or unknown UIDs."""
        current = dav.list_resources(collection)
        if not self.is_initialized(collection):
            self.baseline(current, collection)
            return {"baselined": True, "new": 0}

        now = time.time()
        present = {r.href: r.etag for r in current}
        with self._transaction() as conn:
            existing = conn.execute(
                "SELECT href,etag,present FROM resources WHERE collection=?", (collection,)
            ).fetchall()
            old = {row["href"]: row for row in existing}
            gone = set(old) - set(present)
            for href in gone:
                conn.execute(
                    "UPDATE resources SET present=0 WHERE collection=? AND href=?",
                    (collection, href),
                )
                conn.execute(
                    """UPDATE tasks SET state='removed', lease_until=0, updated_at=?
                       WHERE collection=? AND href=? AND state NOT IN ('completed','removed')""",
                    (now, collection, href),
                )
            conn.execute(
                "INSERT INTO meta(key,value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (f"last_scan:{collection}", str(now)),
            )

        new_count = 0
        for resource in current:
            row = old.get(resource.href)
            is_new_resource = row is None
            known_uid = self._uid_for_href(collection, resource.href)
            # Existing resources are fetched only after ETag changes, to learn
            # their UID once. They are recorded as baseline and never dispatched.
            changed = row is not None and row["etag"] != resource.etag
            if not is_new_resource and not changed and known_uid:
                continue
            if not is_new_resource and not changed and not known_uid:
                continue
            payload, fetched_etag = dav.get_vtodo(collection, resource.href)
            if fetched_etag != resource.etag:
                # A concurrent iPhone edit raced the metadata listing. Leave it
                # untouched and let the next poll observe a stable ETag.
                continue
            uid = ical.task_identity(payload)
            status = ical.task_status(payload)
            previous = self._task(collection, uid)
            with self._transaction() as conn:
                conn.execute(
                    """INSERT INTO resources(collection,href,etag,present) VALUES(?,?,?,1)
                       ON CONFLICT(collection,href) DO UPDATE SET etag=excluded.etag,present=1""",
                    (collection, resource.href, fetched_etag),
                )
            if previous:
                self._update_seen_task(collection, uid, resource.href, fetched_etag, status)
                continue
            state = "baseline" if not is_new_resource else (
                "completed" if status == "COMPLETED" else
                "removed" if status == "CANCELLED" else
                "queued"
            )
            self._insert_task(collection, uid, resource.href, fetched_etag, state, now)
            if state == "queued":
                new_count += 1
        self._recover_expired(collection, dav)
        return {"baselined": False, "new": new_count}

    def _uid_for_href(self, collection: str, href: str) -> str | None:
        with contextlib.closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT uid FROM tasks WHERE collection=? AND href=? ORDER BY created_at LIMIT 1",
                (collection, href),
            ).fetchone()
        return row["uid"] if row else None

    def _insert_task(self, collection: str, uid: str, href: str, etag: str, state: str, now: float) -> None:
        with self._transaction() as conn:
            conn.execute(
                """INSERT OR IGNORE INTO tasks
                   (collection,uid,href,etag,state,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?)""",
                (collection, uid, href, etag, state, now, now),
            )

    def _update_seen_task(self, collection: str, uid: str, href: str, etag: str, status: str) -> None:
        now = time.time()
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT state FROM tasks WHERE collection=? AND uid=?", (collection, uid)
            ).fetchone()
            if not row:
                return
            state = row["state"]
            if status == "COMPLETED":
                state = "completed"
            elif status == "CANCELLED":
                state = "removed"
            # A completed/removed task is terminal; external edits never reopen it.
            if row["state"] in {"completed", "removed"}:
                state = row["state"]
            if row["state"] in {"completed", "removed"} or href != self._href_for_uid(conn, collection, uid):
                conn.execute(
                    """UPDATE tasks SET state=?,lease_until=0,updated_at=?
                       WHERE collection=? AND uid=?""",
                    (state, now, collection, uid),
                )
            else:
                conn.execute(
                    """UPDATE tasks SET etag=?,state=?,
                       lease_until=CASE WHEN ? IN ('completed','removed') THEN 0 ELSE lease_until END,
                       updated_at=?
                       WHERE collection=? AND uid=?""",
                    (etag, state, state, now, collection, uid),
                )

    @staticmethod
    def _href_for_uid(conn: sqlite3.Connection, collection: str, uid: str) -> str | None:
        row = conn.execute(
            "SELECT href FROM tasks WHERE collection=? AND uid=?", (collection, uid)
        ).fetchone()
        return row["href"] if row else None

    def _recover_expired(self, collection: str, dav: CalDAV) -> None:
        now = time.time()
        with contextlib.closing(self._connect()) as conn:
            expired = conn.execute(
                """SELECT uid,href,etag,state,attempts FROM tasks
                   WHERE collection=? AND state IN ('dispatching','in_progress')
                   AND lease_until>0 AND lease_until<?""",
                (collection, now),
            ).fetchall()
        for row in expired:
            # If the managed in-progress marker reached DAV, the agent may have
            # done work before a crash. Hold for human review rather than replay.
            needs_review = False
            try:
                payload, etag = dav.get_vtodo(collection, row["href"])
                note = ical.get_text(payload, "DESCRIPTION")
                needs_review = "[Hermes ToDo Pilot]" in note and "Status: In Bearbeitung" in note
            except DavError:
                needs_review = True
            if needs_review or row["attempts"] >= MAX_ATTEMPTS:
                target_state = "needs_review" if needs_review else "failed"
                self.set_state(collection, row["uid"], target_state, lease_until=0)
            else:
                self.set_state(
                    collection,
                    row["uid"],
                    "queued",
                    next_try=now + min(60 * (2**row["attempts"]), 15 * 60),
                    lease_until=0,
                )

    def claim_next(self, collection: str = "Hermes") -> str | None:
        now = time.time()
        with self._transaction() as conn:
            active = conn.execute(
                """SELECT 1 FROM tasks WHERE collection=?
                   AND state IN ('dispatching','in_progress') AND lease_until>? LIMIT 1""",
                (collection, now),
            ).fetchone()
            if active:
                return None
            row = conn.execute(
                """SELECT uid,attempts FROM tasks
                   WHERE collection=? AND state='queued' AND next_try<=? AND attempts<?
                   ORDER BY created_at,uid LIMIT 1""",
                (collection, now, MAX_ATTEMPTS),
            ).fetchone()
            if not row:
                return None
            conn.execute(
                """UPDATE tasks SET state='dispatching',attempts=attempts+1,
                   lease_until=?,updated_at=? WHERE collection=? AND uid=? AND state='queued'""",
                (now + LEASE_SECONDS, now, collection, row["uid"]),
            )
            return row["uid"]

    def task_record(self, uid: str, collection: str = "Hermes"):
        return self._task(collection, uid)

    def set_state(
        self,
        collection: str,
        uid: str,
        state: str,
        *,
        next_try: float | None = None,
        lease_until: float | None = None,
    ) -> bool:
        now = time.time()
        values = ["state=?", "updated_at=?"]
        params: list[object] = [state, now]
        if next_try is not None:
            values.append("next_try=?")
            params.append(next_try)
        if lease_until is not None:
            values.append("lease_until=?")
            params.append(lease_until)
        params.extend([collection, uid])
        with self._transaction() as conn:
            cur = conn.execute(
                f"UPDATE tasks SET {','.join(values)} WHERE collection=? AND uid=?",
                params,
            )
            return cur.rowcount == 1

    def update_etag(self, collection: str, uid: str, etag: str, state: str | None = None) -> bool:
        now = time.time()
        with self._transaction() as conn:
            if state is None:
                cur = conn.execute(
                    """UPDATE tasks SET etag=?,updated_at=?
                       WHERE collection=? AND uid=? AND state NOT IN ('completed','removed')""",
                    (etag, now, collection, uid),
                )
            else:
                cur = conn.execute(
                    """UPDATE tasks SET etag=?,state=?,updated_at=?
                       WHERE collection=? AND uid=? AND state NOT IN ('completed','removed')""",
                    (etag, state, now, collection, uid),
                )
            return cur.rowcount == 1

    def begin(self, uid: str, etag: str, collection: str = "Hermes") -> bool:
        now = time.time()
        with self._transaction() as conn:
            cur = conn.execute(
                """UPDATE tasks SET state='in_progress',etag=?,lease_until=?,updated_at=?
                   WHERE collection=? AND uid=? AND state IN ('dispatching','in_progress')""",
                (etag, now + LEASE_SECONDS, now, collection, uid),
            )
            return cur.rowcount == 1

    def finish(self, uid: str, etag: str, collection: str = "Hermes") -> bool:
        now = time.time()
        with self._transaction() as conn:
            cur = conn.execute(
                """UPDATE tasks SET state='completed',etag=?,lease_until=0,updated_at=?
                   WHERE collection=? AND uid=? AND state NOT IN ('removed','needs_review')""",
                (etag, now, collection, uid),
            )
            return cur.rowcount == 1

    def wait_for_approval(self, uid: str, approval_id: str, scope: str, collection: str = "Hermes") -> bool:
        now = time.time()
        with self._transaction() as conn:
            cur = conn.execute(
                """UPDATE tasks SET state='waiting_approval',approval_id=?,approval_scope=?,
                   lease_until=0,updated_at=?
                   WHERE collection=? AND uid=? AND state IN ('dispatching','in_progress')""",
                (approval_id, scope, now, collection, uid),
            )
            return cur.rowcount == 1

    def approve(self, approval_id: str, collection: str = "Hermes") -> str | None:
        now = time.time()
        with self._transaction() as conn:
            row = conn.execute(
                """SELECT uid FROM tasks WHERE collection=? AND approval_id=?
                   AND state='waiting_approval' AND updated_at>?""",
                (collection, approval_id, now - 24 * 60 * 60),
            ).fetchone()
            if not row:
                return None
            conn.execute(
                """UPDATE tasks SET state='queued',next_try=0,approval_id=NULL,
                   updated_at=? WHERE collection=? AND uid=?""",
                (now, collection, row["uid"]),
            )
            return row["uid"]

    def health(self) -> tuple[int, float, str]:
        with contextlib.closing(self._connect()) as conn:
            rows = {row["key"]: row["value"] for row in conn.execute(
                "SELECT key,value FROM meta WHERE key IN ('health:failures','health:next_poll','health:state')"
            )}
        return (
            int(rows.get("health:failures", "0")),
            float(rows.get("health:next_poll", "0")),
            rows.get("health:state", "ok"),
        )

    def record_failure(self, category: str) -> str | None:
        now = time.time()
        with self._transaction() as conn:
            row = conn.execute("SELECT value FROM meta WHERE key='health:failures'").fetchone()
            count = min(12, int(row["value"]) + 1 if row else 1)
            old_state_row = conn.execute("SELECT value FROM meta WHERE key='health:state'").fetchone()
            old_state = old_state_row["value"] if old_state_row else "ok"
            delay = min(5 * 60 * (2 ** min(count - 1, 4)), 60 * 60)
            new_state = "offline" if count >= 3 else "degraded"
            for key, value in (
                ("health:failures", str(count)),
                ("health:next_poll", str(now + delay)),
                ("health:state", new_state),
                ("health:last_error_category", category[:40]),
            ):
                conn.execute(
                    "INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (key, value),
                )
            return "connection_lost" if new_state == "offline" and old_state != "offline" else None

    def record_success(self) -> str | None:
        with self._transaction() as conn:
            row = conn.execute("SELECT value FROM meta WHERE key='health:state'").fetchone()
            was_offline = bool(row and row["value"] == "offline")
            for key, value in (
                ("health:failures", "0"),
                ("health:next_poll", "0"),
                ("health:state", "ok"),
            ):
                conn.execute(
                    "INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (key, value),
                )
            return "connection_recovered" if was_offline else None


class PilotPoller:
    def __init__(self, dav: CalDAV, queue: QueueStore):
        self.dav = dav
        self.queue = queue

    def tick(self) -> dict[str, object]:
        failures, next_poll, state = self.queue.health()
        if time.time() < next_poll:
            return {"event": None}
        try:
            scan = self.queue.scan(self.dav, "Hermes")
            recovery = self.queue.record_success()
            if scan["baselined"]:
                return {"event": None, "baselined": True}
            if recovery:
                return {"event": recovery}
            uid = self.queue.claim_next("Hermes")
            if uid:
                return {"event": "task", "uid": uid}
            return {"event": None}
        except DavError as exc:
            category = "authentication" if exc.status in {401, 403} else (
                f"http_{exc.status}" if exc.status else "connection"
            )
            event = self.queue.record_failure(category)
            return {"event": event, "error_category": category}

import hashlib
import json
import sqlite3
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any


class Conflict(Exception):
    pass


def canonical(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class Store:
    def __init__(self, directory: Path):
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = directory / "gateway.sqlite3"
        with self.db() as db:
            if db.execute("PRAGMA user_version").fetchone()[0] > 1:
                raise RuntimeError("Database schema is newer than this gateway version")
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS deliveries (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    delivery_id TEXT NOT NULL UNIQUE,
                    dedup_key TEXT NOT NULL UNIQUE,
                    fingerprint TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    chat_id TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    total_attempts INTEGER NOT NULL DEFAULT 0,
                    next_attempt_at REAL NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    last_error TEXT,
                    result_message_id TEXT
                );
                CREATE INDEX IF NOT EXISTS queue_head
                    ON deliveries(kind, chat_id, seq, status);
                CREATE INDEX IF NOT EXISTS queue_due ON deliveries(status,next_attempt_at);
                CREATE TABLE IF NOT EXISTS message_chats (
                    message_id TEXT PRIMARY KEY, chat_id TEXT NOT NULL
                );
                PRAGMA user_version=1;
            """)
        self.path.chmod(0o600)

    @contextmanager
    def db(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=2)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA synchronous=FULL")
        try:
            with db:
                yield db
        finally:
            db.close()

    def recover(self) -> None:
        # Only called by the lock-owning parent, before workers/receiver start.
        with self.db() as db:
            db.execute("UPDATE deliveries SET status='pending' WHERE status='processing'")

    def enqueue(self, kind: str, chat_id: str, key: str, payload: dict[str, Any]) -> dict[str, Any]:
        encoded = canonical(payload)
        fingerprint = hashlib.sha256(encoded.encode()).hexdigest()
        now = time.time()
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT * FROM deliveries WHERE dedup_key=?", (key,)).fetchone()
            if old:
                if kind != "webhook" and old["fingerprint"] != fingerprint:
                    raise Conflict("idempotency_conflict")
                return dict(old)
            delivery_id = str(uuid.uuid4())
            db.execute(
                """INSERT INTO deliveries
                (delivery_id,dedup_key,fingerprint,kind,chat_id,payload,
                 next_attempt_at,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?)""",
                (delivery_id, key, fingerprint, kind, chat_id, encoded, now, now, now),
            )
            if kind == "webhook":
                db.execute(
                    "INSERT OR IGNORE INTO message_chats VALUES (?,?)",
                    (payload["message_id"], chat_id),
                )
            return dict(
                db.execute(
                    "SELECT * FROM deliveries WHERE delivery_id=?", (delivery_id,)
                ).fetchone()
            )

    def claim(self) -> dict[str, Any] | None:
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                """SELECT d.* FROM deliveries d
                WHERE d.status='pending' AND d.next_attempt_at<=?
                AND NOT EXISTS (SELECT 1 FROM deliveries p
                    WHERE p.chat_id=d.chat_id AND
                    (CASE WHEN p.kind='webhook' THEN 0 ELSE 1 END)=
                    (CASE WHEN d.kind='webhook' THEN 0 ELSE 1 END)
                    AND p.seq<d.seq AND p.status!='succeeded')
                ORDER BY d.seq LIMIT 1""",
                (time.time(),),
            ).fetchone()
            if row is None:
                return None
            db.execute(
                """UPDATE deliveries SET status='processing',attempts=attempts+1,
                total_attempts=total_attempts+1,updated_at=? WHERE delivery_id=?""",
                (time.time(), row["delivery_id"]),
            )
            return dict(
                db.execute(
                    "SELECT * FROM deliveries WHERE delivery_id=?", (row["delivery_id"],)
                ).fetchone()
            )

    def finish(
        self,
        delivery_id: str,
        *,
        error: str | None = None,
        delay: float = 0,
        dead: bool = False,
        message_id: str | None = None,
    ) -> None:
        with self.db() as db:
            db.execute(
                """UPDATE deliveries SET status=?,last_error=?,next_attempt_at=?,
                updated_at=?,result_message_id=? WHERE delivery_id=?""",
                (
                    "dead" if dead else "pending" if error else "succeeded",
                    error,
                    time.time() + delay,
                    time.time(),
                    message_id,
                    delivery_id,
                ),
            )
            if message_id:
                db.execute(
                    """INSERT OR REPLACE INTO message_chats
                    SELECT ?,chat_id FROM deliveries WHERE delivery_id=?""",
                    (message_id, delivery_id),
                )

    def get(self, delivery_id: str) -> dict[str, Any] | None:
        with self.db() as db:
            row = db.execute(
                "SELECT * FROM deliveries WHERE delivery_id=?", (delivery_id,)
            ).fetchone()
            return dict(row) if row else None

    def by_key(self, key: str) -> dict[str, Any] | None:
        with self.db() as db:
            row = db.execute("SELECT * FROM deliveries WHERE dedup_key=?", (key,)).fetchone()
            return dict(row) if row else None

    def chat_for(self, message_id: str) -> str | None:
        with self.db() as db:
            row = db.execute(
                "SELECT chat_id FROM message_chats WHERE message_id=?", (message_id,)
            ).fetchone()
            return row[0] if row else None

    def replay(self, delivery_id: str) -> bool:
        with self.db() as db:
            return (
                db.execute(
                    """UPDATE deliveries SET status='pending',attempts=0,
                next_attempt_at=?,updated_at=? WHERE delivery_id=? AND status='dead'""",
                    (time.time(), time.time(), delivery_id),
                ).rowcount
                == 1
            )

    def stats(self) -> dict[str, Any]:
        with self.db() as db:
            counts = dict(db.execute("SELECT status,COUNT(*) FROM deliveries GROUP BY status"))
            last = db.execute("""SELECT delivery_id,status,updated_at,last_error
                FROM deliveries WHERE total_attempts>0
                ORDER BY updated_at DESC LIMIT 1""").fetchone()
            dead = [
                row[0]
                for row in db.execute(
                    "SELECT delivery_id FROM deliveries WHERE status='dead' ORDER BY seq LIMIT 100"
                )
            ]
            return {
                "queue_count": counts.get("pending", 0) + counts.get("processing", 0),
                "dead_count": counts.get("dead", 0),
                "counts": counts,
                "dead_delivery_ids": dead,
                "latest_delivery": dict(last) if last else None,
            }

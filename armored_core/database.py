from __future__ import annotations
import json
import sqlite3
from pathlib import Path
from .models import Item, State

class Database:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self._init()

    def _init(self) -> None:
        self.conn.executescript("""
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS items (
            content_id TEXT PRIMARY KEY,
            telegram_message_id TEXT NOT NULL UNIQUE,
            source_id TEXT NOT NULL DEFAULT 'telegram',
            topic_id INTEGER,
            topic_name TEXT,
            original_url TEXT,
            state TEXT NOT NULL,
            original_path TEXT NOT NULL,
            original_sha256 TEXT,
            working_path TEXT,
            result_path TEXT,
            affiliate_name TEXT,
            affiliate_url TEXT,
            publication_caption TEXT,
            affiliate_urls_json TEXT NOT NULL DEFAULT '[]',
            attempts INTEGER NOT NULL DEFAULT 0,
            recovery_count INTEGER NOT NULL DEFAULT 0,
            cleanup_completed INTEGER NOT NULL DEFAULT 0,
            last_error TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS state_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            content_id TEXT NOT NULL,
            old_state TEXT,
            new_state TEXT NOT NULL,
            reason TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS publications (
            content_id TEXT PRIMARY KEY,
            idempotency_key TEXT NOT NULL UNIQUE,
            published_message_id TEXT,
            confirmed INTEGER NOT NULL DEFAULT 0,
            verification_status TEXT NOT NULL DEFAULT 'PENDING',
            destination_chat_id TEXT,
            destination_topic_id INTEGER,
            verified_at TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS sync_state (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS sync_topics (
            topic_id INTEGER PRIMARY KEY,
            topic_name TEXT NOT NULL,
            last_seen_message_id INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS runtime_locks (
            name TEXT PRIMARY KEY,
            pid INTEGER NOT NULL,
            started_at TEXT NOT NULL,
            heartbeat_at TEXT NOT NULL
        );
        """)
        self.conn.commit()
        self._migrate_columns()

    def _migrate_columns(self) -> None:
        # Legacy Vision evidence tables, when present in an existing database,
        # are preserved. Migrations must never destroy historical audit data.
        migrations = {
            "items": [
                ("source_id", "ALTER TABLE items ADD COLUMN source_id TEXT NOT NULL DEFAULT 'telegram'"),
                ("topic_id", "ALTER TABLE items ADD COLUMN topic_id INTEGER"),
                ("topic_name", "ALTER TABLE items ADD COLUMN topic_name TEXT"),
                ("original_url", "ALTER TABLE items ADD COLUMN original_url TEXT"),
                ("original_sha256", "ALTER TABLE items ADD COLUMN original_sha256 TEXT"),
                ("publication_caption", "ALTER TABLE items ADD COLUMN publication_caption TEXT"),
                ("ia_context_json", "ALTER TABLE items ADD COLUMN ia_context_json TEXT NOT NULL DEFAULT '{}'"),
                ("affiliate_urls_json", "ALTER TABLE items ADD COLUMN affiliate_urls_json TEXT NOT NULL DEFAULT '[]'"),
                ("attempts", "ALTER TABLE items ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0"),
                ("recovery_count", "ALTER TABLE items ADD COLUMN recovery_count INTEGER NOT NULL DEFAULT 0"),
                ("cleanup_completed", "ALTER TABLE items ADD COLUMN cleanup_completed INTEGER NOT NULL DEFAULT 0"),
            ],
            "publications": [
                ("verification_status", "ALTER TABLE publications ADD COLUMN verification_status TEXT NOT NULL DEFAULT 'PENDING'"),
                ("destination_chat_id", "ALTER TABLE publications ADD COLUMN destination_chat_id TEXT"),
                ("destination_topic_id", "ALTER TABLE publications ADD COLUMN destination_topic_id INTEGER"),
                ("verified_at", "ALTER TABLE publications ADD COLUMN verified_at TEXT"),
            ],
        }
        for table, columns in migrations.items():
            existing = {
                row[1] for row in self.conn.execute(f"PRAGMA table_info({table})").fetchall()
            }
            for name, sql in columns:
                if name not in existing:
                    self.conn.execute(sql)
        self.conn.commit()

    def sync_mode(self) -> str:
        row = self.conn.execute(
            "SELECT value FROM sync_state WHERE key='mode'"
        ).fetchone()
        return str(row["value"]) if row else "CATCH_UP"

    def set_sync_mode(self, mode: str) -> None:
        self.conn.execute(
            "INSERT INTO sync_state(key,value) VALUES('mode',?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=CURRENT_TIMESTAMP",
            (str(mode),),
        )
        self.conn.commit()

    def historical_complete(self) -> bool:
        return self.sync_mode() == "LIVE"

    def has_sync_checkpoints(self) -> bool:
        row = self.conn.execute("SELECT 1 FROM sync_topics LIMIT 1").fetchone()
        return row is not None

    def complete_historical_sync(self) -> None:
        self.set_sync_mode("LIVE")

    def sync_topic_checkpoint(self, topic_id: int) -> int:
        row = self.conn.execute(
            "SELECT last_seen_message_id FROM sync_topics WHERE topic_id=?",
            (int(topic_id),),
        ).fetchone()
        return int(row["last_seen_message_id"]) if row else 0

    def set_sync_topic_checkpoint(self, topic_id: int, topic_name: str, message_id: int) -> None:
        self.conn.execute(
            "INSERT INTO sync_topics(topic_id,topic_name,last_seen_message_id) VALUES(?,?,?) "
            "ON CONFLICT(topic_id) DO UPDATE SET topic_name=excluded.topic_name, "
            "last_seen_message_id=MAX(sync_topics.last_seen_message_id, excluded.last_seen_message_id), "
            "updated_at=CURRENT_TIMESTAMP",
            (int(topic_id), str(topic_name), int(message_id)),
        )
        self.conn.commit()

    def reserve_item(
        self,
        telegram_message_id: str,
        source_id: str = "telegram",
        topic_id: int | None = None,
        topic_name: str | None = None,
        original_url: str | None = None,
        original_path: Path | None = None,
    ) -> str:
        content_id = str(telegram_message_id)
        cur = self.conn.execute(
            "INSERT INTO items (content_id,telegram_message_id,source_id,topic_id,topic_name,original_url,state,original_path) VALUES (?,?,?,?,?,?,?,?)",
            (
                content_id, telegram_message_id, source_id, topic_id, topic_name, original_url,
                State.RECEIVED.value, str(original_path or ""),
            ),
        )
        item_id = content_id
        self.conn.execute(
            "INSERT INTO state_events (content_id,new_state,reason) VALUES (?,?,?)",
            (item_id, State.RECEIVED.value, "ingest-reserved"),
        )
        self.conn.commit()
        return item_id

    def repair_original_path(self, item_id: str, path: Path) -> None:
        """Repair a legacy/incomplete row without changing its pipeline state."""
        self.conn.execute(
            "UPDATE items SET original_path=?, updated_at=CURRENT_TIMESTAMP WHERE content_id=?",
            (str(path), str(item_id)),
        )
        self.conn.commit()

    def finalize_original_path(self, item_id: str, path: Path, sha256: str) -> None:
        self.conn.execute(
            "UPDATE items SET original_path=?, original_sha256=?, updated_at=CURRENT_TIMESTAMP WHERE content_id=?",
            (str(path), sha256, item_id),
        )
        self.conn.commit()

    def rollback_ingest(self) -> None:
        self.conn.rollback()

    def create_item(
        self, telegram_message_id: str, original_path: Path,
        source_id: str = "telegram", topic_id: int | None = None,
        topic_name: str | None = None, original_url: str | None = None,
    ) -> str:
        content_id = str(telegram_message_id)
        self.conn.execute(
            "INSERT INTO items (content_id,telegram_message_id,source_id,topic_id,topic_name,original_url,state,original_path) VALUES (?,?,?,?,?,?,?,?)",
            (content_id, telegram_message_id, source_id, topic_id, topic_name, original_url,
             State.RECEIVED.value, str(original_path)),
        )
        self.conn.execute(
            "INSERT INTO state_events (content_id,new_state,reason) VALUES (?,?,?)",
            (content_id, State.RECEIVED.value, "ingest-reserved"),
        )
        self.conn.commit()
        return content_id

    @staticmethod
    def _optional_path(value) -> Path | None:
        if value is None:
            return None
        text = str(value).strip()
        if not text or text.lower() == "none":
            return None
        return Path(text)

    def get(self, item_id: str) -> Item:
        content_id = str(item_id)
        row = self.conn.execute("SELECT * FROM items WHERE content_id=?", (content_id,)).fetchone()
        if row is None:
            raise KeyError(item_id)
        return Item(
            row["content_id"], row["telegram_message_id"], State(row["state"]),
            Path(row["original_path"]).parent, Path(row["original_path"]),
            self._optional_path(row["working_path"]),
            self._optional_path(row["result_path"]),
            row["affiliate_name"], row["affiliate_url"],
            row["source_id"], row["original_url"], row["topic_id"], row["topic_name"],
            row["original_sha256"], row["attempts"], row["recovery_count"], bool(row["cleanup_completed"]),
            row["publication_caption"],
            tuple(json.loads(row["affiliate_urls_json"] or "[]")),
            json.loads(row["ia_context_json"] or "{}"),
        )

    def last_state_event(self, item_id: str):
        row = self.conn.execute(
            "SELECT old_state, new_state, reason, created_at "
            "FROM state_events WHERE content_id=? ORDER BY id DESC LIMIT 1",
            (str(item_id),),
        ).fetchone()
        return row

    def last_error(self, item_id: str) -> str | None:
        row = self.conn.execute(
            "SELECT last_error FROM items WHERE content_id=?", (str(item_id),)
        ).fetchone()
        if row is None:
            raise KeyError(item_id)
        value = row["last_error"]
        return str(value) if value is not None else None

    def record_attempt(self, item_id: str) -> None:
        self.conn.execute(
            "UPDATE items SET attempts=attempts+1, updated_at=CURRENT_TIMESTAMP WHERE content_id=?",
            (item_id,),
        )
        self.conn.commit()

    def record_recovery(self, item_id: str) -> None:
        self.conn.execute(
            "UPDATE items SET recovery_count=recovery_count+1, updated_at=CURRENT_TIMESTAMP WHERE content_id=?",
            (item_id,),
        )
        self.conn.commit()

    def transition(self, item_id: str, new_state: State, reason: str = "") -> None:
        old = self.get(item_id).state
        self.conn.execute(
            "UPDATE items SET state=?, last_error=NULL, updated_at=CURRENT_TIMESTAMP WHERE content_id=?",
            (new_state.value, item_id),
        )
        self.conn.execute(
            "INSERT INTO state_events (content_id,old_state,new_state,reason) VALUES (?,?,?,?)",
            (item_id, old.value, new_state.value, reason),
        )
        self.conn.commit()

    def mark_vision_waiting(self, item_id: str, reason: str) -> None:
        old = self.get(item_id).state
        self.conn.execute(
            "UPDATE items SET state=?, last_error=?, updated_at=CURRENT_TIMESTAMP WHERE content_id=?",
            (State.WAITING_VISION.value, reason, item_id),
        )
        self.conn.execute(
            "INSERT INTO state_events (content_id,old_state,new_state,reason) VALUES (?,?,?,?)",
            (item_id, old.value, State.WAITING_VISION.value, reason),
        )
        self.conn.commit()

    def set_vision(
        self,
        item_id: str,
        affiliate_name: str,
        affiliate_url: str,
        affiliate_urls=(),
        publication_caption: str | None = None,
        ia_context: dict | None = None,
    ) -> None:
        links = [str(link).strip() for link in (affiliate_urls or ()) if str(link).strip()]
        if not links and affiliate_url:
            links = [str(affiliate_url).strip()]

        self.conn.execute(
            "UPDATE items SET affiliate_name=?, affiliate_url=?, publication_caption=?, "
            "affiliate_urls_json=?, ia_context_json=?, updated_at=CURRENT_TIMESTAMP WHERE content_id=?",
            (
                affiliate_name,
                affiliate_url,
                publication_caption,
                json.dumps(list(dict.fromkeys(links)), ensure_ascii=False),
                json.dumps(ia_context or {}, ensure_ascii=False, default=str),
                item_id,
            ),
        )
        self.conn.commit()


    def set_ia_context(self, item_id: str, context: dict | None) -> None:
        self.conn.execute(
            "UPDATE items SET ia_context_json=?, updated_at=CURRENT_TIMESTAMP WHERE content_id=?",
            (json.dumps(context or {}, ensure_ascii=False, default=str), str(item_id)),
        )
        self.conn.commit()

    def set_caption(self, item_id: str, caption: str) -> None:
        self.conn.execute(
            "UPDATE items SET publication_caption=?, updated_at=CURRENT_TIMESTAMP WHERE content_id=?",
            (str(caption), str(item_id)),
        )
        self.conn.commit()

    def set_working(self, item_id: str, path: Path | None) -> None
        self.conn.execute(
            "UPDATE items SET working_path=?, updated_at=CURRENT_TIMESTAMP WHERE content_id=?",
            (str(path), item_id),
        )
        self.conn.commit()

    def set_result(self, item_id: str, path: Path) -> None:
        self.conn.execute(
            "UPDATE items SET result_path=?, updated_at=CURRENT_TIMESTAMP WHERE content_id=?",
            (str(path), item_id),
        )
        self.conn.commit()

    def fail(self, item_id: str, error: str) -> None:
        self.conn.execute(
            "UPDATE items SET state=?, last_error=?, updated_at=CURRENT_TIMESTAMP WHERE content_id=?",
            (State.FAILED.value, error, item_id),
        )
        self.conn.execute(
            "INSERT INTO state_events (content_id,new_state,reason) VALUES (?,?,?)",
            (item_id, State.FAILED.value, error),
        )
        self.conn.commit()

    def mark_cleanup_completed(self, item_id: str) -> None:
        self.conn.execute(
            "UPDATE items SET cleanup_completed=1, updated_at=CURRENT_TIMESTAMP WHERE content_id=?",
            (item_id,),
        )
        self.conn.commit()

    def publication_started(
        self,
        item_id: str,
        destination_chat_id: str | None = None,
        destination_topic_id: int | None = None,
    ) -> None:
        self.conn.execute(
            "INSERT INTO publications(content_id,idempotency_key,destination_chat_id,destination_topic_id) "
            "VALUES(?,?,?,?) "
            "ON CONFLICT(content_id) DO UPDATE SET "
            "destination_chat_id=COALESCE(excluded.destination_chat_id, publications.destination_chat_id), "
            "destination_topic_id=COALESCE(excluded.destination_topic_id, publications.destination_topic_id), "
            "updated_at=CURRENT_TIMESTAMP",
            (str(item_id), f"armoredcreator:content:{str(item_id)}",
             destination_chat_id, destination_topic_id),
        )
        self.conn.commit()

    def publication_send_started(self, item_id: str) -> None:
        """Persist that the external Telegram send has entered its side-effect window."""
        self.conn.execute(
            "UPDATE publications SET verification_status='SENT_UNVERIFIED', updated_at=CURRENT_TIMESTAMP "
            "WHERE content_id=?",
            (str(item_id),),
        )
        self.conn.commit()

    def publication_message_sent(self, item_id: str, message_id: str) -> None:
        """Persist the Telegram message ID before any post-send failure window."""
        self.conn.execute(
            "UPDATE publications SET published_message_id=?, confirmed=0, "
            "verification_status='SENT_UNVERIFIED', updated_at=CURRENT_TIMESTAMP "
            "WHERE content_id=?",
            (str(message_id), str(item_id)),
        )
        self.conn.commit()

    def publication_confirmed(self, item_id: str, message_id: str) -> None:
        self.conn.execute(
            "UPDATE publications SET published_message_id=?, confirmed=1, "
            "verification_status='CONFIRMED', verified_at=CURRENT_TIMESTAMP, "
            "updated_at=CURRENT_TIMESTAMP WHERE content_id=?",
            (str(message_id), str(item_id)),
        )
        self.conn.commit()

    def publication(self, item_id: int):
        return self.conn.execute(
            "SELECT * FROM publications WHERE content_id=?", (item_id,)
        ).fetchone()

    def acquire_runtime_lock(self, name: str = "coordinator") -> None:
        """Acquire the single-process runtime lease stored in SQLite.

        The lease intentionally lives in the database, not in a legacy lock
        directory/file. A crashed process leaves the row behind; on restart,
        a dead PID is deterministically replaced. A live PID blocks a second
        Coordinator from starting.
        """
        import os

        now = __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat()
        self.conn.execute(
            """CREATE TABLE IF NOT EXISTS runtime_locks (
                name TEXT PRIMARY KEY,
                pid INTEGER NOT NULL,
                started_at TEXT NOT NULL,
                heartbeat_at TEXT NOT NULL
            )"""
        )
        row = self.conn.execute(
            "SELECT pid FROM runtime_locks WHERE name=?", (str(name),)
        ).fetchone()
        current_pid = os.getpid()
        if row is not None:
            pid = int(row["pid"])
            # A live PID is always an active owner, including when a second
            # Coordinator object is created inside the same process. This keeps
            # the one-Coordinator invariant independent of process boundaries.
            alive = pid == current_pid
            if not alive:
                try:
                    os.kill(pid, 0)
                    alive = True
                except OSError:
                    alive = False
            if alive:
                raise RuntimeError(
                    f"runtime-lock-active: {name} is already owned by PID {pid}"
                )

        self.conn.execute(
            "INSERT INTO runtime_locks(name,pid,started_at,heartbeat_at) VALUES(?,?,?,?) "
            "ON CONFLICT(name) DO UPDATE SET pid=excluded.pid, "
            "started_at=excluded.started_at, heartbeat_at=excluded.heartbeat_at",
            (str(name), current_pid, now, now),
        )
        self.conn.commit()

    def heartbeat_runtime_lock(self, name: str = "coordinator") -> None:
        import os
        self.conn.execute(
            "UPDATE runtime_locks SET heartbeat_at=CURRENT_TIMESTAMP "
            "WHERE name=? AND pid=?",
            (str(name), os.getpid()),
        )
        self.conn.commit()

    def release_runtime_lock(self, name: str = "coordinator") -> None:
        import os
        self.conn.execute(
            "DELETE FROM runtime_locks WHERE name=? AND pid=?",
            (str(name), os.getpid()),
        )
        self.conn.commit()

    def close(self) -> None:
        if self.conn is None:
            return
        try:
            self.conn.commit()
            try:
                self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except sqlite3.OperationalError:
                # Another SQLite connection may still be open during shutdown.
                # Closing this connection is safe; the remaining connection can
                # perform the checkpoint later.
                pass
        finally:
            self.conn.close()
            self.conn = None

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
            telegram_message_id TEXT NOT NULL,
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
        CREATE TABLE IF NOT EXISTS sync_source_topics (
            source_id TEXT NOT NULL,
            topic_id INTEGER NOT NULL,
            topic_name TEXT NOT NULL,
            last_seen_message_id INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY(source_id, topic_id)
        );
        CREATE TABLE IF NOT EXISTS sync_sources (
            source_id TEXT PRIMARY KEY,
            historical_complete INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS caption_candidates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            content_id TEXT NOT NULL,
            batch_id TEXT NOT NULL,
            candidate_index INTEGER NOT NULL,
            caption TEXT NOT NULL,
            policy_valid INTEGER NOT NULL,
            rejection_reason TEXT,
            score REAL NOT NULL DEFAULT 0,
            selected INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(content_id, batch_id, candidate_index)
        );
        CREATE INDEX IF NOT EXISTS idx_caption_candidates_content
            ON caption_candidates(content_id, created_at);
        CREATE TABLE IF NOT EXISTS runtime_locks (
            name TEXT PRIMARY KEY,
            pid INTEGER NOT NULL,
            started_at TEXT NOT NULL,
            heartbeat_at TEXT NOT NULL
        );
        """)
        self.conn.commit()
        self._migrate_columns()
        self._migrate_telegram_identity()

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

    def _migrate_telegram_identity(self) -> None:
        """Replace global Telegram message uniqueness with source-scoped identity."""
        indexes = self.conn.execute("PRAGMA index_list(items)").fetchall()
        legacy_unique = False
        for row in indexes:
            if int(row["unique"] or 0) != 1:
                continue
            cols = self.conn.execute(f'PRAGMA index_info("{row["name"]}")').fetchall()
            if [str(col["name"]) for col in cols] == ["telegram_message_id"]:
                legacy_unique = True
                break
        if legacy_unique:
            self.conn.execute("""
                CREATE TABLE items_new (
                    content_id TEXT PRIMARY KEY,
                    telegram_message_id TEXT NOT NULL,
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
                    ia_context_json TEXT NOT NULL DEFAULT '{}',
                    affiliate_urls_json TEXT NOT NULL DEFAULT '[]',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    recovery_count INTEGER NOT NULL DEFAULT 0,
                    cleanup_completed INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
            """)
            columns = [
                "content_id","telegram_message_id","source_id","topic_id","topic_name",
                "original_url","state","original_path","original_sha256","working_path",
                "result_path","affiliate_name","affiliate_url","publication_caption",
                "ia_context_json","affiliate_urls_json","attempts","recovery_count",
                "cleanup_completed","last_error","created_at","updated_at"
            ]
            self.conn.execute(
                f"INSERT INTO items_new ({','.join(columns)}) SELECT {','.join(columns)} FROM items"
            )
            self.conn.execute("DROP TABLE items")
            self.conn.execute("ALTER TABLE items_new RENAME TO items")
        self.conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_items_source_message "
            "ON items(source_id, telegram_message_id)"
        )
        self.conn.commit()

    @staticmethod
    def content_id_for(telegram_message_id: str, source_id: str = "telegram") -> str:
        source = str(source_id)
        message = str(telegram_message_id)
        if source in {"telegram", "local"}:
            return message
        return f"{source}_{message}"

    def initialize_source_state(self, source_id: str, legacy_source_id: str | None = None) -> None:
        """Initialize a source without resetting the approved legacy Source 1 state."""
        source_id = str(source_id)
        exists = self.conn.execute(
            "SELECT 1 FROM sync_sources WHERE source_id=?",
            (source_id,),
        ).fetchone()
        if exists:
            return
        legacy = legacy_source_id is not None and source_id == str(legacy_source_id)
        historical_complete = 1 if legacy and self.historical_complete() else 0
        self.conn.execute(
            "INSERT INTO sync_sources(source_id,historical_complete) VALUES(?,?)",
            (source_id, historical_complete),
        )
        if legacy:
            rows = self.conn.execute(
                "SELECT topic_id,topic_name,last_seen_message_id FROM sync_topics"
            ).fetchall()
            for row in rows:
                self.conn.execute(
                    "INSERT OR IGNORE INTO sync_source_topics("
                    "source_id,topic_id,topic_name,last_seen_message_id"
                    ") VALUES(?,?,?,?)",
                    (source_id, int(row["topic_id"]), str(row["topic_name"]), int(row["last_seen_message_id"])),
                )
        self.conn.commit()

    def source_sync_mode(self, source_id: str) -> str:
        row = self.conn.execute(
            "SELECT historical_complete FROM sync_sources WHERE source_id=?",
            (str(source_id),),
        ).fetchone()
        return "LIVE" if row and int(row["historical_complete"]) else "CATCH_UP"

    def complete_source_historical_sync(self, source_id: str) -> None:
        self.conn.execute(
            "INSERT INTO sync_sources(source_id,historical_complete) VALUES(?,1) "
            "ON CONFLICT(source_id) DO UPDATE SET historical_complete=1, updated_at=CURRENT_TIMESTAMP",
            (str(source_id),),
        )
        self.conn.commit()

    def reset_source_historical_sync(self, source_id: str) -> None:
        self.conn.execute(
            "INSERT INTO sync_sources(source_id,historical_complete) VALUES(?,0) "
            "ON CONFLICT(source_id) DO UPDATE SET historical_complete=0, updated_at=CURRENT_TIMESTAMP",
            (str(source_id),),
        )
        self.conn.commit()

    def all_sources_historical_complete(self, source_ids) -> bool:
        ids = [str(value) for value in source_ids]
        if not ids:
            return False
        rows = self.conn.execute(
            f"SELECT source_id,historical_complete FROM sync_sources "
            f"WHERE source_id IN ({','.join('?' for _ in ids)})",
            ids,
        ).fetchall()
        return len(rows) == len(ids) and all(int(row["historical_complete"]) == 1 for row in rows)

    def source_sync_topic_checkpoint(self, source_id: str, topic_id: int) -> int:
        row = self.conn.execute(
            "SELECT last_seen_message_id FROM sync_source_topics WHERE source_id=? AND topic_id=?",
            (str(source_id), int(topic_id)),
        ).fetchone()
        return int(row["last_seen_message_id"]) if row else 0

    def set_source_sync_topic_checkpoint(self, source_id: str, topic_id: int, topic_name: str, message_id: int) -> None:
        self.conn.execute(
            "INSERT INTO sync_source_topics(source_id,topic_id,topic_name,last_seen_message_id) VALUES(?,?,?,?) "
            "ON CONFLICT(source_id,topic_id) DO UPDATE SET topic_name=excluded.topic_name, "
            "last_seen_message_id=MAX(sync_source_topics.last_seen_message_id, excluded.last_seen_message_id), "
            "updated_at=CURRENT_TIMESTAMP",
            (str(source_id), int(topic_id), str(topic_name), int(message_id)),
        )
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
        existing = self.conn.execute(
            "SELECT content_id FROM items WHERE source_id=? AND telegram_message_id=?",
            (str(source_id), str(telegram_message_id)),
        ).fetchone()
        if existing:
            return str(existing["content_id"])
        content_id = self.content_id_for(telegram_message_id, source_id)
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
        existing = self.conn.execute(
            "SELECT content_id FROM items WHERE source_id=? AND telegram_message_id=?",
            (str(source_id), str(telegram_message_id)),
        ).fetchone()
        if existing:
            return str(existing["content_id"])
        content_id = self.content_id_for(telegram_message_id, source_id)
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

    def pending_vision_approved_items(self) -> list[Item]:
        """Return all durable Vision-approved RECEIVED rows for staged catch-up.

        Some rows may already have an ORIGINAL because the process stopped
        between materialization and pipeline execution. Returning those too is
        essential: discovery suppresses represented URLs, so they must be
        resumed from SQLite rather than rediscovered from Telegram.
        """
        rows = self.conn.execute(
            "SELECT content_id FROM items "
            "WHERE state=? AND affiliate_url IS NOT NULL AND TRIM(affiliate_url)<>'' "
            "ORDER BY source_id, COALESCE(topic_id, 0), "
            "CAST(telegram_message_id AS INTEGER), created_at, content_id",
            (State.RECEIVED.value,),
        ).fetchall()
        return [self.get(str(row["content_id"])) for row in rows]

    def vision_approved_interrupted_items(self) -> list[Item]:
        """Find Vision rows where evidence was committed before a crash.

        Pipeline persists affiliate_url before transitioning VISION back to
        RECEIVED in pre-download mode. A process crash between those commits
        must not strand the approved candidate outside both Vision and Stock.
        """
        rows = self.conn.execute(
            "SELECT content_id FROM items WHERE state=? "
            "AND affiliate_url IS NOT NULL AND TRIM(affiliate_url)<>'' "
            "ORDER BY source_id, COALESCE(topic_id, 0), "
            "CAST(telegram_message_id AS INTEGER), created_at, content_id",
            (State.VISION.value,),
        ).fetchall()
        return [self.get(str(row["content_id"])) for row in rows]

    def pending_vision_candidates(self) -> list[Item]:
        """Return reserved rows whose Vision gate has not produced a decision."""
        rows = self.conn.execute(
            "SELECT content_id FROM items "
            "WHERE state IN (?, ?) AND (affiliate_url IS NULL OR TRIM(affiliate_url)='') "
            "ORDER BY source_id, COALESCE(topic_id, 0), "
            "CAST(telegram_message_id AS INTEGER), created_at, content_id",
            (State.RECEIVED.value, State.VISION.value),
        ).fetchall()
        return [self.get(str(row["content_id"])) for row in rows]

    def catch_up_production_items(self) -> list[Item]:
        """Return durable items that can resume production without re-running Vision."""
        rows = self.conn.execute(
            "SELECT content_id FROM items WHERE "
            "(state=? AND affiliate_url IS NOT NULL AND TRIM(affiliate_url)<>'') "
            "OR state IN (?, ?, ?, ?) "
            "OR (state=? AND cleanup_completed=0) "
            "ORDER BY source_id, COALESCE(topic_id, 0), "
            "CAST(telegram_message_id AS INTEGER), created_at, content_id",
            (
                State.RECEIVED.value,
                State.IA.value,
                State.STUDIO.value,
                State.PUBLISHING.value,
                State.RECOVERY.value,
                State.PUBLISHED.value,
            ),
        ).fetchall()
        return [self.get(str(row["content_id"])) for row in rows]

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

    def record_caption_candidates(self, item_id: str, batch_id: str, evaluations) -> None:
        """Persist every caption candidate and its local Policy/ranking decision."""
        self.conn.executemany(
            "INSERT OR REPLACE INTO caption_candidates "
            "(content_id,batch_id,candidate_index,caption,policy_valid,rejection_reason,score,selected) "
            "VALUES (?,?,?,?,?,?,?,?)",
            [
                (
                    str(item_id),
                    str(batch_id),
                    int(evaluation.index),
                    str(evaluation.caption),
                    1 if evaluation.policy_valid else 0,
                    evaluation.rejection_reason,
                    float(evaluation.score),
                    1 if evaluation.selected else 0,
                )
                for evaluation in evaluations
            ],
        )
        self.conn.commit()

    def caption_candidates(self, item_id: str, batch_id: str | None = None):
        if batch_id is None:
            return self.conn.execute(
                "SELECT * FROM caption_candidates "
                "WHERE content_id=? ORDER BY created_at, candidate_index",
                (str(item_id),),
            ).fetchall()
        return self.conn.execute(
            "SELECT * FROM caption_candidates "
            "WHERE content_id=? AND batch_id=? ORDER BY candidate_index",
            (str(item_id), str(batch_id)),
        ).fetchall()

    def set_working(self, item_id: str, path: Path | None) -> None:
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

    def record_retryable_error(self, item_id: str, error: str) -> None:
        """Persist a technical error without moving a pre-download item to RECOVERY."""
        current = self.get(item_id)
        self.conn.execute(
            "UPDATE items SET last_error=?, updated_at=CURRENT_TIMESTAMP WHERE content_id=?",
            (str(error), str(item_id)),
        )
        self.conn.execute(
            "INSERT INTO state_events (content_id,old_state,new_state,reason) VALUES (?,?,?,?)",
            (str(item_id), current.state.value, current.state.value, str(error)),
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

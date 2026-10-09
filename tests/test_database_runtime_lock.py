import tempfile
from pathlib import Path

from armored_core.database import Database


def test_deferred_schema_initialization_allows_runtime_lease_first(tmp_path):
    db = Database(Path(tmp_path) / "armoredcreator.db", initialize=False)
    try:
        tables_before = {
            row["name"]
            for row in db.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert "items" not in tables_before

        db.acquire_runtime_lock("coordinator")

        tables_with_lease = {
            row["name"]
            for row in db.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert "runtime_locks" in tables_with_lease
        assert "items" not in tables_with_lease

        db.initialize()

        tables_after = {
            row["name"]
            for row in db.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert "items" in tables_after
        assert db.conn.execute(
            "SELECT pid FROM runtime_locks WHERE name='coordinator'"
        ).fetchone() is not None
    finally:
        db.release_runtime_lock("coordinator")
        db.close()

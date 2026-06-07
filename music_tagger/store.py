"""SQLite-backed cache + rollback journal.

Three tables:
  lookup_cache  album_key -> beets/MusicBrainz proposal (skips network on re-run)
  claude_cache  (album_key, candidates_hash) -> Claude verdict (skips API on re-run)
  write_log     run_id -> per-file original + written tags (powers --undo)
"""
import json
import time
import sqlite3
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS lookup_cache (
    album_key      TEXT PRIMARY KEY,
    folder         TEXT,
    recommendation TEXT,
    proposal_json  TEXT,
    fetched_at     REAL
);
CREATE TABLE IF NOT EXISTS claude_cache (
    album_key       TEXT,
    candidates_hash TEXT,
    verdict_json    TEXT,
    ts              REAL,
    PRIMARY KEY (album_key, candidates_hash)
);
CREATE TABLE IF NOT EXISTS write_log (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id             TEXT,
    file_path          TEXT,
    original_tags_json TEXT,
    written_tags_json  TEXT,
    ts                 REAL
);
CREATE INDEX IF NOT EXISTS idx_write_log_run ON write_log(run_id);
"""


class Store:
    def __init__(self, path: str | Path):
        self.conn = sqlite3.connect(str(path))
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(_SCHEMA)
        self.conn.commit()

    # ── lookup cache (MusicBrainz proposals) ──────────────────────────────
    def get_lookup(self, album_key: str) -> dict | None:
        row = self.conn.execute(
            "SELECT proposal_json FROM lookup_cache WHERE album_key = ?", (album_key,)
        ).fetchone()
        return json.loads(row["proposal_json"]) if row else None

    def put_lookup(self, album_key: str, folder: str, recommendation: str, proposal: dict) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO lookup_cache "
            "(album_key, folder, recommendation, proposal_json, fetched_at) VALUES (?,?,?,?,?)",
            (album_key, folder, recommendation, json.dumps(proposal), time.time()),
        )
        self.conn.commit()

    # ── claude verdict cache ──────────────────────────────────────────────
    def get_claude(self, album_key: str, candidates_hash: str) -> dict | None:
        row = self.conn.execute(
            "SELECT verdict_json FROM claude_cache WHERE album_key = ? AND candidates_hash = ?",
            (album_key, candidates_hash),
        ).fetchone()
        return json.loads(row["verdict_json"]) if row else None

    def put_claude(self, album_key: str, candidates_hash: str, verdict: dict) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO claude_cache "
            "(album_key, candidates_hash, verdict_json, ts) VALUES (?,?,?,?)",
            (album_key, candidates_hash, json.dumps(verdict), time.time()),
        )
        self.conn.commit()

    # ── write log (rollback journal) ──────────────────────────────────────
    def log_write(self, run_id: str, file_path: str, original: dict, written: dict) -> None:
        self.conn.execute(
            "INSERT INTO write_log (run_id, file_path, original_tags_json, written_tags_json, ts) "
            "VALUES (?,?,?,?,?)",
            (run_id, file_path, json.dumps(original), json.dumps(written), time.time()),
        )
        self.conn.commit()

    def get_run_writes(self, run_id: str) -> list[dict]:
        rows = self.conn.execute(
            "SELECT file_path, original_tags_json, written_tags_json FROM write_log "
            "WHERE run_id = ? ORDER BY id",
            (run_id,),
        ).fetchall()
        return [
            {
                "file_path": r["file_path"],
                "original": json.loads(r["original_tags_json"]),
                "written": json.loads(r["written_tags_json"]),
            }
            for r in rows
        ]

    def list_runs(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT run_id, COUNT(*) AS n, MIN(ts) AS started FROM write_log "
            "GROUP BY run_id ORDER BY started DESC"
        ).fetchall()
        return [{"run_id": r["run_id"], "files": r["n"], "started": r["started"]} for r in rows]

    def close(self) -> None:
        self.conn.commit()
        self.conn.close()

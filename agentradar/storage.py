"""Local SQLite persistence with transactional collection/checkpoint updates."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path


def _now():
    return datetime.now(timezone.utc).isoformat()


def _dump(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


class Store:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS items (
                    id TEXT PRIMARY KEY, source TEXT NOT NULL, external_id TEXT NOT NULL,
                    version_key TEXT NOT NULL, fetched_at TEXT NOT NULL, first_seen_at TEXT NOT NULL,
                    payload TEXT NOT NULL, UNIQUE(source, external_id, version_key)
                );
                CREATE INDEX IF NOT EXISTS items_identity ON items(source, external_id, fetched_at);
                CREATE TABLE IF NOT EXISTS source_status (
                    source TEXT PRIMARY KEY, payload TEXT NOT NULL, checkpoint TEXT
                );
                CREATE TABLE IF NOT EXISTS collection_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, saved_at TEXT NOT NULL,
                    statuses TEXT NOT NULL, item_ids TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS reports (
                    date TEXT PRIMARY KEY, generated_at TEXT NOT NULL, payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS feedback (
                    item_id TEXT PRIMARY KEY REFERENCES items(id), action TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS analyses (
                    item_id TEXT NOT NULL REFERENCES items(id), input_hash TEXT NOT NULL,
                    model TEXT NOT NULL, prompt_version TEXT NOT NULL, payload TEXT NOT NULL,
                    PRIMARY KEY(item_id, input_hash, model, prompt_version)
                );
            """)

    @contextmanager
    def _connection(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA busy_timeout=10000")
        try:
            with db:
                yield db
        finally:
            db.close()

    def save_collection(self, items: list[dict], statuses: list[dict]) -> list[dict]:
        saved = {}
        with self._connection() as db:
            for incoming in items:
                value = dict(incoming)
                identity = [value["source"], value["external_id"], value["version_key"]]
                item_id = hashlib.sha256(_dump(identity).encode()).hexdigest()[:24]
                existing = db.execute("SELECT first_seen_at FROM items WHERE id=?", (item_id,)).fetchone()
                value["id"] = item_id
                value["first_seen_at"] = existing["first_seen_at"] if existing else _now()
                value.setdefault("fetched_at", _now())
                db.execute("""INSERT INTO items(id,source,external_id,version_key,fetched_at,first_seen_at,payload)
                    VALUES(?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET fetched_at=excluded.fetched_at,payload=excluded.payload""",
                    (item_id, *identity, value["fetched_at"], value["first_seen_at"], _dump(value)))
                saved[item_id] = value
            for status in statuses:
                checkpoint = status.get("checkpoint") if status.get("status") == "ok" else None
                db.execute("""INSERT INTO source_status(source,payload,checkpoint) VALUES(?,?,?)
                    ON CONFLICT(source) DO UPDATE SET payload=excluded.payload,
                    checkpoint=COALESCE(excluded.checkpoint,source_status.checkpoint)""",
                    (status["source"], _dump(status), checkpoint))
            db.execute("INSERT INTO collection_runs(saved_at,statuses,item_ids) VALUES(?,?,?)",
                       (_now(), _dump(statuses), _dump(list(saved))))
        return list(saved.values())

    def collection_runs(self, limit=30) -> list[dict]:
        """Historical source outcomes, including unsuccessful collection attempts."""
        with self._connection() as db:
            return [{"id": row["id"], "saved_at": row["saved_at"],
                     "statuses": json.loads(row["statuses"]), "item_ids": json.loads(row["item_ids"])}
                    for row in db.execute("SELECT * FROM collection_runs ORDER BY id DESC LIMIT ?", (max(0, int(limit)),))]

    def checkpoints(self) -> dict:
        with self._connection() as db:
            return {row["source"]: row["checkpoint"] for row in db.execute(
                "SELECT source,checkpoint FROM source_status WHERE checkpoint IS NOT NULL")}

    def items(self, limit=500) -> list[dict]:
        with self._connection() as db:
            rows = db.execute("""SELECT payload FROM (
                SELECT payload,fetched_at,first_seen_at,id,
                ROW_NUMBER() OVER(PARTITION BY source,external_id ORDER BY fetched_at DESC,first_seen_at DESC,rowid DESC) AS rank
                FROM items) WHERE rank=1 ORDER BY fetched_at DESC,first_seen_at DESC,id DESC LIMIT ?""", (max(0, int(limit)),))
            return [json.loads(row["payload"]) for row in rows]

    def item(self, item_id: str) -> dict | None:
        with self._connection() as db:
            row = db.execute("SELECT payload FROM items WHERE id=?", (item_id,)).fetchone()
            return json.loads(row["payload"]) if row else None

    def save_report(self, report: dict) -> None:
        value = dict(report)
        report_date = value["date"]
        if date.fromisoformat(report_date).isoformat() != report_date:
            raise ValueError("Report date must use YYYY-MM-DD")
        value.setdefault("generated_at", _now())
        with self._connection() as db:
            db.execute("""INSERT INTO reports(date,generated_at,payload) VALUES(?,?,?)
                ON CONFLICT(date) DO UPDATE SET generated_at=excluded.generated_at,payload=excluded.payload""",
                (report_date, value["generated_at"], _dump(value)))

    def report(self, date: str | None = None) -> dict | None:
        with self._connection() as db:
            row = db.execute("SELECT payload FROM reports WHERE date=?", (date,)).fetchone() if date else db.execute(
                "SELECT payload FROM reports ORDER BY date DESC LIMIT 1").fetchone()
            return json.loads(row["payload"]) if row else None

    def reports(self, limit=30) -> list[dict]:
        with self._connection() as db:
            return [json.loads(row["payload"]) for row in db.execute(
                "SELECT payload FROM reports ORDER BY date DESC LIMIT ?", (max(0, int(limit)),))]

    def recommended_before(self, date: str) -> set:
        with self._connection() as db:
            reports = [json.loads(row["payload"]) for row in db.execute("SELECT payload FROM reports WHERE date<?", (date,))]
        return {card["item_id"] for report in reports for card in report.get("cards", []) if card.get("item_id")}

    def feedback(self, item_id: str, action: str) -> None:
        if action not in {"useful", "hard", "ignore", "saved", "completed"}:
            raise ValueError("Unknown feedback action")
        with self._connection() as db:
            if not db.execute("SELECT 1 FROM items WHERE id=?", (item_id,)).fetchone():
                raise ValueError("Unknown item")
            db.execute("""INSERT INTO feedback(item_id,action,updated_at) VALUES(?,?,?)
                ON CONFLICT(item_id) DO UPDATE SET action=excluded.action,updated_at=excluded.updated_at""", (item_id, action, _now()))

    def feedback_items(self) -> list[dict]:
        with self._connection() as db:
            return [{**json.loads(row["payload"]), "item_id": row["item_id"], "action": row["action"],
                     "feedback_at": row["updated_at"]} for row in db.execute(
                "SELECT i.payload,f.* FROM feedback f JOIN items i ON i.id=f.item_id ORDER BY f.updated_at DESC")]

    def feedback_map(self) -> dict:
        with self._connection() as db:
            return {row["item_id"]: row["action"] for row in db.execute("SELECT item_id,action FROM feedback")}

    def save_analysis(self, item_id: str, input_hash: str, model: str, prompt_version: str, result: dict) -> None:
        with self._connection() as db:
            if not db.execute("SELECT 1 FROM items WHERE id=?", (item_id,)).fetchone():
                raise ValueError("Unknown item")
            db.execute("""INSERT INTO analyses(item_id,input_hash,model,prompt_version,payload) VALUES(?,?,?,?,?)
                ON CONFLICT(item_id,input_hash,model,prompt_version) DO UPDATE SET payload=excluded.payload""",
                (item_id, input_hash, model, prompt_version, _dump(result)))

    def analysis(self, item_id, input_hash, model, prompt_version) -> dict | None:
        with self._connection() as db:
            row = db.execute("SELECT payload FROM analyses WHERE item_id=? AND input_hash=? AND model=? AND prompt_version=?",
                             (item_id, input_hash, model, prompt_version)).fetchone()
            return json.loads(row["payload"]) if row else None

    def stats(self) -> dict:
        with self._connection() as db:
            return {"total_items": db.execute("SELECT count(*) FROM (SELECT DISTINCT source,external_id FROM items)").fetchone()[0],
                    "total_versions": db.execute("SELECT count(*) FROM items").fetchone()[0],
                    "reports": db.execute("SELECT count(*) FROM reports").fetchone()[0]}

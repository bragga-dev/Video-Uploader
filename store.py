"""
store.py — fila persistente e histórico (SQLite).

Cada tarefa (job) fica gravada em disco; se o programa fechar ou a internet cair, ao abrir de novo
as tarefas interrompidas aparecem na lista e podem ser retomadas. NUNCA grava senhas.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Optional

# status possíveis
QUEUED, RUNNING, DONE, ERROR, CANCELLED, INTERRUPTED = (
    "queued", "running", "done", "error", "cancelled", "interrupted")
ACTIVE = (QUEUED, RUNNING)
RESUMABLE = (ERROR, CANCELLED, INTERRUPTED)

_COLUMNS = ("id", "url", "dest", "status", "title", "size", "link", "error", "options",
            "attempts", "created_at", "started_at", "finished_at")


class Store:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(str(self.path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        with self._lock:
            self._db.execute("""
                CREATE TABLE IF NOT EXISTS jobs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    url TEXT NOT NULL,
                    dest TEXT NOT NULL,
                    status TEXT NOT NULL,
                    title TEXT DEFAULT '',
                    size INTEGER DEFAULT 0,
                    link TEXT DEFAULT '',
                    error TEXT DEFAULT '',
                    options TEXT DEFAULT '{}',
                    attempts INTEGER DEFAULT 0,
                    created_at REAL,
                    started_at REAL,
                    finished_at REAL
                )""")
            self._db.commit()

    # ── escrita ───────────────────────────────────────────────
    def add_job(self, url: str, dest: str, options: dict) -> int:
        with self._lock:
            cur = self._db.execute(
                "INSERT INTO jobs (url, dest, status, options, created_at) VALUES (?,?,?,?,?)",
                (url, dest, QUEUED, json.dumps(options, ensure_ascii=False), time.time()))
            self._db.commit()
            return int(cur.lastrowid)

    def update(self, job_id: int, **fields: Any):
        bad = set(fields) - set(_COLUMNS)
        if bad:
            raise ValueError(f"colunas inválidas: {bad}")
        if "options" in fields and not isinstance(fields["options"], str):
            fields["options"] = json.dumps(fields["options"], ensure_ascii=False)
        if not fields:
            return
        cols = ", ".join(f"{k}=?" for k in fields)
        with self._lock:
            self._db.execute(f"UPDATE jobs SET {cols} WHERE id=?", (*fields.values(), job_id))
            self._db.commit()

    def delete(self, job_id: int):
        with self._lock:
            self._db.execute("DELETE FROM jobs WHERE id=?", (job_id,))
            self._db.commit()

    def clear_finished(self) -> int:
        with self._lock:
            cur = self._db.execute("DELETE FROM jobs WHERE status=?", (DONE,))
            self._db.commit()
            return cur.rowcount

    def recover_interrupted(self) -> int:
        """Tarefas que estavam rodando/na fila quando o programa fechou viram 'interrupted'."""
        with self._lock:
            cur = self._db.execute("UPDATE jobs SET status=? WHERE status IN (?,?)",
                                   (INTERRUPTED, RUNNING, QUEUED))
            self._db.commit()
            return cur.rowcount

    # ── leitura ───────────────────────────────────────────────
    @staticmethod
    def _row(r: Optional[sqlite3.Row]) -> Optional[dict]:
        if r is None:
            return None
        d = dict(r)
        try:
            d["options"] = json.loads(d.get("options") or "{}")
        except Exception:
            d["options"] = {}
        return d

    def get(self, job_id: int) -> Optional[dict]:
        with self._lock:
            return self._row(self._db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())

    def list_jobs(self, limit: int = 200) -> list[dict]:
        with self._lock:
            rows = self._db.execute("SELECT * FROM jobs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [self._row(r) for r in rows]

    def find_done(self, url: str) -> Optional[dict]:
        """Já baixei e enviei esta URL antes? (evita repetir)"""
        with self._lock:
            r = self._db.execute(
                "SELECT * FROM jobs WHERE url=? AND status=? ORDER BY id DESC LIMIT 1", (url, DONE)).fetchone()
        return self._row(r)

    def close(self):
        with self._lock:
            self._db.close()
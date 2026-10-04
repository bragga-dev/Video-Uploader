"""
runner.py — executa a fila de tarefas (store.py) com vários downloads em paralelo.

* N tarefas ao mesmo tempo (set_workers); enquanto uma envia ao Drive, a próxima já baixa.
* Cada tarefa tem pasta de trabalho própria (work_root/<id>): se cair a luz/internet ou você
  cancelar, "retomar" continua de onde parou (yt-dlp/aria2c retomam o arquivo .part).
* Não conhece a interface: avisa tudo por `on_event(job_id, tipo, dados)`.
    tipos: "status" (novo status), "progress" ((fração, texto)), "log" (texto), "idle" (fila esvaziou; job_id=None)
"""
from __future__ import annotations

import queue
import re
import shutil
import threading
import time
from pathlib import Path
from typing import Callable, Optional

import store as st
import video_uploader as vu

EventCB = Callable[[Optional[int], str, object], None]

# opções da tarefa que são repassadas para vu.process_url
_PASS = ("quality", "playlist", "keep_local", "cookies_file", "cookies_from_browser", "proxy",
         "folder_id", "gdrive_folder", "mega_email", "mega_folder", "use_aria2", "rate_limit")


class Runner:
    def __init__(self, store: st.Store, work_root: Path, on_event: Optional[EventCB] = None, workers: int = 1):
        self.store = store
        self.work_root = Path(work_root)
        self.on_event: EventCB = on_event or (lambda *_: None)
        self._q: queue.Queue = queue.Queue()
        self._limit = max(1, workers)
        self._active = 0
        self._pending = 0
        self._cond = threading.Condition()
        self._cancels: dict[int, threading.Event] = {}
        self._secrets: dict[int, str] = {}
        self._lock = threading.Lock()
        threading.Thread(target=self._dispatch, daemon=True, name="runner-dispatch").start()

    # ── API pública ───────────────────────────────────────────
    def set_workers(self, n: int):
        with self._cond:
            self._limit = max(1, int(n))
            self._cond.notify_all()

    def submit(self, job_id: int, secret: str = ""):
        """Coloca a tarefa na fila (nova ou para retomar). `secret` = senha do MEGA, só em memória."""
        with self._lock:
            if job_id in self._cancels:          # já está na fila/rodando
                return
            self._cancels[job_id] = threading.Event()
            self._secrets[job_id] = secret
            self._pending += 1
        self.store.update(job_id, status=st.QUEUED, error="")
        self.on_event(job_id, "status", st.QUEUED)
        self._q.put(job_id)

    def cancel(self, job_id: int):
        with self._lock:
            ev = self._cancels.get(job_id)
        if ev:
            ev.set()

    def cancel_all(self):
        with self._lock:
            for ev in self._cancels.values():
                ev.set()

    def is_busy(self) -> bool:
        with self._lock:
            return self._pending > 0

    def forget(self, job_id: int):
        """Remove a tarefa do histórico e apaga a pasta de retomada."""
        self.cancel(job_id)
        shutil.rmtree(self.work_root / str(job_id), ignore_errors=True)
        self.store.delete(job_id)

    # ── internos ──────────────────────────────────────────────
    def _dispatch(self):
        while True:
            jid = self._q.get()
            with self._cond:
                while self._active >= self._limit:
                    self._cond.wait()
                self._active += 1
            threading.Thread(target=self._worker, args=(jid,), daemon=True, name=f"job-{jid}").start()

    def _worker(self, jid: int):
        try:
            self._run_job(jid)
        except Exception as e:  # noqa: BLE001  (bug inesperado não pode travar a fila)
            self.store.update(jid, status=st.ERROR, error=f"Erro interno: {type(e).__name__}: {e}",
                              finished_at=time.time())
            self.on_event(jid, "status", st.ERROR)
        finally:
            with self._lock:
                self._cancels.pop(jid, None)
                self._secrets.pop(jid, None)
                self._pending -= 1
                idle = self._pending == 0
            with self._cond:
                self._active -= 1
                self._cond.notify_all()
            if idle:
                self.on_event(None, "idle", None)

    def _run_job(self, jid: int):
        job = self.store.get(jid)
        if job is None:
            return
        with self._lock:
            cancel = self._cancels[jid]
            secret = self._secrets.get(jid, "")
        if cancel.is_set():
            self.store.update(jid, status=st.CANCELLED, finished_at=time.time())
            self.on_event(jid, "status", st.CANCELLED)
            return

        o = job["options"]
        self.store.update(jid, status=st.RUNNING, started_at=time.time(), attempts=(job["attempts"] or 0) + 1,
                          error="")
        self.on_event(jid, "status", st.RUNNING)

        total_size = 0

        def on_file(p: Path):
            nonlocal total_size
            total_size += p.stat().st_size
            title = re.sub(r"\s*\[[^\]]{2,}\]$", "", p.stem)       # tira o " [id]" do yt-dlp
            first = self.store.get(jid)
            if first and first.get("title"):
                title = first["title"]                              # playlist: mantém o 1º título
            self.store.update(jid, title=title, size=total_size)
            self.on_event(jid, "status", st.RUNNING)

        kwargs = {k: o[k] for k in _PASS if k in o}
        try:
            links = vu.process_url(
                job["url"], dest=job["dest"],
                local_dir=Path(o.get("local_dir") or "downloads"),
                credentials_file=Path(o.get("credentials_file") or "gdrive_credentials.json"),
                mega_password=secret,
                work_dir=self.work_root / str(jid),
                on_file=on_file,
                progress=lambda f, t: self.on_event(jid, "progress", (f, t)),
                log=lambda m: self.on_event(jid, "log", m),
                cancel=cancel, **kwargs)
            self.store.update(jid, status=st.DONE, link="\n".join(links), finished_at=time.time(), error="")
            self.on_event(jid, "status", st.DONE)
        except vu.Cancelled:
            self.store.update(jid, status=st.CANCELLED, finished_at=time.time())
            self.on_event(jid, "status", st.CANCELLED)
        except Exception as e:  # noqa: BLE001
            self.store.update(jid, status=st.ERROR, error=str(e), finished_at=time.time())
            self.on_event(jid, "log", f"❌ {e}")
            self.on_event(jid, "status", st.ERROR)
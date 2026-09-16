"""
Shared background-job registry.

OCR, Summarize and Compare all run long work in a daemon thread while the
browser polls for progress. Each of them used to carry its own near-identical
copy of the same ~60 lines (dict + lock + new/get/cancel/patch/sweep/discard),
which meant bug fixes had to be applied three times — exactly how the
"cancel reported as error" and "scratch files never deleted" bugs ended up
being inconsistent between them.

This module owns that machinery once. A tool creates a registry with the extra
fields it needs, and keeps its own thin module-level wrappers so existing call
sites don't change.
"""

from __future__ import annotations

import os
import threading
import time
import uuid

DEFAULT_MAX_AGE = 3600          # keep finished jobs for ~1 hour


class JobRegistry:
    """Thread-safe store of in-flight/finished jobs for one tool."""

    def __init__(self, extra_fields: dict | None = None,
                 max_age: int = DEFAULT_MAX_AGE, max_history: int = 200):
        self._jobs: dict[str, dict] = {}
        self._lock = threading.Lock()
        self._extra = dict(extra_fields or {})
        self._max_age = max_age
        self._max_history = max_history

    # -- internals --------------------------------------------------------
    def _sweep_locked(self) -> None:
        """Drop finished jobs that are old, and cap total history size."""
        now = time.time()
        for k in [k for k, v in self._jobs.items()
                  if v.get('finished_at') and now - v['finished_at'] > self._max_age]:
            self._jobs.pop(k, None)
        if len(self._jobs) > self._max_history:
            finished = sorted(
                ((k, v) for k, v in self._jobs.items() if v.get('finished_at')),
                key=lambda kv: kv[1]['finished_at'])
            for k, _ in finished[:len(self._jobs) - self._max_history]:
                self._jobs.pop(k, None)

    def _blank(self, jid: str) -> dict:
        job = {
            'id': jid,
            'status': 'pending',      # pending | running | done | error | cancelled
            'progress': 0,            # 0..100
            'message': 'Pending…',
            'error': None,
            'started_at': time.time(),
            'finished_at': None,
            'cancelled': False,
        }
        for key, default in self._extra.items():
            # copy mutable defaults so jobs never share a list/dict
            job[key] = list(default) if isinstance(default, list) else (
                dict(default) if isinstance(default, dict) else default)
        return job

    # -- public API -------------------------------------------------------
    def new(self) -> str:
        jid = uuid.uuid4().hex[:12]
        with self._lock:
            self._sweep_locked()
            self._jobs[jid] = self._blank(jid)
        return jid

    def get(self, jid: str) -> dict | None:
        with self._lock:
            job = self._jobs.get(jid)
            return None if job is None else dict(job)

    def cancel(self, jid: str) -> bool:
        with self._lock:
            job = self._jobs.get(jid)
            if not job:
                return False
            job['cancelled'] = True
            return True

    def patch(self, jid: str, **kw) -> None:
        with self._lock:
            job = self._jobs.get(jid)
            if job is not None:
                job.update(kw)

    def is_cancelled(self, jid: str) -> bool:
        with self._lock:
            job = self._jobs.get(jid)
            return bool(job and job.get('cancelled'))

    # -- terminal states ---------------------------------------------------
    def finish(self, jid: str, **fields) -> None:
        """Mark a job done, merging any result fields."""
        self.patch(jid, status='done', progress=100,
                   finished_at=time.time(), **fields)

    def fail(self, jid: str, error: str, cancelled_message: str) -> None:
        """
        Terminal failure. A user-requested stop is reported as 'cancelled'
        rather than 'error', so the UI can show a neutral notice instead of a
        red banner for a deliberate action.
        """
        if self.is_cancelled(jid):
            self.patch(jid, status='cancelled', error=None,
                       message=cancelled_message, finished_at=time.time())
        else:
            self.patch(jid, status='error', error=error, message=error,
                       finished_at=time.time())


def discard(paths) -> None:
    """
    Delete per-job scratch files once a worker is finished with them.

    Accepts a single path or an iterable; always safe to call in a `finally`.
    """
    if paths is None:
        return
    if isinstance(paths, (str, bytes, os.PathLike)):
        paths = [paths]
    for path in paths:
        try:
            if path and os.path.isfile(path):
                os.unlink(path)
        except OSError:
            pass
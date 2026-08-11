"""ace.gateway.local_store — the sidecar's own telemetry, kept on the developer's machine.

Deliberately **not** the cloud gateway's telemetry path. That one writes to a shared
TimescaleDB hypertable and stores prompt/response text; both are wrong here. A local sidecar
holds one developer's traffic, and persisting their prompts to a shared store is the exact
posture the local-first design exists to avoid.

So this stores **numbers only**, in **SQLite** (stdlib — no dependency, no server, no DSN), at
``~/.ace/telemetry.db``. No prompt text, no responses, no file paths. What it keeps is what
the coding-workload analysis actually needs: the token buckets, the cache split, and cost.

Why SQLite rather than the in-memory repository the gateway defaults to: a sidecar is
restarted constantly — every ``ace up`` would otherwise begin with an empty history, which
makes cross-session analysis impossible and is the whole point of having a dashboard.

Schema note
-----------
``cache_read_tokens`` / ``cache_write_tokens`` are the **provider's** prompt cache, not ACE's
semantic cache. On coding traffic they carry ~98% of prompt volume, so they are first-class
columns rather than a nested blob. ``tokens_in`` is the *fresh* remainder and is a small
fraction of what was really sent — see docs/22 §1.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import threading
import time
from typing import Any, Dict, List, Optional

log = logging.getLogger("ace.gateway.local_store")

DEFAULT_DB_PATH = os.path.expanduser("~/.ace/telemetry.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS turns (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    ts                 REAL    NOT NULL,
    request_id         TEXT,
    session_id         TEXT,
    model              TEXT,
    status             TEXT,
    streamed           INTEGER DEFAULT 0,
    tokens_in          INTEGER DEFAULT 0,   -- FRESH input only; see module docstring
    tokens_out         INTEGER DEFAULT 0,
    cache_read_tokens  INTEGER DEFAULT 0,   -- provider prompt cache, not ACE's
    cache_write_tokens INTEGER DEFAULT 0,
    cost_usd           REAL    DEFAULT 0.0,
    cache_saved_usd    REAL    DEFAULT 0.0,
    ttft_ms            REAL    DEFAULT 0.0
);
CREATE INDEX IF NOT EXISTS idx_turns_ts ON turns(ts);
CREATE INDEX IF NOT EXISTS idx_turns_session ON turns(session_id);
"""


class LocalStore:
    """SQLite-backed turn log. Satisfies the ``record_log`` seam the messages route calls.

    Writes are synchronous and small (one row, all integers and floats). The alternative —
    buffering like the cloud ``TokenAccountant`` — trades durability for a saving that is
    invisible next to a multi-second upstream call, and a sidecar that loses its tail on
    Ctrl-C is worse than one that is a microsecond slower.
    """

    def __init__(self, path: str = DEFAULT_DB_PATH) -> None:
        self.path = path
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self._lock = threading.Lock()
        # check_same_thread=False: uvicorn serves from a worker thread, and every write is
        # already serialised behind the lock below.
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.executescript(_SCHEMA)
        self._db.commit()

    # -- write -------------------------------------------------------------------------

    def record_log(self, row: Any) -> None:
        """Persist one turn. Never raises — telemetry must not cost a developer their turn."""
        try:
            with self._lock:
                self._db.execute(
                    "INSERT INTO turns (ts, request_id, session_id, model, status, streamed,"
                    " tokens_in, tokens_out, cache_read_tokens, cache_write_tokens,"
                    " cost_usd, cache_saved_usd, ttft_ms)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        getattr(row, "finished_at", time.time()),
                        getattr(row, "request_id", ""),
                        getattr(row, "session_id", None),
                        getattr(row, "model", ""),
                        getattr(row, "status", "ok"),
                        1 if getattr(row, "streamed", False) else 0,
                        getattr(row, "tokens_in", 0),
                        getattr(row, "tokens_out", 0),
                        getattr(row, "cache_read_input_tokens", 0),
                        getattr(row, "cache_write_input_tokens", 0),
                        getattr(row, "cost_usd", 0.0),
                        getattr(row, "cache_saved_usd", 0.0),
                        getattr(row, "ttft_ms", 0.0),
                    ),
                )
                self._db.commit()
        except Exception:  # pragma: no cover - defensive
            log.debug("[local_store] failed to record a turn", exc_info=True)

    # -- read --------------------------------------------------------------------------

    def summary(self, since: Optional[float] = None) -> Dict[str, Any]:
        """Aggregates for the dashboard."""
        where, args = ("WHERE ts >= ?", (since,)) if since else ("", ())
        with self._lock:
            cur = self._db.execute(
                f"""SELECT COUNT(*), COALESCE(SUM(tokens_in),0), COALESCE(SUM(tokens_out),0),
                           COALESCE(SUM(cache_read_tokens),0), COALESCE(SUM(cache_write_tokens),0),
                           COALESCE(SUM(cost_usd),0), COALESCE(SUM(cache_saved_usd),0),
                           MIN(ts), MAX(ts)
                    FROM turns {where}""",
                args,
            )
            n, ti, to, cr, cw, cost, saved, t0, t1 = cur.fetchone()
        prompt = ti + cr + cw
        return {
            "turns": n,
            "tokens_in": ti,
            "tokens_out": to,
            "cache_read_tokens": cr,
            "cache_write_tokens": cw,
            "prompt_tokens": prompt,
            "cache_read_share": (cr / prompt) if prompt else 0.0,
            "cost_usd": cost,
            "cache_saved_usd": saved,
            "first_ts": t0,
            "last_ts": t1,
        }

    def recent(self, limit: int = 50) -> List[Dict[str, Any]]:
        with self._lock:
            cur = self._db.execute(
                "SELECT ts, model, status, tokens_in, tokens_out, cache_read_tokens,"
                " cache_write_tokens, cost_usd FROM turns ORDER BY ts DESC LIMIT ?",
                (limit,),
            )
            cols = [c[0] for c in cur.description]
            return [dict(zip(cols, r)) for r in cur.fetchall()]

    def by_model(self) -> List[Dict[str, Any]]:
        with self._lock:
            cur = self._db.execute(
                "SELECT model, COUNT(*) n, COALESCE(SUM(cost_usd),0) cost,"
                " COALESCE(SUM(cache_read_tokens),0) cr FROM turns"
                " GROUP BY model ORDER BY cost DESC"
            )
            cols = [c[0] for c in cur.description]
            return [dict(zip(cols, r)) for r in cur.fetchall()]

    def close(self) -> None:
        with self._lock:
            self._db.close()

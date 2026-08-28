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

import json
import logging
import os
import sqlite3
import threading
import time
from typing import Any, Dict, Iterable, List, Mapping, Optional

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

-- One row per lever per proxied turn: what an installed lever, run for real against the
-- actual request body, measured. Sibling of `turns` rather than columns on it, because a
-- turn has N of these (one per enabled lever) and because every row here is a
-- COUNTERFACTUAL -- a prompt that was never sent -- while every row in `turns` is what the
-- provider actually billed. Merging the two would put a real charge and a hypothetical
-- saving in one record with nothing to tell them apart.
--
-- Why this table has to exist at all: a measured result is produced once, in a background
-- task, moments after a response is served. Without a row here it is logged and lost, and
-- the dashboard is back to simulating headroom over transcripts.
CREATE TABLE IF NOT EXISTS lever_turns (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    ts                    REAL    NOT NULL,
    request_id            TEXT,
    session_id            TEXT,
    lever                 TEXT    NOT NULL,
    mode                  TEXT,
    model                 TEXT,
    -- Both sides of the counterfactual, kept so the delta can be re-derived rather than
    -- trusted. Counted the same way through the provider's own counter; only their
    -- difference is exact.
    baseline_tokens       INTEGER DEFAULT 0,
    counterfactual_tokens INTEGER DEFAULT 0,
    removed_tokens        INTEGER DEFAULT 0,
    -- How the removed tokens were allocated against this turn's real usage buckets. Kept
    -- because it is the entire pricing argument: the same token delta is worth ~12x more
    -- coming out of a cache write than out of a cache read.
    from_cache_write      INTEGER DEFAULT 0,
    from_input            INTEGER DEFAULT 0,
    from_cache_read       INTEGER DEFAULT 0,
    usd                   REAL    DEFAULT 0.0,
    -- 0 means the model had no catalog entry: tokens are real, dollars are absent. Must
    -- never render as $0.00 of saving -- a silent zero looks like a measured result.
    priced                INTEGER DEFAULT 1,
    -- 0 where an edit touched already-cached history, whose cache-write penalty lands on
    -- the NEXT turn and is therefore not netted into `usd`.
    prefix_safe           INTEGER DEFAULT 1,
    edits_applied         INTEGER DEFAULT 0,
    -- The cost side of a truncation, observed rather than assumed: how many proposed cuts
    -- the agent later came back for. `paginated` is excluded from `same_or_earlier` because
    -- a read at a HIGHER offset fetches fresh bytes and would have happened regardless --
    -- counting it would condemn the lever for behaviour it did not cause.
    revisit_candidates    INTEGER DEFAULT 0,
    revisits_observed     INTEGER DEFAULT 0,
    revisits_paginated    INTEGER DEFAULT 0,
    revisits_same_earlier INTEGER DEFAULT 0,
    -- Lever-authored counters, numeric values only -- see LocalStore._numeric_diagnostics.
    diagnostics           TEXT,
    note                  TEXT
);
CREATE INDEX IF NOT EXISTS idx_lever_turns_ts ON lever_turns(ts);
CREATE INDEX IF NOT EXISTS idx_lever_turns_lever ON lever_turns(lever);
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
        self._migrate()
        self._db.commit()

    def _migrate(self) -> None:
        """Add columns a `lever_turns` written by an older build does not have.

        CREATE TABLE IF NOT EXISTS silently keeps the OLD shape when the table already
        exists, so a schema change alone does not reach a developer who has been running the
        sidecar. Every added column is nullable with a default, so an old row reads back as
        "not observed" rather than as a zero re-read rate -- which would be a measurement
        claim nobody made.
        """
        try:
            have = {r[1] for r in self._db.execute("PRAGMA table_info(lever_turns)")}
        except Exception:
            return
        for col in ("revisit_candidates", "revisits_observed", "revisits_paginated",
                    "revisits_same_earlier"):
            if col not in have:
                try:
                    self._db.execute(
                        f"ALTER TABLE lever_turns ADD COLUMN {col} INTEGER DEFAULT 0"
                    )
                except Exception:
                    log.debug("[local_store] could not add %s", col, exc_info=True)

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

    @staticmethod
    def _numeric_diagnostics(diagnostics: Any) -> Optional[str]:
        """A lever's diagnostics, numbers only, as JSON — or ``None``.

        This store's one invariant is that it holds numbers and never text from a developer's
        session. Diagnostics are authored by a third-party lever package, so they are the one
        field here that could carry arbitrary strings — a lever logging the command it
        matched would quietly put a shell line into the database. Numeric values survive,
        everything else is dropped, and the invariant stays a property of the code rather
        than a promise about third-party behaviour.
        """
        if not isinstance(diagnostics, Mapping):
            return None
        clean = {
            str(k): v
            for k, v in diagnostics.items()
            if isinstance(v, (int, float)) and not isinstance(v, bool)
        }
        return json.dumps(clean, sort_keys=True) if clean else None

    def record_lever_turns(self, rows: Iterable[Any]) -> int:
        """Persist measured lever results for one turn. Never raises.

        Takes the whole batch for a turn in one transaction: the rows describe a single
        request, and half of them landing would leave the rail ranking levers against
        different denominators.
        """
        prepared = []
        for m in rows or ():
            try:
                edits = getattr(m, "edits", ()) or ()
                prepared.append((
                    getattr(m, "ts", None) or time.time(),
                    getattr(m, "request_id", ""),
                    getattr(m, "session_id", None),
                    getattr(m, "lever", ""),
                    getattr(m, "mode", ""),
                    getattr(m, "model", ""),
                    int(getattr(m, "baseline_tokens", 0)),
                    int(getattr(m, "counterfactual_tokens", 0)),
                    int(getattr(m, "removed_tokens", 0)),
                    int(getattr(m, "from_cache_write", 0)),
                    int(getattr(m, "from_input", 0)),
                    int(getattr(m, "from_cache_read", 0)),
                    float(getattr(m, "usd", 0.0)),
                    1 if getattr(m, "priced", True) else 0,
                    0 if any(e.applied and not e.prefix_safe for e in edits) else 1,
                    sum(1 for e in edits if e.applied),
                    int(getattr(m, "revisit_candidates", 0)),
                    int(getattr(m, "revisits_observed", 0)),
                    int(getattr(m, "revisits_paginated", 0)),
                    int(getattr(m, "revisits_same_or_earlier", 0)),
                    self._numeric_diagnostics(getattr(m, "diagnostics", None)),
                    getattr(m, "note", ""),
                ))
            except Exception:
                log.debug("[local_store] skipped a malformed lever row", exc_info=True)
        if not prepared:
            return 0
        try:
            with self._lock:
                self._db.executemany(
                    "INSERT INTO lever_turns (ts, request_id, session_id, lever, mode, model,"
                    " baseline_tokens, counterfactual_tokens, removed_tokens,"
                    " from_cache_write, from_input, from_cache_read, usd, priced,"
                    " prefix_safe, edits_applied, revisit_candidates, revisits_observed,"
                    " revisits_paginated, revisits_same_earlier, diagnostics, note)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    prepared,
                )
                self._db.commit()
            return len(prepared)
        except Exception:  # pragma: no cover - defensive
            log.debug("[local_store] failed to record lever turns", exc_info=True)
            return 0

    # -- read --------------------------------------------------------------------------

    def lever_summary(self, since: Optional[float] = None) -> Dict[str, Any]:
        """Measured lever results, aggregated per lever. What the rail's live half renders.

        Aggregated **per lever and never totalled**, the same discipline
        ``levers.ledger.LedgerReport`` documents: two levers can claim the same bytes, so
        adding their savings produces a number larger than anything they could jointly
        deliver. Ranking answers the question actually being asked.

        Only ``priced`` rows contribute dollars. Unpriced rows still contribute their token
        counts and are surfaced separately — a model with no catalog entry saved real tokens,
        and rendering that as $0.00 would read as "this lever does nothing".
        """
        where, args = ("WHERE ts >= ?", (since,)) if since else ("", ())
        with self._lock:
            cur = self._db.execute(
                f"""SELECT lever,
                           COUNT(*)                                        AS turns,
                           COALESCE(SUM(removed_tokens), 0)                AS removed_tokens,
                           COALESCE(SUM(CASE WHEN priced=1 THEN usd END), 0.0) AS usd,
                           COALESCE(SUM(from_cache_write), 0)              AS from_cache_write,
                           COALESCE(SUM(from_input), 0)                    AS from_input,
                           COALESCE(SUM(from_cache_read), 0)               AS from_cache_read,
                           SUM(CASE WHEN priced=0 THEN 1 ELSE 0 END)       AS unpriced_turns,
                           SUM(CASE WHEN prefix_safe=0 THEN 1 ELSE 0 END)  AS unsafe_turns,
                           COALESCE(SUM(edits_applied), 0)                 AS edits_applied,
                           COALESCE(SUM(revisit_candidates), 0)            AS revisit_candidates,
                           COALESCE(SUM(revisits_same_earlier), 0)         AS revisits,
                           COALESCE(SUM(revisits_paginated), 0)            AS revisits_paginated,
                           MAX(ts)                                         AS last_ts
                    FROM lever_turns {where}
                    GROUP BY lever
                    ORDER BY usd DESC""",
                args,
            )
            cols = [c[0] for c in cur.description]
            by_lever = [dict(zip(cols, r)) for r in cur.fetchall()]
            cur = self._db.execute(
                f"SELECT COUNT(*), COUNT(DISTINCT request_id) FROM lever_turns {where}", args
            )
            n_rows, n_turns = cur.fetchone()
        return {
            "by_lever": by_lever,
            "rows": n_rows,
            "turns_observed": n_turns,
            # Deliberately absent: a `total_usd`. See the docstring.
        }

    def recent_lever_turns(self, limit: int = 50) -> List[Dict[str, Any]]:
        with self._lock:
            cur = self._db.execute(
                "SELECT ts, lever, mode, model, removed_tokens, usd, priced, prefix_safe,"
                " edits_applied, note FROM lever_turns ORDER BY ts DESC LIMIT ?",
                (limit,),
            )
            cols = [c[0] for c in cur.description]
            return [dict(zip(cols, r)) for r in cur.fetchall()]

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

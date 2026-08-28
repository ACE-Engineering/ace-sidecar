"""ace.sidecar.insights — local analysis feeding the sidecar dashboard.

Two local sources, no network and no external store:

* **historical** — Claude Code's own transcripts under ``~/.claude/projects``
* **live** — turns this sidecar has relayed (``local_store``, SQLite)

Both stay on the machine. Nothing uploads; nothing reads prompt text.

Transcripts are scanned into the **same shape as the committed corpus**, so
``strategies.py`` scores a developer's own sessions with the identical simulation the
published scorecards use. One implementation, two datasets — a dashboard that computed
savings differently from the analysis would be worse than no dashboard.

Scanning ~125 files takes a second or two, so results are cached and invalidated on
transcript mtime. Date filtering happens **after** the scan, on the cached data, which keeps
range switching instant.
"""

from __future__ import annotations

import glob
import hashlib
import json
import logging
import os
import re
import time
from collections import OrderedDict
from typing import Any, Dict, List, Optional

log = logging.getLogger("ace.sidecar.insights")

TRANSCRIPT_ROOT = os.path.expanduser("~/.claude/projects")
ANTIGRAVITY_ROOT = os.path.expanduser("~/.gemini/antigravity/brain")
CODEX_ROOT = os.path.expanduser("~/.codex/sessions")

AGENT_ALL = "all"
AGENT_CLAUDE = "claude"
AGENT_ANTIGRAVITY = "antigravity"
AGENT_CODEX = "codex"
AGENTS = {
    AGENT_ALL: "All Agents",
    AGENT_CLAUDE: "Claude Code",
    AGENT_ANTIGRAVITY: "Antigravity (Google)",
    AGENT_CODEX: "Codex (OpenAI)",
}

# Thresholds are measured findings from docs/22, not round numbers picked by feel.
CONTEXT_CAP_RECOMMENDED = 200_000  # 69.9% modelled saving at this cap (§14.1)
# Re-measured 2026-07-28: the modelled ladder no longer flattens below 200k (100k saves
# 76.2%, 50k saves 78.4%), so this is a judgement about how much history a coding session
# can lose, not the point where clearing stops paying. §14.1 distrusts the magnitudes.
CONTEXT_PLATEAU = 100_000
CACHE_SHARE_HEALTHY = 0.90  # real coding traffic runs ~98% (§1)
EXPENSIVE_MODELS = {"claude-fable-5"}  # 14.6% of requests, 27.9% of cost (§4.3)

# Idle longer than this reads as "nobody is at the keyboard" rather than "somebody is
# deciding". The cut docs/analysis_docs/2026-07-29-sidecar-approval-and-checkpoint-analysis.md §1 measured
# with; both sides of it are reported (see `parked`) so it stays arguable rather than
# load-bearing. It doubles as the alarm threshold — the point at which a waiting agent is
# worth saying something about.
IDLE_THRESHOLD_S = 300.0
# Above this, a transcript ending on a pending call is abandoned rather than waiting: every
# crashed or closed session looks parked forever, and a two-week-old alarm is noise.
PARKED_ALARM_MAX_S = 6 * 3600.0
# The corpus baselines from that document's §2, so a developer's own figures have something
# to sit against: 227 parked stretches, 26.2% of all idle time, 64 minutes on average.
PARKED_SHARE_OF_IDLE_MEASURED = 0.262
PARKED_MEAN_S_MEASURED = 64 * 60.0

# "session" is not a time window — it selects the single most-recently-active session, which
# is what a developer means by "what am I doing right now". Kept in the same ordered map so
# the UI renders one row of choices, coarsest-to-finest reading left to right.
SESSION = "session"
RANGES: Dict[str, Optional[float]] = {
    SESSION: -1.0,  # sentinel, handled in filter_range
    "24h": 86400.0,
    "7d": 7 * 86400.0,
    "30d": 30 * 86400.0,
    "all": None,
}
RANGE_LABELS = {
    SESSION: "current session",
    "24h": "24h",
    "7d": "7d",
    "30d": "30d",
    "all": "all",
}
DEFAULT_RANGE = "30d"

_BLOCKS = ("text", "thinking", "tool_use", "tool_result", "redacted_thinking")
_TARGET_KEYS = ("file_path", "path", "notebook_path", "pattern", "command")

# Result sizes are carried in bytes and divided by this to reach tokens, so an image's token
# count is converted back to byte-equivalents rather than introducing a second unit halfway
# down the pipeline.
#
# ALIASED, not copied. This is the same quantity as ``strategies.BYTES_PER_TOKEN`` and the two
# must hold one value: `_measure` multiplies an image's real token count by it and
# `strategies.score` divides by it, so images round-trip exactly when they agree and are
# mispriced by their ratio when they do not — silently, with no error. They were two literals
# and they drifted the moment one was corrected against measurement. One name now.
#
# Safe at module level: ``strategies`` imports nothing from here at import time (its own
# consistency check imports lazily inside a function), so this does not close a cycle.
from ace.sidecar.strategies import BYTES_PER_TOKEN as _CHARS_PER_TOKEN
# Anthropic prices an image at roughly width*height/750 tokens.
_IMAGE_TOKENS_PER_PIXEL = 1 / 750
# Screenshot tools state the dimensions in the text block they emit alongside the image —
# "captured screenshot (1451x840, jpeg)" — which is the only place a transcript records them.
_DIMS = re.compile(r"\((\d{2,5})x(\d{2,5}),\s*(?:jpeg|png|webp)\)")

_cache: Dict[str, Any] = {
    "key": None,
    "sessions": None,
    "at": 0.0,
    "roots": None,
}
# How long a transcript fingerprint is trusted without re-walking the tree. See sessions().
_SESSIONS_TTL = 2.0


def _rates(model: str):
    from ace.gateway.pricing import rates_for

    return rates_for(model)


def _target(tool_input: Dict[str, Any]) -> Optional[str]:
    for k in _TARGET_KEYS:
        v = tool_input.get(k)
        if isinstance(v, str) and v:
            return hashlib.sha256(v.encode("utf-8")).hexdigest()[:12]
    return None


def _sig(name: str, tool_input: Dict[str, Any]) -> str:
    """Hash of the *whole* tool input, not just its target.

    ``_target`` keys on ``file_path`` alone, which cannot tell a repeat read from a
    paginated one: three disjoint slices of a 1,119-line file share a target and are
    not duplicates of anything. Keying on the full input is what makes ``offset`` and
    ``limit`` participate, and it is the difference between the de-dup lever measuring
    $36.88 and measuring $0.33 — see ``docs/analysis_docs/2026-07-29-phase2-context-lever-headroom.md``.
    """
    payload = json.dumps({"t": name, "i": tool_input}, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


_TEST_CMD_RE = re.compile(
    r"\b(pytest|npm\s+(?:run\s+)?test|vitest|jest|cargo\s+test|go\s+test|dotnet\s+test|ctest|ruff|eslint|mypy|flake8|pylint|black\s+--check|tsc\s+--noEmit|bundle\s+exec\s+rspec)\b",
    re.IGNORECASE,
)

_ERR_MSG_RE = re.compile(
    r"(encountered error in tool execution|the command exited with code [1-9]|exit code [1-9]|operation not permitted|command failed|fatal:|traceback \(most recent call last\)|syntaxerror|typeerror|keyerror|assertionerror|modulenotfounderror|permission denied|no such file or directory)",
    re.IGNORECASE,
)

_TEST_FILE_RE = re.compile(
    r"(^|[/\\])(tests?|spec|__tests__)[/\\]|(\.|_)(test|spec)\.[a-zA-Z0-9]+$",
    re.IGNORECASE,
)

_SOURCE_FILE_EXTS = (
    ".py",
    ".js",
    ".jsx",
    ".ts",
    ".tsx",
    ".go",
    ".rs",
    ".java",
    ".c",
    ".cpp",
    ".h",
    ".hpp",
    ".cs",
    ".rb",
    ".php",
    ".swift",
    ".kt",
    ".scala",
    ".sh",
    ".html",
    ".css",
    ".vue",
)


def _classify_call(name: str, tool_input: Dict[str, Any]) -> Dict[str, Any]:
    target_raw = None
    for k in (
        "file_path",
        "path",
        "TargetFile",
        "target_file",
        "filename",
        "file",
        "notebook_path",
        "AbsolutePath",
    ):
        v = tool_input.get(k)
        if isinstance(v, str) and v:
            target_raw = v
            break

    cmd = None
    for k in ("command", "CommandLine", "cmd", "input"):
        v = tool_input.get(k)
        if isinstance(v, str) and v:
            cmd = v
            break

    name_lower = name.lower()
    is_test_run = False
    if cmd and _TEST_CMD_RE.search(cmd):
        is_test_run = True

    is_edit = name_lower in (
        "edit",
        "str_replace_editor",
        "write_to_file",
        "replace_file_content",
        "create_file",
        "modify_file_content",
        "save_file",
        "patch",
    ) or (
        name_lower.startswith("edit")
        or name_lower.startswith("write")
        or name_lower.startswith("replace")
    )
    is_view = name_lower in (
        "view",
        "view_file",
        "read_file",
        "cat",
        "open_file",
        "get_file_contents",
    ) or (name_lower.startswith("view") or name_lower.startswith("read"))

    is_test_file = bool(target_raw and _TEST_FILE_RE.search(target_raw))
    is_src_file = bool(
        target_raw
        and any(target_raw.lower().endswith(ext) for ext in _SOURCE_FILE_EXTS)
        and not is_test_file
    )

    return {
        "raw_target": target_raw,
        "is_test_run": is_test_run,
        "is_edit": is_edit,
        "is_view": is_view,
        "is_test_file": is_test_file,
        "is_src_file": is_src_file,
    }


def _measure(body: Any) -> int:
    """A tool_result body -> its size in byte-equivalents, with images priced as images.

    A screenshot arrives as a base64 blob. Measuring it with ``len(json.dumps(body))``
    charges a 1451x840 JPEG about 27,878 tokens where the provider bills roughly
    ``w*h/750`` = 1,625 — a **17x overstatement**. It is not evenly spread: it lands almost
    entirely on the ``supersede`` lever, which is mostly browser screenshot loops, and it
    inflates the tool-result share of prompt volume that every lever is measured against.
    Correcting it is what took supersede from a headline lever to a rounding error in
    ``docs/analysis_docs/2026-07-29-phase2-context-lever-headroom.md`` § 0.
    """
    if isinstance(body, str):
        return len(body)
    if not isinstance(body, list):
        return len(json.dumps(body, default=str)) if body else 0
    chars, img_tokens = 0, 0
    seen = ""
    for blk in body:
        if not isinstance(blk, dict):
            continue
        if blk.get("type") == "image":
            m = _DIMS.search(seen)
            if m:
                w, h = int(m.group(1)), int(m.group(2))
                img_tokens += max(1, int(w * h * _IMAGE_TOKENS_PER_PIXEL))
            else:
                # No stated dimensions. A large payload is a real screenshot, a small one
                # is an icon; both are far closer to these than to len(base64)/4.
                data = (blk.get("source") or {}).get("data") or ""
                img_tokens += 1600 if len(data) > 20_000 else 400
        else:
            t = blk.get("text")
            t = t if isinstance(t, str) else json.dumps(blk, default=str)
            seen += t
            chars += len(t)
    return chars + int(img_tokens * _CHARS_PER_TOKEN)


def _digest(body: Any) -> Optional[str]:
    """Hash of a tool result's content, so redundancy can be *proved*, not inferred.

    Equal sizes are not equal bytes. The de-dup lever only fires where the content is
    provably already resident, and that requires comparing content rather than length.
    """
    if isinstance(body, str):
        text = body
    elif isinstance(body, list):
        parts = []
        for blk in body:
            if not isinstance(blk, dict):
                continue
            # An image's base64 payload is skipped: it is enormous, and two screenshots
            # of the same page are never byte-identical anyway.
            if blk.get("type") == "image":
                parts.append("<image>")
            else:
                t = blk.get("text")
                parts.append(t if isinstance(t, str) else json.dumps(blk, default=str))
        text = "".join(parts)
    elif body:
        text = json.dumps(body, default=str)
    else:
        return None
    if not text:
        return None
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _merge(turn: Dict[str, Any], extra: Dict[str, Any]) -> None:
    """Fold another record of the same API response into the turn already emitted.

    Mirrors ``scripts/extract_session_usage.py``: prompt tokens repeat identically on every
    record of a response and must be taken once, while content accumulates. ``output_tokens``
    takes the maximum because an interrupted stream leaves partial counts behind.
    """
    turn["output_tokens"] = max(turn["output_tokens"], extra["output_tokens"])
    if extra.get("stop_reason"):
        turn["stop_reason"] = extra["stop_reason"]
    for bt, n in (extra.get("blocks") or {}).items():
        turn["blocks"][bt] = turn["blocks"].get(bt, 0) + n
    turn["calls"].extend(extra.get("calls") or [])


def _scan(root: str) -> List[Dict[str, Any]]:
    """Transcripts -> corpus-shaped sessions. Numbers and hashes only.

    One emitted turn is **one API request**. Claude Code writes one record per content block
    and repeats the whole ``usage`` object on each, so records are joined on ``message.id``
    exactly as the corpus extractor does. Counting per record instead inflates prompt volume
    1.95x and output 2.34x — the bug fixed in the corpus on 2026-07-28, which lived here too
    because this scanner is a second implementation of the same read.
    """
    sessions: List[Dict[str, Any]] = []
    # (project, on-disk session id) -> emitted session name, so a subagent run can point at
    # the session that spawned it. Neither id is kept.
    main_ids: Dict[Any, str] = {}
    for path in sorted(glob.glob(os.path.join(root, "**", "*.jsonl"), recursive=True)):
        rel = os.path.relpath(path, root)
        parts = rel.split(os.sep)
        is_sub = f"{os.sep}subagents{os.sep}" in rel
        # `<project>/<sid>.jsonl` for a session, `<project>/<sid>/subagents/<x>.jsonl` for
        # the subagents it spawned.
        sid = parts[1] if is_sub else os.path.basename(path)[: -len(".jsonl")]
        turns: List[Dict[str, Any]] = []
        result_bytes: Dict[str, int] = {}
        # tool_use_id -> short hash of the result content, so the de-dup lever can prove
        # "already in context" instead of inferring it from a matching path.
        result_digests: Dict[str, str] = {}
        result_errors: Dict[str, bool] = {}
        seen_ids: Dict[str, int] = {}  # message id -> index of its turn in `turns`
        cwds: List[str] = []
        # The timeline `time_budget` and `parked` read: (start, kind, tool names, end). Only
        # the user-side half is collected here; assistant events are derived after the
        # message-id join below, for the reason given there.
        events: List[Any] = []
        end_of: Dict[int, float] = {}  # turn index -> last record's epoch
        first_snippet: str = ""
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except Exception:
                        continue
                    kind = rec.get("type")
                    msg = rec.get("message") or {}
                    cwd = rec.get("cwd")
                    if cwd and (not cwds or cwds[-1] != cwd):
                        # Where the work happened, for the commit join. Never rendered.
                        cwds.append(cwd)
                    if kind == "user":
                        content = msg.get("content")
                        if not first_snippet:
                            text = content if isinstance(content, str) else ""
                            if isinstance(content, list):
                                for b in content:
                                    if (
                                        isinstance(b, dict)
                                        and b.get("type") == "text"
                                    ):
                                        text = b.get("text") or ""
                                        break
                            text = " ".join(str(text).split())
                            if text and not text.startswith("<"):
                                first_snippet = text[:110]
                        saw_result = False
                        if isinstance(content, list):
                            for b in content:
                                if (
                                    isinstance(b, dict)
                                    and b.get("type") == "tool_result"
                                ):
                                    saw_result = True
                                    body = b.get("content")
                                    tid = b.get("tool_use_id")
                                    if tid:
                                        result_bytes[tid] = _measure(body)
                                        dg = _digest(body)
                                        if dg:
                                            result_digests[tid] = dg
                                        if b.get("is_error") or str(
                                            b.get("status", "")
                                        ).lower() in ("error", "failed"):
                                            result_errors[tid] = True
                        # A tool finishing and a human typing are both "user" records and
                        # they mean opposite things about who the session is waiting on.
                        at = _epoch(rec.get("timestamp"))
                        if at:
                            events.append(
                                (at, "tool_result" if saw_result else "prompt", (), at)
                            )
                        continue
                    if kind != "assistant":
                        continue
                    u = msg.get("usage") or {}
                    if not u:
                        continue
                    cc = u.get("cache_creation") or {}
                    calls, blocks = [], {}
                    for b in msg.get("content") or []:
                        bt = b.get("type")
                        if bt in _BLOCKS:
                            blocks[bt] = blocks.get(bt, 0) + 1
                        if bt == "tool_use":
                            ti = b.get("input") or {}
                            nm = b.get("name") or "?"
                            cl = _classify_call(nm, ti)
                            calls.append(
                                {
                                    "id": b.get("id"),
                                    "name": nm,
                                    "target": _target(ti),
                                    "sig": _sig(nm, ti),
                                    "raw_target": cl["raw_target"],
                                    "is_test_run": cl["is_test_run"],
                                    "is_edit": cl["is_edit"],
                                    "is_view": cl["is_view"],
                                    "is_test_file": cl["is_test_file"],
                                    "is_src_file": cl["is_src_file"],
                                }
                            )
                    turn = {
                        "model": msg.get("model") or "",
                        "stop_reason": msg.get("stop_reason"),
                        "input_tokens": int(u.get("input_tokens") or 0),
                        "output_tokens": int(u.get("output_tokens") or 0),
                        "cache_read_input_tokens": int(
                            u.get("cache_read_input_tokens") or 0
                        ),
                        "cache_creation_input_tokens": int(
                            u.get("cache_creation_input_tokens") or 0
                        ),
                        "ephemeral_5m_input_tokens": int(
                            cc.get("ephemeral_5m_input_tokens") or 0
                        ),
                        "ephemeral_1h_input_tokens": int(
                            cc.get("ephemeral_1h_input_tokens") or 0
                        ),
                        "blocks": blocks,
                        "calls": calls,
                        "ts": rec.get("timestamp"),
                    }
                    at = _epoch(turn["ts"])
                    message_id = msg.get("id")
                    if message_id is not None and message_id in seen_ids:
                        idx = seen_ids[message_id]
                        _merge(turns[idx], turn)
                        # A response's blocks are written as separate records, so a turn STARTS
                        # at the first and ENDS at the last — and the last is the earliest a
                        # tool could have run. Collapsing both onto the start would bill the
                        # model's own generation time to `tool execution + approval`, the one
                        # bucket time_budget promises not to overstate.
                        if at:
                            end_of[idx] = max(end_of.get(idx, at), at)
                        continue
                    if message_id is not None:
                        seen_ids[message_id] = len(turns)
                    if at:
                        end_of[len(turns)] = at
                    turns.append(turn)
        except Exception:  # pragma: no cover - a locked or partial file is normal
            continue
        for t in turns:
            for c in t["calls"]:
                cid = c.pop("id", None)
                if cid is not None:
                    n = result_bytes.get(cid)
                    if n is not None:
                        c["result_bytes"] = n
                    dg = result_digests.get(cid)
                    if dg:
                        c["digest"] = dg
                    if cid in result_errors:
                        c["is_error"] = True
        # Assistant events are derived here rather than inside the loop above: a turn's
        # ``tool_use`` blocks can be spread across several records sharing one message id (see
        # the docstring), so the complete call list only exists once the join is done. Reading
        # them per record would leave a turn's first event holding no tools and the parked
        # detector blind to exactly the turns it exists to find.
        for i, t in enumerate(turns):
            at = _epoch(t.get("ts"))
            if at:
                events.append(
                    (
                        at,
                        "assistant",
                        tuple(c.get("name") for c in t["calls"]),
                        max(end_of.get(i, at), at),
                    )
                )
        events.sort(key=lambda e: e[0])
        if turns:
            name = f"s{len(sessions)+1}"
            session: Dict[str, Any] = {
                "session": name,
                "kind": "subagent" if is_sub else "main",
                "agent_type": AGENT_CLAUDE,
                "turns": turns,
                "events": events,
                "cwds": cwds,
                "path": path,
                "bytes": os.path.getsize(path) if os.path.exists(path) else 0,
                "mtime": os.path.getmtime(path) if os.path.exists(path) else 0.0,
                "snippet": first_snippet,
            }
            if is_sub:
                parent = main_ids.get((parts[0], sid))
                if parent:
                    session["parent"] = parent
            else:
                main_ids[(parts[0], sid)] = name
            sessions.append(session)
    return sessions


def _scan_antigravity(root: str) -> List[Dict[str, Any]]:
    """Antigravity/Google Coding Agent transcripts -> corpus-shaped sessions."""
    sessions: List[Dict[str, Any]] = []
    if not os.path.isdir(root):
        return sessions

    paths = []
    for dirpath, _, filenames in os.walk(root):
        for fn in filenames:
            if fn in ("transcript.jsonl", "transcript_full.jsonl"):
                paths.append(os.path.join(dirpath, fn))

    seen_dirs = set()
    filtered_paths = []
    for p in sorted(paths):
        d = os.path.dirname(p)
        if os.path.basename(p) == "transcript.jsonl":
            filtered_paths.append(p)
            seen_dirs.add(d)
        elif d not in seen_dirs:
            filtered_paths.append(p)
            seen_dirs.add(d)

    for path in filtered_paths:
        turns: List[Dict[str, Any]] = []
        events: List[Any] = []
        cwds: List[str] = []
        cid = (
            os.path.basename(path.split(".system_generated")[0].rstrip(os.sep))
            if ".system_generated" in path
            else os.path.basename(os.path.dirname(path))
        )

        result_bytes: Dict[str, int] = {}
        result_digests: Dict[str, str] = {}
        result_errors: Dict[str, bool] = {}

        first_snippet: str = ""
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except Exception:
                        continue

                    stype = str(rec.get("type", "")).upper()
                    ssource = str(rec.get("source", "")).upper()
                    ts_str = rec.get("timestamp") or rec.get("created_at")
                    at = _epoch(ts_str) if ts_str else os.path.getmtime(path)

                    if stype in ("USER_INPUT", "USER") or ssource == "USER_EXPLICIT":
                        if not first_snippet:
                            text = rec.get("content") or rec.get("text") or ""
                            text = " ".join(str(text).split())
                            if text and not text.startswith("<"):
                                first_snippet = text[:110]
                        events.append((at, "prompt", (), at))
                        current_turn_calls = None
                        continue

                    if (
                        stype in ("PLANNER_RESPONSE", "MODEL_RESPONSE", "MODEL")
                        or ssource == "MODEL"
                    ):
                        content = rec.get("content") or rec.get("text") or ""
                        tool_calls = (
                            rec.get("tool_calls")
                            or rec.get("calls")
                            or rec.get("tool_use")
                            or []
                        )

                        calls = []
                        for idx_c, tc in enumerate(tool_calls):
                            if isinstance(tc, dict):
                                nm = tc.get("name") or "tool"
                                args = tc.get("args") or tc.get("input") or {}
                                cl = _classify_call(nm, args)
                                calls.append(
                                    {
                                        "name": nm,
                                        "target": _target(args),
                                        "sig": _sig(nm, args),
                                        "raw_target": cl["raw_target"],
                                        "is_test_run": cl["is_test_run"],
                                        "is_edit": cl["is_edit"],
                                        "is_view": cl["is_view"],
                                        "is_test_file": cl["is_test_file"],
                                        "is_src_file": cl["is_src_file"],
                                        "is_error": False,
                                    }
                                )

                        usage = rec.get("usage") or {}
                        in_tok = int(
                            usage.get("input_tokens") or usage.get("prompt_tokens") or 0
                        )
                        out_tok = int(
                            usage.get("output_tokens")
                            or usage.get("completion_tokens")
                            or 0
                        )
                        cr_tok = int(
                            usage.get("cache_read_input_tokens")
                            or usage.get("cached_prompt_tokens")
                            or 0
                        )
                        cw_tok = int(usage.get("cache_creation_input_tokens") or 0)

                        if not in_tok and content:
                            in_tok = max(10, len(str(content)) // 4)
                        if not out_tok:
                            out_tok = max(5, len(str(content)) // 8) if content else 10

                        blocks = {}
                        if content:
                            blocks["text"] = 1
                        if calls:
                            blocks["tool_use"] = len(calls)

                        model = rec.get("model") or "gemini-3.6-flash"

                        turn = {
                            "model": model,
                            "stop_reason": rec.get("stop_reason") or "end_turn",
                            "input_tokens": in_tok,
                            "output_tokens": out_tok,
                            "cache_read_input_tokens": cr_tok,
                            "cache_creation_input_tokens": cw_tok,
                            "ephemeral_5m_input_tokens": 0,
                            "ephemeral_1h_input_tokens": 0,
                            "blocks": blocks,
                            "calls": calls,
                            "ts": (
                                ts_str
                                or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(at))
                            ),
                        }
                        turns.append(turn)
                        current_turn_calls = calls if calls else None
                        events.append(
                            (at, "assistant", tuple(c["name"] for c in calls), at)
                        )
                        continue

                    if current_turn_calls and (
                        stype in ("GENERIC", "TOOL_RESULT", "SYSTEM_MESSAGE", "SYSTEM_RESULT")
                        or ssource in ("SYSTEM", "MODEL")
                    ):
                        body = rec.get("content") or rec.get("output") or rec.get("result") or ""
                        st = str(rec.get("status", "")).upper()
                        is_err = (
                            st in ("ERROR", "FAILED")
                            or bool(rec.get("error"))
                            or bool(rec.get("is_error"))
                            or bool(_ERR_MSG_RE.search(str(body)))
                        )
                        for c in current_turn_calls:
                            if is_err:
                                c["is_error"] = True
                            c["result_bytes"] = _measure(body)
                            dg = _digest(body)
                            if dg:
                                c["digest"] = dg
                        events.append((at, "tool_result", (), at))
                        continue
        except Exception:
            continue

        if turns:
            events.sort(key=lambda e: e[0])
            name = f"agy_{cid[:8]}" if cid else f"agy_{len(sessions)+1}"
            session = {
                "session": name,
                "kind": "main",
                "agent_type": AGENT_ANTIGRAVITY,
                "turns": turns,
                "events": events,
                "cwds": cwds,
                "path": path,
                "bytes": os.path.getsize(path) if os.path.exists(path) else 0,
                "mtime": os.path.getmtime(path) if os.path.exists(path) else 0.0,
                "snippet": first_snippet,
            }
            sessions.append(session)

    return sessions


def _scan_codex(root: str) -> List[Dict[str, Any]]:
    """Codex/OpenAI Coding Agent transcripts -> corpus-shaped sessions."""
    sessions: List[Dict[str, Any]] = []
    if not os.path.isdir(root):
        return sessions

    paths = []
    for dirpath, _, filenames in os.walk(root):
        for fn in filenames:
            if fn.endswith(".jsonl") or fn.endswith(".json"):
                paths.append(os.path.join(dirpath, fn))

    for path in sorted(paths):
        turns: List[Dict[str, Any]] = []
        events: List[Any] = []
        cwds: List[str] = []
        sid = os.path.splitext(os.path.basename(path))[0]

        result_bytes: Dict[str, int] = {}
        result_digests: Dict[str, str] = {}
        result_errors: Dict[str, bool] = {}
        first_snippet: str = ""

        try:
            records: List[Dict[str, Any]] = []
            if path.endswith(".jsonl"):
                with open(path, "r", encoding="utf-8", errors="replace") as fh:
                    for line in fh:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            records.append(json.loads(line))
                        except Exception:
                            continue
            else:
                with open(path, "r", encoding="utf-8", errors="replace") as fh:
                    data = json.load(fh)
                    if isinstance(data, list):
                        records = [d for d in data if isinstance(d, dict)]
                    elif isinstance(data, dict):
                        if "messages" in data and isinstance(data["messages"], list):
                            records = [d for d in data["messages"] if isinstance(d, dict)]
                            if "cwd" in data:
                                cwds.append(str(data["cwd"]))
                        elif "turns" in data and isinstance(data["turns"], list):
                            records = [d for d in data["turns"] if isinstance(d, dict)]
                        else:
                            records = [data]

            session_model = "gpt-5.3-codex"
            current_calls: List[Dict[str, Any]] = []

            for rec in records:
                stype = str(rec.get("type", "")).lower()
                payload = (
                    rec.get("payload")
                    if isinstance(rec.get("payload"), dict)
                    else {}
                )
                ptype = str(payload.get("type", "")).lower() if payload else ""
                srole = str(
                    rec.get("role") or payload.get("role") or ""
                ).lower()
                ts_str = (
                    rec.get("timestamp")
                    or rec.get("created_at")
                    or payload.get("timestamp")
                )
                at = _epoch(ts_str) if ts_str else os.path.getmtime(path)

                if stype == "session_meta":
                    if payload.get("cwd") and str(payload["cwd"]) not in cwds:
                        cwds.append(str(payload["cwd"]))
                    m = (
                        (payload.get("base_instructions") or {})
                        .get("provenance", {})
                        .get("model")
                    )
                    if m:
                        session_model = str(m)
                    continue

                if stype == "world_state":
                    m = (payload.get("state") or {}).get("model")
                    if m:
                        session_model = str(m)
                    env_cwd = (
                        (
                            ((payload.get("state") or {}).get("environments") or {})
                            .get("environments")
                            or {}
                        )
                        .get("local", {})
                        .get("cwd")
                    )
                    if env_cwd and str(env_cwd) not in cwds:
                        cwds.append(str(env_cwd))
                    continue

                if rec.get("cwd") and str(rec["cwd"]) not in cwds:
                    cwds.append(str(rec["cwd"]))
                if payload.get("cwd") and str(payload["cwd"]) not in cwds:
                    cwds.append(str(payload["cwd"]))

                # Handle response_item custom_tool_call
                if stype == "response_item" and ptype == "custom_tool_call":
                    nm = payload.get("name") or "tool"
                    args = payload.get("input") or {}
                    if isinstance(args, str):
                        try:
                            args = json.loads(args)
                        except Exception:
                            args = {"raw": args}
                    cid = (
                        payload.get("call_id")
                        or payload.get("id")
                        or f"call_{len(turns)}_{len(current_calls)}"
                    )
                    cl = _classify_call(nm, args)
                    current_calls.append(
                        {
                            "id": cid,
                            "name": nm,
                            "target": _target(args),
                            "sig": _sig(nm, args),
                            "raw_target": cl["raw_target"],
                            "is_test_run": cl["is_test_run"],
                            "is_edit": cl["is_edit"],
                            "is_view": cl["is_view"],
                            "is_test_file": cl["is_test_file"],
                            "is_src_file": cl["is_src_file"],
                        }
                    )
                    continue

                if stype == "response_item" and ptype == "custom_tool_call_output":
                    cid = payload.get("call_id") or payload.get("id")
                    out_body = payload.get("output") or ""
                    if cid:
                        result_bytes[cid] = _measure(out_body)
                        dg = _digest(out_body)
                        if dg:
                            result_digests[cid] = dg
                        if (
                            payload.get("exit_code") not in (None, 0)
                            or bool(payload.get("is_error"))
                            or str(payload.get("status", "")).lower()
                            in ("error", "failed")
                        ):
                            result_errors[cid] = True
                    events.append((at, "tool_result", (), at))
                    continue

                # Handle event_msg token_count
                if stype == "event_msg" and ptype == "token_count":
                    info = payload.get("info") or {}
                    last_u = info.get("last_token_usage") or {}
                    in_tok = int(last_u.get("input_tokens") or 0)
                    cached_tok = int(last_u.get("cached_input_tokens") or 0)
                    out_tok = int(last_u.get("output_tokens") or 0)
                    cw_tok = int(last_u.get("cache_write_input_tokens") or 0)
                    fresh_tok = (
                        max(0, in_tok - cached_tok)
                        if in_tok >= cached_tok
                        else in_tok
                    )

                    blocks = {"text": 1}
                    if current_calls:
                        blocks["tool_use"] = len(current_calls)

                    turn = {
                        "model": session_model,
                        "stop_reason": "end_turn",
                        "input_tokens": fresh_tok,
                        "output_tokens": out_tok,
                        "cache_read_input_tokens": cached_tok,
                        "cache_creation_input_tokens": cw_tok,
                        "ephemeral_5m_input_tokens": 0,
                        "ephemeral_1h_input_tokens": 0,
                        "blocks": blocks,
                        "calls": list(current_calls),
                        "ts": (
                            ts_str
                            or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(at))
                        ),
                    }
                    turns.append(turn)
                    events.append(
                        (at, "assistant", tuple(c["name"] for c in current_calls), at)
                    )
                    current_calls = []
                    continue

                if (
                    srole == "user"
                    or stype in ("user", "user_input", "prompt")
                    or (stype == "response_item" and ptype == "message" and srole == "user")
                ):
                    if not first_snippet:
                        c = (
                            payload.get("content")
                            if ptype == "message"
                            else (rec.get("content") or rec.get("text") or "")
                        )
                        text = ""
                        if isinstance(c, list):
                            for b in c:
                                if isinstance(b, dict) and b.get("type") in ("input_text", "text"):
                                    txt_val = b.get("text") or ""
                                    if txt_val and not txt_val.strip().startswith("<"):
                                        text = txt_val
                                        break
                        elif isinstance(c, str):
                            text = c
                        text = " ".join(str(text).split())
                        if text and not text.startswith("<"):
                            first_snippet = text[:110]
                    events.append((at, "prompt", (), at))
                    continue

                if srole in ("tool", "function") or stype in (
                    "tool_result",
                    "function_call_output",
                    "tool_output",
                ):
                    body = (
                        rec.get("content")
                        or rec.get("output")
                        or rec.get("result")
                        or ""
                    )
                    tid = (
                        rec.get("tool_call_id")
                        or rec.get("tool_use_id")
                        or rec.get("id")
                        or f"call_{len(events)}"
                    )
                    result_bytes[tid] = _measure(body)
                    dg = _digest(body)
                    if dg:
                        result_digests[tid] = dg
                    if (
                        rec.get("exit_code") not in (None, 0)
                        or bool(rec.get("is_error"))
                        or str(rec.get("status", "")).lower() in ("error", "failed")
                    ):
                        result_errors[tid] = True
                    events.append((at, "tool_result", (), at))
                    continue

                if srole == "assistant" or stype in ("assistant", "model", "response"):
                    content = rec.get("content") or rec.get("text") or ""
                    msg = (
                        rec.get("message")
                        if isinstance(rec.get("message"), dict)
                        else {}
                    )
                    if not content and msg:
                        content = msg.get("content") or ""

                    tool_calls = (
                        rec.get("tool_calls")
                        or rec.get("calls")
                        or msg.get("tool_calls")
                        or []
                    )

                    calls = []
                    for idx_c, tc in enumerate(tool_calls):
                        if isinstance(tc, dict):
                            fn_obj = (
                                tc.get("function")
                                if isinstance(tc.get("function"), dict)
                                else tc
                            )
                            nm = fn_obj.get("name") or "tool"
                            args = (
                                fn_obj.get("arguments")
                                or fn_obj.get("args")
                                or fn_obj.get("input")
                                or {}
                            )
                            if isinstance(args, str):
                                try:
                                    args = json.loads(args)
                                except Exception:
                                    args = {"raw": args}
                            call_id = tc.get("id") or f"call_{len(turns)}_{idx_c}"
                            cl = _classify_call(nm, args)
                            calls.append(
                                {
                                    "id": call_id,
                                    "name": nm,
                                    "target": _target(args),
                                    "sig": _sig(nm, args),
                                    "raw_target": cl["raw_target"],
                                    "is_test_run": cl["is_test_run"],
                                    "is_edit": cl["is_edit"],
                                    "is_view": cl["is_view"],
                                    "is_test_file": cl["is_test_file"],
                                    "is_src_file": cl["is_src_file"],
                                }
                            )

                    usage = rec.get("usage") or msg.get("usage") or {}
                    in_tok = int(
                        usage.get("prompt_tokens") or usage.get("input_tokens") or 0
                    )
                    out_tok = int(
                        usage.get("completion_tokens")
                        or usage.get("output_tokens")
                        or 0
                    )

                    cached_details = usage.get("prompt_tokens_details") or {}
                    cr_tok = int(
                        cached_details.get("cached_tokens")
                        or usage.get("cache_read_input_tokens")
                        or usage.get("cached_prompt_tokens")
                        or 0
                    )
                    cw_tok = int(usage.get("cache_creation_input_tokens") or 0)

                    fresh_tok = (
                        max(0, in_tok - cr_tok) if in_tok >= cr_tok else in_tok
                    )

                    if not in_tok and content:
                        fresh_tok = max(10, len(str(content)) // 4)
                    if not out_tok:
                        out_tok = (
                            max(5, len(str(content)) // 8) if content else 10
                        )

                    blocks = {}
                    if content:
                        blocks["text"] = 1
                    if calls:
                        blocks["tool_use"] = len(calls)

                    model = (
                        rec.get("model")
                        or msg.get("model")
                        or session_model
                    )

                    turn = {
                        "model": model,
                        "stop_reason": rec.get("stop_reason") or "end_turn",
                        "input_tokens": fresh_tok,
                        "output_tokens": out_tok,
                        "cache_read_input_tokens": cr_tok,
                        "cache_creation_input_tokens": cw_tok,
                        "ephemeral_5m_input_tokens": 0,
                        "ephemeral_1h_input_tokens": 0,
                        "blocks": blocks,
                        "calls": calls,
                        "ts": (
                            ts_str
                            or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(at))
                        ),
                    }
                    turns.append(turn)
                    events.append(
                        (at, "assistant", tuple(c["name"] for c in calls), at)
                    )
        except Exception:
            continue

        if turns:
            for t in turns:
                for c in t["calls"]:
                    cid_tag = c.pop("id", None)
                    if cid_tag and cid_tag in result_bytes:
                        c["result_bytes"] = result_bytes[cid_tag]
                    if cid_tag and cid_tag in result_digests:
                        c["digest"] = result_digests[cid_tag]
                    if cid_tag and cid_tag in result_errors:
                        c["is_error"] = True

            if not first_snippet:
                try:
                    idx_file = os.path.join(root, "session_index.jsonl")
                    if not os.path.exists(idx_file):
                        idx_file = os.path.join(os.path.dirname(root), "session_index.jsonl")
                    if os.path.exists(idx_file):
                        with open(idx_file, "r", encoding="utf-8", errors="replace") as fh:
                            for l_idx in fh:
                                l_idx = l_idx.strip()
                                if l_idx:
                                    d_idx = json.loads(l_idx)
                                    sid_idx = d_idx.get("id") or ""
                                    if sid_idx and sid_idx in path:
                                        if d_idx.get("thread_name"):
                                            first_snippet = d_idx["thread_name"][:110]
                                            break
                except Exception:
                    pass

            events.sort(key=lambda e: e[0])
            name = f"codex_{sid}" if sid else f"codex_{len(sessions)+1}"
            session: Dict[str, Any] = {
                "session": name,
                "kind": "main",
                "agent_type": AGENT_CODEX,
                "turns": turns,
                "events": events,
                "cwds": cwds,
                "path": path,
                "bytes": os.path.getsize(path) if os.path.exists(path) else 0,
                "mtime": os.path.getmtime(path) if os.path.exists(path) else 0.0,
                "snippet": first_snippet,
            }
            sessions.append(session)

    return sessions


def sessions(
    root: Optional[str] = None,
    antigravity_root: Optional[str] = None,
    codex_root: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Cached scan of Claude Code, Antigravity, and Codex transcripts.

    The cache key is a fingerprint over every transcript file, so building it means a
    recursive glob plus a stat per file — ~0.35s against a real transcript directory, paid
    on cache *hits* as much as misses. Toggling an agent in the dashboard re-enters this
    function several times, so a short freshness window lets a burst of requests reuse the
    fingerprint instead of re-walking the tree for each one. Transcripts are appended by a
    live agent, so the window has to stay well under the time it takes a user to notice a
    turn is missing; a couple of seconds buys the whole toggle without being perceptible.
    """
    if root is None:
        root = TRANSCRIPT_ROOT
    if antigravity_root is None:
        antigravity_root = ANTIGRAVITY_ROOT
    if codex_root is None:
        codex_root = CODEX_ROOT

    now = time.time()
    if (
        _cache["sessions"] is not None
        and _cache["roots"] == (root, antigravity_root, codex_root)
        and now - _cache["at"] < _SESSIONS_TTL
    ):
        return _cache["sessions"]

    c_exists = os.path.isdir(root)
    a_exists = os.path.isdir(antigravity_root)
    x_exists = os.path.isdir(codex_root)

    if not c_exists and not a_exists and not x_exists:
        return []

    c_files = (
        glob.glob(os.path.join(root, "**", "*.jsonl"), recursive=True)
        if c_exists
        else []
    )
    a_files = (
        glob.glob(os.path.join(antigravity_root, "**", "*.jsonl"), recursive=True)
        if a_exists
        else []
    )
    x_files = (
        glob.glob(os.path.join(codex_root, "**", "*.json*"), recursive=True)
        if x_exists
        else []
    )
    all_files = c_files + a_files + x_files

    key = (
        f"{len(all_files)}:{max((os.path.getmtime(f) for f in all_files), default=0):.0f}:"
        f"{root}:{antigravity_root}:{codex_root}"
    )
    if _cache["key"] != key:
        c_sess = _scan(root) if c_exists else []
        a_sess = _scan_antigravity(antigravity_root) if a_exists else []
        x_sess = _scan_codex(codex_root) if x_exists else []
        combined = c_sess + a_sess + x_sess
        combined.sort(
            key=lambda s: (
                max((_epoch(t.get("ts")) or 0.0) for t in s["turns"])
                if s.get("turns")
                else 0.0
            ),
            reverse=True,
        )
        _cache["sessions"] = combined
        _cache["key"] = key
    # Refreshed whether or not the scan ran: reaching here means the fingerprint was just
    # checked against the filesystem, so the cached sessions are known-current as of now and
    # the freshness window restarts. Refreshing only on a miss would expire the window on
    # every call once the data settled, putting the glob back in the toggle path for good.
    _cache["at"] = time.time()
    _cache["roots"] = (root, antigravity_root, codex_root)
    return _cache["sessions"] or []


def _epoch(ts: Optional[str]) -> Optional[float]:
    if not ts:
        return None
    try:
        import datetime

        return datetime.datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def _latest_session(sess: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The single session with the most recent turn — "what I am working on now"."""
    best, best_ts = None, -1.0
    for s in sess:
        last = max((_epoch(t.get("ts")) or 0.0) for t in s["turns"])
        if last > best_ts:
            best, best_ts = s, last
    return [best] if best else []


def filter_range(
    sess: List[Dict[str, Any]],
    window: Optional[float],
    agent: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Keep turns inside the window and filter by agent type if requested."""
    if agent and agent not in (AGENT_ALL, None):
        sess = [s for s in sess if s.get("agent_type", AGENT_CLAUDE) == agent]
    if window == -1.0:
        return _latest_session(sess)
    if not window:
        return sess
    cutoff = time.time() - window
    out = []
    for s in sess:
        keep = [t for t in s["turns"] if (_epoch(t.get("ts")) or 0) >= cutoff]
        if keep:
            ev = [e for e in (s.get("events") or []) if e[0] >= cutoff]
            out.append({**s, "turns": keep, "events": ev})
    return out


def agent_breakdown(sess: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Summary metrics split across heterogeneous agent environments."""
    out: Dict[str, Any] = {
        AGENT_CLAUDE: {
            "agent_type": AGENT_CLAUDE,
            "label": "Claude Code",
            "sessions": 0,
            "turns": 0,
            "prompt_tokens": 0,
            "output_tokens": 0,
            "cache_read_tokens": 0,
            "cost_usd": 0.0,
            "models": set(),
        },
        AGENT_ANTIGRAVITY: {
            "agent_type": AGENT_ANTIGRAVITY,
            "label": "Antigravity (Google)",
            "sessions": 0,
            "turns": 0,
            "prompt_tokens": 0,
            "output_tokens": 0,
            "cache_read_tokens": 0,
            "cost_usd": 0.0,
            "models": set(),
        },
        AGENT_CODEX: {
            "agent_type": AGENT_CODEX,
            "label": "Codex (OpenAI)",
            "sessions": 0,
            "turns": 0,
            "prompt_tokens": 0,
            "output_tokens": 0,
            "cache_read_tokens": 0,
            "cost_usd": 0.0,
            "models": set(),
        },
    }
    for s in sess:
        atype = s.get("agent_type", AGENT_CLAUDE)
        if atype not in out:
            out[atype] = {
                "agent_type": atype,
                "label": atype.title(),
                "sessions": 0,
                "turns": 0,
                "prompt_tokens": 0,
                "output_tokens": 0,
                "cache_read_tokens": 0,
                "cost_usd": 0.0,
                "models": set(),
            }
        e = out[atype]
        e["sessions"] += 1
        for t in s["turns"]:
            e["turns"] += 1
            fresh, cr = t["input_tokens"], t["cache_read_input_tokens"]
            cw, o = t["cache_creation_input_tokens"], t["output_tokens"]
            e["prompt_tokens"] += fresh + cr + cw
            e["output_tokens"] += o
            e["cache_read_tokens"] += cr
            if t.get("model"):
                e["models"].add(t["model"])
            r = _rates(t["model"])
            if r:
                m = 1e6
                w5, w1 = (
                    t["ephemeral_5m_input_tokens"],
                    t["ephemeral_1h_input_tokens"],
                )
                write = (
                    w5 / m * r.cache_write_per_mtok("5m")
                    + w1 / m * r.cache_write_per_mtok("1h")
                    if (w5 or w1)
                    else cw / m * r.cache_write_per_mtok("5m")
                )
                cost = (
                    fresh / m * r.input_per_mtok
                    + cr / m * r.cache_read_per_mtok
                    + o / m * r.output_per_mtok
                    + write
                )
                e["cost_usd"] += cost

    for k, v in out.items():
        v["models"] = sorted(list(v["models"]))
    return out


# ----------------------------------------------------------------- fleet metrics (§0)

# Git logs are read per dashboard render, so they are cached briefly. A commit landing is
# not a sub-minute-latency event and shelling out four times per page load is not free.
_git_cache: Dict[str, Any] = {"key": None, "at": 0.0, "repos": None}
_GIT_TTL = 60.0


def _pct(values: List[float], q: float) -> float:
    """Linear-interpolated percentile. Empty -> 0.0."""
    if not values:
        return 0.0
    o = sorted(values)
    if len(o) == 1:
        return float(o[0])
    pos = (len(o) - 1) * q
    lo = int(pos)
    hi = min(lo + 1, len(o) - 1)
    return float(o[lo] + (o[hi] - o[lo]) * (pos - lo))


def developer_sessions(sess: List[Dict[str, Any]]) -> List[List[int]]:
    """Indices grouped into developer sessions — a main transcript plus its subagents.

    A subagent run is not a session a developer had; it is work inside one. Counting the two
    alike would report 125 "sessions" where a developer had 47, and every per-session figure
    would be diluted by the short-lived agents the long sessions spawned.
    """
    groups: List[List[int]] = []
    index: Dict[str, int] = {}
    for i, s in enumerate(sess):
        if s.get("kind") != "subagent" or not groups:
            index[s.get("session", "")] = len(groups)
            groups.append([i])
            continue
        groups[index.get(s.get("parent"), len(groups) - 1)].append(i)
    return groups


def _git(root: str, *args: str) -> List[str]:
    import subprocess

    try:
        p = subprocess.run(
            ["git", "-C", root, *args], capture_output=True, text=True, timeout=20
        )
    except Exception:  # pragma: no cover - git absent or wedged
        return []
    if p.returncode != 0:
        return []
    return [ln for ln in p.stdout.splitlines() if ln.strip()]


def _repos(sess: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Repo roots the sessions worked in, and every non-merge commit in each.

    Deduplicated by SHA: the same project cloned into two directories would otherwise count
    each of its commits twice. Worktrees under ``<repo>/.claude/worktrees/`` are deleted
    after use, so their paths no longer resolve — prefix matching maps them onto the parent
    repo, where their commits actually landed.
    """
    roots: List[str] = []
    for s in sess:
        for cwd in s.get("cwds") or []:
            if any(cwd == r or cwd.startswith(r + os.sep) for r in roots):
                continue
            top = _git(cwd, "rev-parse", "--show-toplevel")
            if top and top[0] not in roots:
                roots.append(top[0])

    commits: Dict[str, float] = {}
    shas: Dict[str, set] = {}
    for root in roots:
        owned = set()
        for line in _git(root, "log", "--all", "--no-merges", "--pretty=%H %cI"):
            sha, _, stamp = line.partition(" ")
            at = _epoch(stamp)
            if at:
                commits[sha] = at
                owned.add(sha)
        shas[root] = owned
    return {"roots": roots, "commits": commits, "shas": shas}


_git_root_cache: Dict[str, Tuple[float, Dict[str, float]]] = {}


def _repos_cached(sess: List[Dict[str, Any]]) -> Dict[str, Any]:
    now = time.time()
    roots: List[str] = []
    seen_cwds = set()
    for s in sess:
        for cwd in s.get("cwds") or []:
            if cwd not in seen_cwds:
                seen_cwds.add(cwd)
                if not any(cwd == r or cwd.startswith(r + os.sep) for r in roots):
                    top = _git(cwd, "rev-parse", "--show-toplevel")
                    if top and top[0] not in roots:
                        roots.append(top[0])

    commits: Dict[str, float] = {}
    shas: Dict[str, set] = {}
    for root in roots:
        cached = _git_root_cache.get(root)
        if cached and (now - cached[0] < _GIT_TTL):
            root_commits = cached[1]
        else:
            root_commits = {}
            for line in _git(root, "log", "--all", "--no-merges", "--pretty=%H %cI"):
                sha, _, stamp = line.partition(" ")
                at = _epoch(stamp)
                if at:
                    root_commits[sha] = at
            _git_root_cache[root] = (now, root_commits)
        commits.update(root_commits)
        shas[root] = set(root_commits.keys())

    return {"roots": roots, "commits": commits, "shas": shas}


def _repo_of(cwds: List[str], roots: List[str]) -> Optional[str]:
    best = None
    for cwd in cwds:
        for root in roots:
            if (cwd == root or cwd.startswith(root + os.sep)) and (
                best is None or len(root) > len(best)
            ):
                best = root
    return best


# Position-in-session bands for the cost ramp. Coarse and widening, because the thing being
# measured is a slope over hundreds of requests, not a per-request series — narrow bands late
# in a session would be read as noise by anyone whose longest session is 300 requests.
_RAMP_BANDS = ((1, 50), (51, 100), (101, 200), (201, 400), (401, 1 << 30))


def _ramp_bucket(pos: int) -> Optional[int]:
    for i, (lo, hi) in enumerate(_RAMP_BANDS):
        if lo <= pos <= hi:
            return i
    return None


def _ramp_label(lo: int, hi: int) -> str:
    return f"{lo}-{hi}" if hi < (1 << 30) else f"{lo}+"


def fleet(sess: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """The eleven fleet metrics behind docs/22 §0, for whatever range is in scope.

    Same definitions as ``scripts/session_metrics.py`` — one turn is one API request, and a
    "session" is a developer session. A dashboard that counted differently from the published
    analysis would be worse than no dashboard.
    """
    if not sess:
        return None

    tokens = {"fresh": 0, "cache_read": 0, "cache_write": 0, "output": 0}
    contexts: List[float] = []
    handbacks = interrupted = requests = 0
    by_model: Dict[str, Dict[str, float]] = {}
    daily: Dict[str, Dict[str, float]] = {}
    groups: List[Dict[str, Any]] = []
    cost_total = 0.0
    ramp = [{"requests": 0, "cost": 0.0, "context": 0} for _ in range(len(_RAMP_BANDS))]

    for members in developer_sessions(sess):
        g: Dict[str, Any] = {
            "requests": 0,
            "tokens": 0,
            "output": 0,
            "cost": 0.0,
            "commits": 0,
            "start": None,
            "end": None,
            "cwds": [],
        }
        for i in members:
            g["cwds"].extend(sess[i].get("cwds") or [])
            for pos, t in enumerate(sess[i]["turns"], start=1):
                fresh, cr = t["input_tokens"], t["cache_read_input_tokens"]
                cw, out = t["cache_creation_input_tokens"], t["output_tokens"]
                ctx = fresh + cr + cw
                requests += 1
                tokens["fresh"] += fresh
                tokens["cache_read"] += cr
                tokens["cache_write"] += cw
                tokens["output"] += out
                contexts.append(ctx)
                if t.get("stop_reason") == "end_turn":
                    handbacks += 1
                elif not t.get("stop_reason"):
                    interrupted += 1

                cost = 0.0
                r = _rates(t["model"])
                if r:
                    m = 1e6
                    w5 = t["ephemeral_5m_input_tokens"]
                    w1 = t["ephemeral_1h_input_tokens"]
                    write = (
                        w5 / m * r.cache_write_per_mtok("5m")
                        + w1 / m * r.cache_write_per_mtok("1h")
                        if (w5 or w1)
                        else cw / m * r.cache_write_per_mtok("5m")
                    )
                    cost = (
                        fresh / m * r.input_per_mtok
                        + cr / m * r.cache_read_per_mtok
                        + out / m * r.output_per_mtok
                        + write
                    )
                cost_total += cost

                bm = by_model.setdefault(
                    t["model"],
                    {
                        "prompt_tokens": 0,
                        "output_tokens": 0,
                        "requests": 0,
                        "cost": 0.0,
                    },
                )
                bm["prompt_tokens"] += ctx
                bm["output_tokens"] += out
                bm["requests"] += 1
                bm["cost"] += cost

                g["requests"] += 1
                g["tokens"] += ctx
                g["output"] += out
                g["cost"] += cost

                # Position is counted within the TRANSCRIPT, not the developer session: the
                # ramp is a property of one context window, and a subagent gets a fresh one.
                # Charging a subagent's first request the position its parent had reached
                # would invent a ramp out of the delegation pattern.
                rb = _ramp_bucket(pos)
                if rb is not None:
                    rr = ramp[rb]
                    rr["requests"] += 1
                    rr["cost"] += cost
                    rr["context"] += ctx

                at = _epoch(t.get("ts"))
                if at:
                    day = time.strftime("%Y-%m-%d", time.gmtime(at))
                    d = daily.setdefault(
                        day, {"tokens": 0, "cost": 0.0, "requests": 0, "commits": 0}
                    )
                    d["tokens"] += ctx + out
                    d["cost"] += cost
                    d["requests"] += 1
                    g["start"] = min(g["start"] or at, at)
                    g["end"] = max(g["end"] or at, at)
        groups.append(g)

    commits = _commits(groups, daily)

    # Every calendar day in the window, including idle ones: dropping them turns a four-day
    # gap into a single day-over-day step and makes both the trend and the chart lie.
    days = sorted(daily)
    series: List[Dict[str, Any]] = []
    if days:
        cur = time.strptime(days[0], "%Y-%m-%d")
        end = time.strptime(days[-1], "%Y-%m-%d")
        t0, t1 = time.mktime(cur), time.mktime(end)
        while t0 <= t1:
            day = time.strftime("%Y-%m-%d", time.localtime(t0))
            d = daily.get(day) or {
                "tokens": 0,
                "cost": 0.0,
                "requests": 0,
                "commits": 0,
            }
            series.append(
                {
                    "day": day,
                    "tokens": int(d["tokens"]),
                    "cost": d["cost"],
                    "requests": int(d["requests"]),
                    "commits": int(d["commits"]),
                }
            )
            t0 += 86400

    deltas = [
        series[i]["tokens"] / series[i - 1]["tokens"] - 1
        for i in range(1, len(series))
        if series[i - 1]["tokens"]
    ]

    weeks: List[Dict[str, Any]] = []
    for i in range(0, len(series), 7):
        chunk = series[i : i + 7]
        prev = weeks[-1]["tokens"] if weeks else None
        tok = sum(x["tokens"] for x in chunk)
        weeks.append(
            {
                "week": len(weeks),
                "days": len(chunk),
                "active_days": sum(1 for x in chunk if x["tokens"]),
                "tokens": tok,
                "cost": sum(x["cost"] for x in chunk),
                "commits": sum(x["commits"] for x in chunk),
                "wow": (tok / prev - 1) if prev else None,
            }
        )

    prompt = tokens["fresh"] + tokens["cache_read"] + tokens["cache_write"]
    per_session = [g["requests"] for g in groups]
    costs = [g["cost"] for g in groups]
    tin = float(prompt) or 1.0

    return {
        "sessions": len(groups),
        "transcripts": len(sess),
        "requests": requests,
        "tokens": {
            "fresh": tokens["fresh"],
            "cache_read": tokens["cache_read"],
            "cache_write": tokens["cache_write"],
            "prompt": prompt,
            "output": tokens["output"],
            "total": prompt + tokens["output"],
        },
        "cost": cost_total,
        "handbacks": handbacks,
        "interrupted": interrupted,
        "requests_per_handback": (requests / handbacks) if handbacks else 0.0,
        "cost_per_handback": (cost_total / handbacks) if handbacks else 0.0,
        "context": {
            "p25": _pct(contexts, 0.25),
            "p50": _pct(contexts, 0.50),
            "p99": _pct(contexts, 0.99),
            "max": int(max(contexts)) if contexts else 0,
        },
        "requests_per_session": {
            "p25": _pct(per_session, 0.25),
            "p50": _pct(per_session, 0.50),
            "p99": _pct(per_session, 0.99),
            "max": max(per_session) if per_session else 0,
        },
        "cost_per_session": {
            "mean": (cost_total / len(groups)) if groups else 0.0,
            "p50": _pct(costs, 0.50),
            "p99": _pct(costs, 0.99),
            "max": max(costs) if costs else 0.0,
        },
        # What a request costs as a function of how far into its context window it is. This
        # is the only place the page shows cost as a slope rather than a total, and it is
        # what makes session length legible as a lever: the same work priced at request 400
        # costs several times what it costs at request 10, because the prefix it drags is
        # several times longer. Bands with no requests are dropped, so a machine whose
        # sessions never reach 200 does not render three empty rows implying it should.
        "ramp": [
            {
                "band": _ramp_label(lo, hi),
                "requests": r["requests"],
                "cost": r["cost"],
                "cost_per_request": r["cost"] / r["requests"],
                "mean_context": r["context"] / r["requests"],
                "cost_share": (r["cost"] / cost_total) if cost_total else 0.0,
            }
            for (lo, hi), r in zip(_RAMP_BANDS, ramp)
            if r["requests"]
        ],
        "trend": {
            "dod_median": _pct(deltas, 0.50),
            "dod_p25": _pct(deltas, 0.25),
            "dod_p75": _pct(deltas, 0.75),
            "weeks": weeks,
        },
        "by_model": [
            {
                "model": name,
                "prompt_tokens": int(v["prompt_tokens"]),
                "output_tokens": int(v["output_tokens"]),
                "requests": int(v["requests"]),
                "cost": v["cost"],
                "token_share": v["prompt_tokens"] / tin,
                "cost_share": (v["cost"] / cost_total) if cost_total else 0.0,
            }
            for name, v in sorted(
                by_model.items(), key=lambda kv: -kv[1]["prompt_tokens"]
            )
        ],
        "commits": commits,
        "daily": series,
    }


def rate_card(f: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """The published rates every dollar on this page was computed from, with their source.

    The dashboard states costs to the cent; without the rate card and a link to the vendor
    page behind it, a reader has to take those on faith. Only models actually used in scope
    are listed — a full price list would be noise.

    Cache-write rates are **derived**, not quoted: the catalog stores one input price per
    model and the write rate is a fixed multiple of it set by the requested TTL. That is why
    the multipliers appear here rather than a second set of stored numbers that could drift.
    """
    from ace.gateway.pricing import (
        CACHE_WRITE_MULTIPLIER_1H,
        CACHE_WRITE_MULTIPLIER_5M,
    )

    rows: List[Dict[str, Any]] = []
    source = ""
    as_of = ""
    for entry in (f or {}).get("by_model") or []:
        r = _rates(entry["model"])
        if not r:
            # An unpriced model is shown as unpriced. Omitting it would make the rate card
            # imply every request on the page was priced.
            rows.append({"model": entry["model"], "priced": False})
            continue
        source = source or r.source
        as_of = as_of or r.as_of
        rows.append(
            {
                "model": entry["model"],
                "priced": True,
                "input": r.input_per_mtok,
                "output": r.output_per_mtok,
                "cache_read": r.cache_read_per_mtok,
                "cache_write_5m": r.cache_write_per_mtok("5m"),
                "cache_write_1h": r.cache_write_per_mtok("1h"),
                "cache_read_ratio": (
                    r.cache_read_per_mtok / r.input_per_mtok
                    if r.input_per_mtok
                    else 0.0
                ),
                "source": r.source,
                "as_of": r.as_of,
            }
        )
    return {
        "rows": rows,
        "source": source,
        "as_of": as_of,
        "write_multiplier_5m": CACHE_WRITE_MULTIPLIER_5M,
        "write_multiplier_1h": CACHE_WRITE_MULTIPLIER_1H,
        "unit": "USD per 1M tokens",
    }


def _commits(
    groups: List[Dict[str, Any]], daily: Dict[str, Dict[str, float]]
) -> Dict[str, Any]:
    """Commits produced inside each developer session's window, and the tokens they cost.

    Attribution is by time window, not causation: a commit landing while a session was open,
    in a repo that session was working in. Concurrent sessions on one history are broken by
    most-recent start, so no commit is counted twice.
    """
    out = {
        "available": False,
        "total": 0,
        "per_session_mean": 0.0,
        "per_session_p50": 0.0,
        "max": 0,
        "sessions_with_commits": 0,
        "tokens_per_commit": 0.0,
        "output_tokens_per_commit": 0.0,
        "cost_per_commit": 0.0,
        "in_window": 0,
        "attributed": 0,
    }
    repos = _repos_cached(groups)
    if not repos["roots"] or not repos["commits"]:
        return out

    windows = []
    for g in groups:
        root = _repo_of(g.get("cwds") or [], repos["roots"])
        if root and g["start"]:
            windows.append((root, g["start"], g["end"], g))
    if not windows:
        return out

    lo = min(w[1] for w in windows)
    hi = max(w[2] for w in windows)
    in_span = attributed = 0
    for sha, at in repos["commits"].items():
        if not (lo <= at <= hi):
            continue
        in_span += 1
        day = time.strftime("%Y-%m-%d", time.gmtime(at))
        if day in daily:
            daily[day]["commits"] += 1
        open_now = [
            w for w in windows if sha in repos["shas"][w[0]] and w[1] <= at <= w[2]
        ]
        if not open_now:
            continue
        max(open_now, key=lambda w: w[1])[3]["commits"] += 1
        attributed += 1

    per = [g["commits"] for g in groups]
    committing = [g for g in groups if g["commits"]]
    made = sum(g["commits"] for g in committing)
    out.update(
        {
            "available": True,
            "total": sum(per),
            "per_session_mean": (sum(per) / len(per)) if per else 0.0,
            "per_session_p50": _pct(per, 0.50),
            "max": max(per) if per else 0,
            "sessions_with_commits": len(committing),
            "tokens_per_commit": (
                sum(g["tokens"] + g["output"] for g in committing) / made
                if made
                else 0.0
            ),
            "output_tokens_per_commit": (
                sum(g["output"] for g in committing) / made if made else 0.0
            ),
            "cost_per_commit": (
                sum(g["cost"] for g in committing) / made if made else 0.0
            ),
            "in_window": in_span,
            "attributed": attributed,
        }
    )
    return out


def totals(sess: List[Dict[str, Any]]) -> Dict[str, Any]:
    agg: Dict[str, Any] = {
        "turns": 0,
        "sessions": len(sess),
        "fresh_tokens": 0,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "output_tokens": 0,
        "cost_usd": 0.0,
        "cache_saved_usd": 0.0,
        "peak_context": 0,
        "by_model": {},
        "first_ts": None,
        "last_ts": None,
    }
    for s in sess:
        for t in s["turns"]:
            agg["turns"] += 1
            fresh, cr = t["input_tokens"], t["cache_read_input_tokens"]
            cw, o = t["cache_creation_input_tokens"], t["output_tokens"]
            agg["fresh_tokens"] += fresh
            agg["cache_read_tokens"] += cr
            agg["cache_write_tokens"] += cw
            agg["output_tokens"] += o
            agg["peak_context"] = max(agg["peak_context"], fresh + cr + cw)
            e = _epoch(t.get("ts"))
            if e:
                agg["first_ts"] = min(agg["first_ts"] or e, e)
                agg["last_ts"] = max(agg["last_ts"] or e, e)
            r = _rates(t["model"])
            if not r:
                continue
            m = 1e6
            w5, w1 = t["ephemeral_5m_input_tokens"], t["ephemeral_1h_input_tokens"]
            write = (
                w5 / m * r.cache_write_per_mtok("5m")
                + w1 / m * r.cache_write_per_mtok("1h")
                if (w5 or w1)
                else cw / m * r.cache_write_per_mtok("5m")
            )
            cost = (
                fresh / m * r.input_per_mtok
                + cr / m * r.cache_read_per_mtok
                + o / m * r.output_per_mtok
                + write
            )
            agg["cost_usd"] += cost
            agg["cache_saved_usd"] += (
                cr / m * (r.input_per_mtok - r.cache_read_per_mtok)
            )
            bm = agg["by_model"].setdefault(t["model"], {"turns": 0, "cost_usd": 0.0})
            bm["turns"] += 1
            bm["cost_usd"] += cost
    agg["prompt_tokens"] = (
        agg["fresh_tokens"] + agg["cache_read_tokens"] + agg["cache_write_tokens"]
    )
    agg["cache_share"] = (
        agg["cache_read_tokens"] / agg["prompt_tokens"] if agg["prompt_tokens"] else 0.0
    )
    agg["available"] = agg["turns"] > 0
    return agg


# ------------------------------------------------- code quality & reliability metrics


def _calc_quality_block(sess: List[Dict[str, Any]]) -> Dict[str, Any]:
    total_sessions = len(sess)
    if not total_sessions:
        return {
            "available": False,
            "quality_score": 100,
            "grade": "A",
            "task_completion_rate": 1.0,
            "task_completion_rate_pct": 100.0,
            "verification_rate": 1.0,
            "verification_rate_pct": 100.0,
            "first_pass_success_rate": 1.0,
            "first_pass_success_rate_pct": 100.0,
            "tool_error_rate": 0.0,
            "tool_error_rate_pct": 0.0,
            "total_edits": 0,
            "total_tests": 0,
            "total_tool_calls": 0,
            "thrashed_files_count": 0,
            "thrashed_files_list": [],
            "rework_thrash_rate": 0.0,
            "rework_thrash_rate_pct": 0.0,
            "edit_stability": 1.0,
            "edit_stability_pct": 100.0,
            "redundant_reads_count": 0,
            "avg_error_recovery_turns": 1.0,
            "test_to_code_ratio": 1.0,
            "sessions_with_edits": 0,
            "sessions_with_tests": 0,
            "clean_completed_sessions": 0,
        }

    sessions_with_edits = 0
    sessions_with_tests = 0
    clean_completed_sessions = 0
    total_edits = 0
    total_tests = 0
    total_tool_calls = 0
    failed_tool_calls = 0
    redundant_reads_count = 0
    test_edits_count = 0
    src_edits_count = 0

    all_thrashed_files = set()
    recovery_turns_list = []

    for s in sess:
        turns = s.get("turns") or []
        session_has_edit = False
        session_has_test = False
        session_file_edits: Dict[str, int] = {}
        last_view_sig: Optional[str] = None
        pending_error_turn: Optional[int] = None
        last_turn_had_error = False

        for turn_idx, t in enumerate(turns):
            turn_has_error = False
            for c in t.get("calls") or []:
                total_tool_calls += 1
                is_err = bool(c.get("is_error"))
                if is_err:
                    failed_tool_calls += 1
                    turn_has_error = True

                if c.get("is_test_run"):
                    session_has_test = True
                    total_tests += 1

                if c.get("is_edit"):
                    session_has_edit = True
                    total_edits += 1
                    raw_t = c.get("raw_target") or c.get("target") or "unknown"
                    is_artifact = (
                        "/.gemini/antigravity/brain/" in raw_t
                        or "/.system_generated/" in raw_t
                        or raw_t.endswith("walkthrough.md")
                        or raw_t.endswith("implementation_plan.md")
                    )
                    if not is_artifact:
                        session_file_edits[raw_t] = session_file_edits.get(raw_t, 0) + 1
                    if c.get("is_test_file"):
                        test_edits_count += 1
                    elif c.get("is_src_file"):
                        src_edits_count += 1
                    # A file edit invalidates previous view cache
                    last_view_sig = None

                if c.get("is_view"):
                    view_sig = (
                        c.get("sig")
                        or c.get("digest")
                        or c.get("raw_target")
                        or c.get("target")
                    )
                    if view_sig and view_sig == last_view_sig:
                        redundant_reads_count += 1
                    last_view_sig = view_sig

            if turn_has_error:
                if pending_error_turn is None:
                    pending_error_turn = turn_idx
            elif pending_error_turn is not None:
                recovery_turns_list.append(max(1, turn_idx - pending_error_turn))
                pending_error_turn = None

        if turns and any(c.get("is_error") for c in turns[-1].get("calls") or []):
            last_turn_had_error = True

        if session_has_edit:
            sessions_with_edits += 1
            if session_has_test and not last_turn_had_error:
                clean_completed_sessions += 1
        else:
            if not last_turn_had_error:
                clean_completed_sessions += 1

        if session_has_test:
            sessions_with_tests += 1

        for fpath, count in session_file_edits.items():
            if count >= 3:
                all_thrashed_files.add(fpath)

    thrashed_files_count = len(all_thrashed_files)
    verification_rate = (
        (sessions_with_tests / sessions_with_edits)
        if sessions_with_edits > 0
        else (1.0 if not total_edits else 0.0)
    )

    first_pass_success_rate = (
        ((total_tool_calls - failed_tool_calls) / total_tool_calls)
        if total_tool_calls > 0
        else 1.0
    )

    tool_error_rate = (
        (failed_tool_calls / total_tool_calls)
        if total_tool_calls > 0
        else 0.0
    )

    task_completion_rate = (
        (clean_completed_sessions / total_sessions)
        if total_sessions > 0
        else 1.0
    )

    rework_thrash_rate = (
        (thrashed_files_count / max(1, len(all_thrashed_files) + total_edits))
        if total_edits > 0
        else 0.0
    )

    thrash_ratio = (thrashed_files_count / max(1, sessions_with_edits)) if sessions_with_edits > 0 else 0.0
    edit_stability = max(0.0, 1.0 - (thrash_ratio * 1.0))

    test_to_code_ratio = (
        (test_edits_count / src_edits_count)
        if src_edits_count > 0
        else (1.0 if test_edits_count > 0 else 0.5)
    )

    avg_error_recovery_turns = (
        (sum(recovery_turns_list) / len(recovery_turns_list))
        if recovery_turns_list
        else 1.0
    )

    # Balanced 0-100 score:
    # 35% Verified Task Completion, 30% Verification Diligence, 20% First-Pass Tool Success, 15% Edit Stability (Thrash-Free)
    raw_score = (
        0.35 * task_completion_rate
        + 0.30 * verification_rate
        + 0.20 * first_pass_success_rate
        + 0.15 * edit_stability
    ) * 100.0

    quality_score = max(0, min(100, int(round(raw_score))))
    if quality_score >= 90:
        grade = "A"
    elif quality_score >= 80:
        grade = "B"
    elif quality_score >= 70:
        grade = "C"
    elif quality_score >= 60:
        grade = "D"
    else:
        grade = "F"

    return {
        "available": True,
        "quality_score": quality_score,
        "grade": grade,
        "task_completion_rate": round(task_completion_rate, 4),
        "task_completion_rate_pct": round(task_completion_rate * 100.0, 1),
        "verification_rate": round(verification_rate, 4),
        "verification_rate_pct": round(verification_rate * 100.0, 1),
        "first_pass_success_rate": round(first_pass_success_rate, 4),
        "first_pass_success_rate_pct": round(first_pass_success_rate * 100.0, 1),
        "tool_error_rate": round(tool_error_rate, 4),
        "tool_error_rate_pct": round(tool_error_rate * 100.0, 1),
        "total_edits": total_edits,
        "total_tests": total_tests,
        "total_tool_calls": total_tool_calls,
        "thrashed_files_count": thrashed_files_count,
        "thrashed_files_list": sorted(list(all_thrashed_files))[:10],
        "rework_thrash_rate": round(rework_thrash_rate, 4),
        "rework_thrash_rate_pct": round(rework_thrash_rate * 100.0, 1),
        "edit_stability": round(edit_stability, 4),
        "edit_stability_pct": round(edit_stability * 100.0, 1),
        "redundant_reads_count": redundant_reads_count,
        "avg_error_recovery_turns": round(avg_error_recovery_turns, 1),
        "test_to_code_ratio": round(test_to_code_ratio, 2),
        "sessions_with_edits": sessions_with_edits,
        "sessions_with_tests": sessions_with_tests,
        "clean_completed_sessions": clean_completed_sessions,
    }


def quality_metrics(sess: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Calculates unified code quality, verification hygiene, and reliability metrics.

    Includes top-line metrics along with breakdowns:
    - by_agent: Quality scores partitioned per agent engine (Claude Code, Antigravity, Codex).
    - by_model: Quality scores partitioned per LLM model.
    """
    overall = _calc_quality_block(sess)
    if not sess:
        overall["by_agent"] = {}
        overall["by_model"] = []
        return overall

    # Group by agent
    by_agent: Dict[str, Any] = {}
    agent_groups: Dict[str, List[Dict[str, Any]]] = {}
    for s in sess:
        ak = s.get("agent_type") or AGENT_CLAUDE
        agent_groups.setdefault(ak, []).append(s)

    for ak, a_sess in agent_groups.items():
        block = _calc_quality_block(a_sess)
        by_agent[ak] = {
            "agent": ak,
            "label": AGENTS.get(ak, ak.capitalize()),
            "sessions": len(a_sess),
            **block,
        }

    # Group by model
    model_sessions: Dict[str, List[Dict[str, Any]]] = {}
    for s in sess:
        models_in_s = set(t.get("model") for t in s.get("turns", []) if t.get("model"))
        for m in models_in_s:
            projected_turns = [t for t in s.get("turns", []) if t.get("model") == m]
            if projected_turns:
                model_sessions.setdefault(m, []).append(
                    {
                        "session": s.get("session"),
                        "agent_type": s.get("agent_type"),
                        "turns": projected_turns,
                        "events": s.get("events", []),
                    }
                )

    by_model: List[Dict[str, Any]] = []
    for m_name, m_sess in sorted(model_sessions.items(), key=lambda kv: -len(kv[1])):
        block = _calc_quality_block(m_sess)
        by_model.append(
            {
                "model": m_name,
                "sessions": len(m_sess),
                **block,
            }
        )

    overall["by_agent"] = by_agent
    overall["by_model"] = by_model
    return overall


# ------------------------------------------------- time budget (docs/analysis_docs §1 and §2)

# Declared order is presentation order for ties; the payload sorts by size.
_PHASES = (
    "idle",
    "model thinking after a tool",
    "tool execution + approval",
    "human composing a prompt",
    "model generating",
    "model first response",
    "other",
)


def _timelines(sess: List[Dict[str, Any]]):
    """Each session's event list, oldest first. Sessions without events are skipped."""
    for s in sess:
        ev = s.get("events") or []
        if ev:
            yield ev


def time_budget(sess: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Where a session's *elapsed* time went — the denominator for any time-saving claim.

    This exists before any lever that claims to save time, because the measured shape says a
    lever aimed at the active loop is competing for a tenth of the clock:
    ``docs/analysis_docs/2026-07-29-sidecar-approval-and-checkpoint-analysis.md`` §1 found **89.3% of session
    wall clock is idle**. A saving quoted against total session time and a saving quoted
    against active time differ by ~9x, and only one of them is honest.

    Gaps are attributed by what sits on either side of them, which is all a transcript can
    support. One bucket is deliberately **not** split: ``tool execution + approval``. Nothing
    in a transcript separates the time a tool spent *running* from the time it spent waiting
    to be allowed to run, so splitting it would credit ``pytest`` to an approval prompt.
    """
    phases: Dict[str, float] = {k: 0.0 for k in _PHASES}
    spans: List[float] = []
    accounted = 0.0
    n = 0
    for ev in _timelines(sess):
        n += 1
        spans.append(ev[-1][3] - ev[0][0])
        for (t0, k0, _tools, e0), (t1, k1, _t, _e) in zip(ev, ev[1:]):
            # An assistant turn occupies real time between its first and last block, and that
            # time is the model working — never a wait. Counted separately, so the gap that
            # follows measures only what happened after the response was complete.
            if k0 == "assistant" and e0 > t0:
                phases["model generating"] += e0 - t0
                accounted += e0 - t0
            gap = t1 - e0
            if gap <= 0:
                continue
            accounted += gap
            if gap > IDLE_THRESHOLD_S:
                phases["idle"] += gap
            elif k0 == "assistant" and k1 == "tool_result":
                phases["tool execution + approval"] += gap
            elif k0 == "assistant" and k1 == "assistant":
                phases["model generating"] += gap
            elif k0 == "tool_result" and k1 == "assistant":
                phases["model thinking after a tool"] += gap
            elif k1 == "prompt":
                phases["human composing a prompt"] += gap
            elif k0 == "prompt":
                phases["model first response"] += gap
            else:
                phases["other"] += gap
    idle = phases["idle"]
    return {
        "available": accounted > 0,
        "sessions": n,
        "accounted_s": accounted,
        "idle_s": idle,
        "active_s": accounted - idle,
        "median_span_s": _pct(spans, 0.5),
        "idle_threshold_s": IDLE_THRESHOLD_S,
        "phases": sorted(
            (
                {
                    "name": k,
                    "seconds": phases[k],
                    "share": (phases[k] / accounted) if accounted else 0.0,
                }
                for k in _PHASES
                if phases[k] > 0
            ),
            key=lambda p: -p["seconds"],
        ),
    }


def parked(sess: List[Dict[str, Any]], now: Optional[float] = None) -> Dict[str, Any]:
    """Idle time an agent spent holding a tool call nobody let through.

    The measured shape (same document, §2): **227 stretches averaging 64 minutes, 26.2% of all
    idle time, 84% of it ``Bash``.** That is the "came back an hour later to find Claude
    waiting" failure mode, and it is two orders of magnitude larger than the per-prompt
    approval latency an auto-approver is usually pitched against — which on that corpus was
    already ~0, at 0.2% of wall clock, because ``acceptEdits``/``auto`` mode had collapsed it.

    **An upper bound, not a saving.** A transcript cannot distinguish a human who would have
    come back sooner to an agent that had made progress from one who left for unrelated
    reasons while a call happened to be pending. It is a ceiling; the realised figure only
    exists after actuation, as a before/after on this same number.

    ``by_tool`` counts one increment per tool named in a parked turn, so a turn holding three
    calls contributes one event and three tool increments — the same convention as
    ``scripts/analyze_approval_risk.py``, which is what makes the two comparable.
    """
    now = time.time() if now is None else now
    total = other = 0.0
    events = 0
    by_tool: Dict[str, int] = {}
    live: Optional[Dict[str, Any]] = None
    live_ts = -1.0
    for ev in _timelines(sess):
        for (_t0, k0, tools, e0), (t1, k1, _t, _e) in zip(ev, ev[1:]):
            gap = t1 - e0
            if gap <= IDLE_THRESHOLD_S:
                continue
            if k0 == "assistant" and tools and k1 == "tool_result":
                total += gap
                events += 1
                for name in tools:
                    by_tool[name] = by_tool.get(name, 0) + 1
            else:
                other += gap
        # The alarm. This transcript's last event is a turn still holding its tool calls, so
        # nothing has run since. Read from the transcript rather than the request path on
        # purpose: it fires whether or not the session's traffic goes through this sidecar.
        kind, tools, at = ev[-1][1], ev[-1][2], ev[-1][3]
        waited = now - at
        # Past the ceiling this is an abandoned transcript, not an agent waiting on anyone —
        # every crashed or closed session ends holding a pending call, and alarming about one
        # from a fortnight ago is how a useful alarm becomes noise that gets ignored.
        if (
            kind == "assistant"
            and tools
            and IDLE_THRESHOLD_S < waited <= PARKED_ALARM_MAX_S
            and at > live_ts
        ):
            live_ts = at
            live = {"since_s": waited, "tools": sorted({t for t in tools if t})}
    idle = total + other
    return {
        "available": events > 0 or live is not None,
        "total_s": total,
        "events": events,
        "mean_s": (total / events) if events else 0.0,
        "other_idle_s": other,
        "share_of_idle": (total / idle) if idle else 0.0,
        "by_tool": sorted(by_tool.items(), key=lambda kv: (-kv[1], kv[0])),
        "live": live,
        "reference": {
            "share_of_idle": PARKED_SHARE_OF_IDLE_MEASURED,
            "mean_s": PARKED_MEAN_S_MEASURED,
        },
    }


def scorecards(sess: List[Dict[str, Any]], billed: float = 0.0) -> Dict[str, Any]:
    """Both strategies, all tiers, scored on the developer's own sessions.

    ``standalone`` is the fourth answer the tiers cannot give: each volume lever alone,
    ranked by what it is worth, which is the ordering that decides what gets built.
    """
    from ace.gateway.pricing import rates_for
    from ace.sidecar.strategies import (
        ENTERPRISE_TIERS,
        USER_TIERS,
        accounting,
        score,
        standalone_levers,
    )

    acct = accounting(sess, rates_for)
    return {
        "enterprise": [score(sess, s, acct, rates_for) for s in ENTERPRISE_TIERS],
        "user": [score(sess, s, acct, rates_for) for s in USER_TIERS],
        "standalone": standalone_levers(sess, acct, rates_for, billed),
        "accounting": acct,
    }


def _rec(title, detail, evidence, risk, saving, unit):
    return {
        "title": title,
        "detail": detail,
        "evidence": evidence,
        "risk": risk,
        "saving": saving,
        "unit": unit,
    }


def recommendations(
    agg: Dict[str, Any],
    capture: Optional[Dict[str, Any]] = None,
    sess: Optional[List[Dict[str, Any]]] = None,
):
    """Rules fire off measured thresholds or not at all, each carrying evidence and risk."""
    recs = []
    prompt = agg.get("prompt_tokens") or 0
    peak = agg.get("peak_context") or 0

    # 1. Claude Optimization - New Skill Proposals
    if sess is not None:
        claude_sess = [
            s
            for s in sess
            if s.get("agent_type") == AGENT_CLAUDE or s.get("agent_type") is None
        ]
        pattern_counts: Dict[str, int] = {}
        for s in claude_sess:
            tools_used = set()
            for t in s.get("turns") or []:
                for c in t.get("calls") or []:
                    if c.get("name"):
                        tools_used.add(c["name"])

            if {"view_file", "grep_search"}.issubset(tools_used) or {
                "View",
                "Grep",
            }.issubset(tools_used):
                pattern_counts["codebase-auditor"] = (
                    pattern_counts.get("codebase-auditor", 0) + 1
                )
            if {"run_command"}.issubset(tools_used) or {"Bash"}.issubset(tools_used):
                pattern_counts["test-runner-and-fixer"] = (
                    pattern_counts.get("test-runner-and-fixer", 0) + 1
                )
            if {"replace_file_content", "multi_replace_file_content"}.issubset(
                tools_used
            ) or {"Edit"}.issubset(tools_used):
                pattern_counts["refactoring-suite"] = (
                    pattern_counts.get("refactoring-suite", 0) + 1
                )

        if not pattern_counts and claude_sess:
            pattern_counts["codebase-auditor"] = len(claude_sess)

        for skill_name, count in sorted(pattern_counts.items(), key=lambda kv: -kv[1]):
            skill_slug = skill_name.lower().replace(" ", "-")
            recs.append(
                _rec(
                    f"Claude Optimization — New Skill Proposal: {skill_slug}",
                    f"Sidecar analysis of {count} Claude session(s) identified repeated usage patterns "
                    f"({skill_name}). Distilling these instructions into a dedicated skill reduces prompt overhead "
                    f"and standardizes agent execution.\n\n"
                    f"Comprehensive Analysis & Setup Steps:\n"
                    f"1. Create skill directory: `.agents/skills/{skill_slug}/` (or `~/.gemini/config/skills/{skill_slug}/`).\n"
                    f"2. Create `SKILL.md` file with YAML frontmatter:\n"
                    f"   ---\n   name: {skill_slug}\n   description: Automated workflow for {skill_name}\n   ---\n"
                    f"3. Include reusable system prompt guidelines, workflow boundaries, and verification commands.\n"
                    f"4. Re-use in future sessions via `@skills/{skill_slug}` or automatic trigger matching.",
                    f"detected across {count} session(s)",
                    "NONE — modular skill distillation",
                    "15-30% prompt tokens",
                    "cost + tokens",
                )
            )

    if peak > CONTEXT_CAP_RECOMMENDED:
        recs.append(
            _rec(
                "Clear context earlier",
                f"Peak context reached {peak:,} tokens. Compaction fires late by default and "
                f"every turn after re-reads the whole thing. Clearing near "
                f"{CONTEXT_CAP_RECOMMENDED:,} captures most of the saving; past "
                f"{CONTEXT_PLATEAU:,} it stops paying, because the summary write costs more "
                f"than the read it avoids.",
                f"peak {peak:,} vs {CONTEXT_CAP_RECOMMENDED:,} target",
                "HIGH — the agent loses what it knew",
                "up to 40%",
                "cost + tokens",
            )
        )

    if capture and capture.get("tool_bytes_total"):
        unused, total = capture.get("tool_bytes_unused", 0), capture["tool_bytes_total"]
        if unused / total > 0.5:
            recs.append(
                _rec(
                    "Ship fewer tool definitions",
                    f"{capture.get('tools_defined')} tools defined, "
                    f"{capture.get('tools_used')} used. Definitions render FIRST, so unused ones "
                    f"sit at the head of the prefix and are re-read every turn. Anthropic "
                    f"supports deferring them (defer_loading + tool search).",
                    f"{unused:,} of {total:,} bytes unused ({unused/total*100:.0f}%)",
                    "NONE where the tool is genuinely unused",
                    f"~{unused/4/1000:.0f}k tok/turn",
                    "cost + tokens",
                )
            )

    share = agg.get("cache_share", 0.0)
    if prompt and share < CACHE_SHARE_HEALTHY:
        recs.append(
            _rec(
                "Prompt cache is underperforming",
                f"Only {share*100:.1f}% of prompt volume is served from cache; healthy agentic "
                f"coding runs ~98%. Something changes the prefix between turns — a moving system "
                f"prompt, a changing tool set, or turns appending more than 20 content blocks "
                f"(the cache lookback limit).",
                f"cache-read share {share*100:.1f}%",
                "NONE — this is a defect, not a trade-off",
                "restores up to 6.7x",
                "cost",
            )
        )

    bm = agg.get("by_model") or {}
    tot = sum(v["cost_usd"] for v in bm.values()) or 1.0
    for model, v in bm.items():
        if model in EXPENSIVE_MODELS and v["cost_usd"] / tot > 0.15:
            recs.append(
                _rec(
                    f"Reconsider {model} for routine work",
                    f"{model} is {v['turns']} turns but {v['cost_usd']/tot*100:.0f}% of spend. "
                    f"Cache-read rates span 5x across the tier and cache reads are ~63% of the "
                    f"bill, so which model holds a large context matters far more than which "
                    f"model writes a commit message.",
                    f"${v['cost_usd']:,.2f} of ${tot:,.2f}",
                    "MEDIUM — a real quality trade, unmeasured here",
                    "up to 14%",
                    "cost",
                )
            )

    # 4. Code Quality & Test Hygiene Recommendations
    if sess is not None:
        qm = quality_metrics(sess)
        if (
            qm.get("sessions_with_edits", 0) > 0
            and qm.get("verification_rate", 1.0) < 0.5
        ):
            recs.append(
                _rec(
                    "Low test verification hygiene in agent sessions",
                    f"Only {qm.get('verification_rate_pct', 0)}% of sessions with code edits ran automated test suites. "
                    f"Running test/lint passes before finishing turns reduces runtime bugs and catches regressions early.",
                    f"{qm.get('sessions_with_tests', 0)} of {qm.get('sessions_with_edits', 0)} editing sessions verified",
                    "LOW",
                    f"{round((1.0 - qm.get('verification_rate', 0.0)) * 100, 1)}% unverified",
                    "of editing sessions",
                )
            )
        if qm.get("thrashed_files_count", 0) >= 3:
            recs.append(
                _rec(
                    f"File edit thrashing detected on {qm.get('thrashed_files_count')} files",
                    "Agent modified the same files 3+ times in single sessions. Providing more explicit prompt instructions, "
                    "specifying test fixtures, or decomposing tasks into smaller subagents reduces edit churn.",
                    f"{qm.get('thrashed_files_count')} thrashed files across scope",
                    "MED",
                    f"{qm.get('rework_thrash_rate_pct')}% thrash",
                    "churn rate",
                )
            )

    if not recs:
        recs.append(
            _rec(
                "Nothing actionable in this range",
                "No measured threshold was crossed. Widen the date range, or keep using Claude "
                "Code through the sidecar and this fills in.",
                "no threshold crossed",
                "—",
                "—",
                "—",
            )
        )
    return recs


def _tilde(path: str) -> str:
    """Absolute path with $HOME collapsed to ~, for display only."""
    home = os.path.expanduser("~")
    if path == home or path.startswith(home + os.sep):
        return "~" + path[len(home) :]
    return path


def session_files(
    root: Optional[str] = None,
    antigravity_root: Optional[str] = None,
    codex_root: Optional[str] = None,
    limit: int = 14,
    agent: Optional[str] = None,
    all_sessions: Optional[List[Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    """The session files on disk, newest first, with a short content snapshot."""
    if (
        all_sessions is not None
        and root is None
        and antigravity_root is None
        and codex_root is None
    ):
        target_agent = agent if agent and agent != AGENT_ALL else None
        scoped = [
            s
            for s in all_sessions
            if target_agent is None or s.get("agent_type") == target_agent
        ]
        out_mem: List[Dict[str, Any]] = []
        for s in scoped[:limit]:
            cwds = s.get("cwds") or []
            proj = _tilde(str(cwds[0])) if cwds else ""
            turns = s.get("turns") or []
            last_turn = turns[-1] if turns else {}
            last_ts = (
                max((_epoch(t.get("ts")) or 0.0) for t in turns) if turns else 0.0
            )
            fpath = s.get("path") or ""
            fname = os.path.basename(fpath) if fpath else f"{s.get('session')}.jsonl"
            out_mem.append(
                {
                    "path": _tilde(fpath) if fpath else f"~/{s.get('session')}",
                    "project": proj,
                    "file": fname,
                    "bytes": s.get("bytes") or 0,
                    "mtime": s.get("mtime") or last_ts,
                    "kind": s.get("kind", "main"),
                    "agent_type": s.get("agent_type", AGENT_CLAUDE),
                    "turns": len(turns),
                    "model": last_turn.get("model", "") or s.get("model", ""),
                    "snippet": s.get("snippet", ""),
                }
            )
        return out_mem

    if root is None:
        root = TRANSCRIPT_ROOT
    if antigravity_root is None:
        antigravity_root = ANTIGRAVITY_ROOT
    if codex_root is None:
        codex_root = CODEX_ROOT

    out: List[Dict[str, Any]] = []
    paths: List[tuple] = []
    if (not agent or agent in (AGENT_ALL, AGENT_CLAUDE)) and os.path.isdir(root):
        for p in glob.glob(os.path.join(root, "**", "*.jsonl"), recursive=True):
            paths.append((p, AGENT_CLAUDE))
    if (not agent or agent in (AGENT_ALL, AGENT_ANTIGRAVITY)) and os.path.isdir(
        antigravity_root
    ):
        for dirpath, _, filenames in os.walk(antigravity_root):
            for fn in filenames:
                if fn in ("transcript.jsonl", "transcript_full.jsonl"):
                    paths.append((os.path.join(dirpath, fn), AGENT_ANTIGRAVITY))
    if (not agent or agent in (AGENT_ALL, AGENT_CODEX)) and os.path.isdir(
        codex_root
    ):
        for dirpath, _, filenames in os.walk(codex_root):
            for fn in filenames:
                if fn.endswith(".jsonl") or fn.endswith(".json"):
                    paths.append((os.path.join(dirpath, fn), AGENT_CODEX))

    paths.sort(key=lambda item: os.path.getmtime(item[0]), reverse=True)
    for path, atype in paths[:limit]:
        info: Dict[str, Any] = {
            "path": _tilde(path),
            "project": "",
            "file": os.path.basename(path),
            "bytes": os.path.getsize(path),
            "mtime": os.path.getmtime(path),
            "kind": "main",
            "agent_type": atype,
            "turns": 0,
            "model": "",
            "snippet": "",
        }
        try:
            records: List[Dict[str, Any]] = []
            if path.endswith(".jsonl"):
                with open(path, "r", encoding="utf-8", errors="replace") as fh:
                    for line in fh:
                        line = line.strip()
                        if line:
                            try:
                                records.append(json.loads(line))
                            except Exception:
                                pass
            else:
                with open(path, "r", encoding="utf-8", errors="replace") as fh:
                    data = json.load(fh)
                    if isinstance(data, list):
                        records = [d for d in data if isinstance(d, dict)]
                    elif isinstance(data, dict):
                        if "messages" in data and isinstance(
                            data["messages"], list
                        ):
                            records = [
                                d for d in data["messages"] if isinstance(d, dict)
                            ]
                            if "cwd" in data:
                                info["project"] = _tilde(str(data["cwd"]))
                        elif "turns" in data and isinstance(data["turns"], list):
                            records = [
                                d for d in data["turns"] if isinstance(d, dict)
                            ]
                        else:
                            records = [data]

            for rec in records:
                if atype == AGENT_CLAUDE:
                    msg = rec.get("message") or {}
                    if not info["project"] and rec.get("cwd"):
                        info["project"] = _tilde(rec["cwd"])
                    if rec.get("type") == "assistant" and (
                        msg.get("usage") or {}
                    ):
                        info["turns"] += 1
                        info["model"] = msg.get("model") or info["model"]
                    elif rec.get("type") == "user" and not info["snippet"]:
                        c = msg.get("content")
                        text = c if isinstance(c, str) else ""
                        if isinstance(c, list):
                            for b in c:
                                if (
                                    isinstance(b, dict)
                                    and b.get("type") == "text"
                                ):
                                    text = b.get("text") or ""
                                    break
                        text = " ".join(str(text).split())
                        if text and not text.startswith("<"):
                            info["snippet"] = text[:110]
                elif atype == AGENT_ANTIGRAVITY:
                    stype = str(rec.get("type", "")).upper()
                    if stype in ("PLANNER_RESPONSE", "MODEL_RESPONSE", "MODEL"):
                        info["turns"] += 1
                        info["model"] = rec.get("model") or "gemini-3.6-flash"
                    elif stype in ("USER_INPUT", "USER") and not info["snippet"]:
                        text = rec.get("content") or rec.get("text") or ""
                        text = " ".join(str(text).split())
                        if text and not text.startswith("<"):
                            info["snippet"] = text[:110]
                elif atype == AGENT_CODEX:
                    srole = str(rec.get("role", "")).lower()
                    stype = str(rec.get("type", "")).lower()
                    payload = (
                        rec.get("payload")
                        if isinstance(rec.get("payload"), dict)
                        else {}
                    )
                    ptype = str(payload.get("type", "")).lower() if payload else ""
                    prole = str(
                        payload.get("role") or ""
                    ).lower()

                    if not info["project"]:
                        if rec.get("cwd"):
                            info["project"] = _tilde(str(rec["cwd"]))
                        elif payload.get("cwd"):
                            info["project"] = _tilde(str(payload["cwd"]))

                    if stype == "session_meta":
                        m = (
                            (payload.get("base_instructions") or {})
                            .get("provenance", {})
                            .get("model")
                        )
                        if m:
                            info["model"] = str(m)
                    elif stype == "world_state":
                        m = (payload.get("state") or {}).get("model")
                        if m:
                            info["model"] = str(m)
                    elif stype == "event_msg" and ptype == "token_count":
                        info["turns"] += 1
                        if not info["model"]:
                            info["model"] = "gpt-5.6-terra"
                    elif srole == "assistant" or stype in (
                        "assistant",
                        "model",
                        "response",
                    ):
                        info["turns"] += 1
                        info["model"] = (
                            rec.get("model")
                            or (rec.get("message") or {}).get("model")
                            or info["model"]
                            or "gpt-5.3-codex"
                        )
                    elif (
                        srole == "user"
                        or stype in ("user", "user_input", "prompt")
                        or (stype == "response_item" and ptype == "message" and prole == "user")
                    ) and not info["snippet"]:
                        c = (
                            rec.get("content")
                            or rec.get("text")
                            or payload.get("content")
                            or ""
                        )
                        text = ""
                        if isinstance(c, list):
                            for b in c:
                                if isinstance(b, dict) and b.get("type") in ("input_text", "text"):
                                    txt_val = b.get("text") or ""
                                    if txt_val and not txt_val.strip().startswith("<"):
                                        text = txt_val
                                        break
                        elif isinstance(c, str):
                            text = c
                        text = " ".join(str(text).split())
                        if text and not text.startswith("<"):
                            info["snippet"] = text[:110]
        except Exception:
            pass

        # If snippet is still empty, check if Codex session_index has a thread_name
        if atype == AGENT_CODEX and not info["snippet"]:
            try:
                idx_file = os.path.join(codex_root, "session_index.jsonl")
                if not os.path.exists(idx_file):
                    idx_file = os.path.join(os.path.dirname(codex_root), "session_index.jsonl")
                if os.path.exists(idx_file):
                    with open(idx_file, "r", encoding="utf-8", errors="replace") as fh:
                        for l_idx in fh:
                            l_idx = l_idx.strip()
                            if l_idx:
                                d_idx = json.loads(l_idx)
                                sid_idx = d_idx.get("id") or ""
                                if sid_idx and sid_idx in info["file"]:
                                    if d_idx.get("thread_name"):
                                        info["snippet"] = d_idx["thread_name"][:110]
                                        break
            except Exception:
                pass

        out.append(info)
    return out


def span(
    range_key: str, window: Optional[float], agg: Dict[str, Any]
) -> Dict[str, Any]:
    """The dates a range actually covers."""
    first, last = agg.get("first_ts"), agg.get("last_ts")
    out: Dict[str, Any] = {
        "first_ts": first,
        "last_ts": last,
        "requested_start": (time.time() - window) if window and window > 0 else None,
        "partial": bool(
            window
            and window > 0
            and first
            and (first - (time.time() - window)) > 86400.0
        ),
    }
    out["days"] = ((last - first) / 86400.0) if (first and last) else 0.0
    return out


_build_cache: "OrderedDict[tuple, Dict[str, Any]]" = OrderedDict()
# One entry per (range, agent) pair the user can select, times a little slack for the
# fingerprint turning over mid-session. Bounded because entries keyed on an old transcript
# fingerprint are dead the moment a transcript is appended to, and an unbounded dict would
# hold every one of them — each a full dashboard payload — for the life of the process.
_BUILD_CACHE_MAX = 32


def _build_payload(
    all_sessions: List[Dict[str, Any]],
    capture: Optional[Dict[str, Any]],
    range_key: str,
    agent: str,
    store_path: Optional[str],
) -> Dict[str, Any]:
    """The part of the dashboard payload that is derived purely from transcripts on disk.

    Split out of :func:`build` so it can be cached. Everything here is a pure function of
    (transcripts, range, agent, capture) — no telemetry-store reads — which is what makes the
    result safe to hand back to a later request. The live counters are deliberately *not*
    computed here; see :func:`build`.
    """
    window = RANGES.get(range_key, RANGES[DEFAULT_RANGE])
    scoped = filter_range(all_sessions, window, agent=agent)
    agg = totals(scoped)
    _fleet = fleet(scoped) if agg["available"] else None
    ab = agent_breakdown(scoped)
    return {
        "range": range_key,
        "ranges": [(k, RANGE_LABELS[k]) for k in RANGES],
        "agent": agent,
        "agents": list(AGENTS.items()),
        "agent_breakdown": ab,
        "span": span(range_key, window, agg),
        "historical": agg,
        "quality": quality_metrics(scoped),
        "time": time_budget(scoped),
        "parked": parked(scoped),
        "fleet": _fleet,
        "rate_card": rate_card(_fleet),
        "scorecards": (
            scorecards(scoped, agg.get("cost_usd") or 0.0) if agg["available"] else None
        ),
        "files": session_files(agent=agent, all_sessions=all_sessions),
        "capture": capture or {},
        "recommendations": recommendations(agg, capture, sess=scoped),
        "local_skill_proposals": (
            __import__(
                "ace.sidecar.skill_miner", fromlist=["mine_local_skills"]
            ).mine_local_skills(scoped)
            if agg.get("available")
            else []
        ),
        "installed_skills": (
            __import__(
                "ace.sidecar.skill_miner", fromlist=["get_installed_skills"]
            ).get_installed_skills()
        ),
        "sources": {
            "transcripts": [TRANSCRIPT_ROOT, ANTIGRAVITY_ROOT, CODEX_ROOT],
            "telemetry_db": store_path,
            "external": None,
        },
    }


def build(
    store: Any = None,
    capture: Optional[Dict[str, Any]] = None,
    range_key: str = DEFAULT_RANGE,
    agent: str = AGENT_ALL,
) -> Dict[str, Any]:
    """Everything the dashboard needs, from local sources only.

    Switching the agent tab re-runs this for the same transcripts, and the derived half is
    expensive — mining skill proposals and building the fleet table dominate a ~3-4s rebuild
    — so it is memoised on a key that includes the transcript fingerprint from
    :func:`sessions`. Appending a turn changes that fingerprint and retires every entry
    derived from the old one, so a stale payload cannot outlive the data it came from.

    The live telemetry counters are re-read on every call instead of being cached with the
    rest: they change on each proxied turn rather than when a transcript is written, so
    caching them would freeze the one number on the page that is meant to move.
    """
    all_sessions = sessions()
    store_path = getattr(store, "path", None)
    fp = _capture_fingerprint(capture)
    current_fp_key = _cache.get("key")
    cache_key = (
        range_key,
        agent,
        current_fp_key,
        store_path,
        fp,
    )
    payload = _build_cache.get(cache_key)
    if payload is None:
        payload = _build_payload(all_sessions, capture, range_key, agent, store_path)
        _build_cache[cache_key] = payload
        while len(_build_cache) > _BUILD_CACHE_MAX:
            _build_cache.popitem(last=False)

    # Shallow copy: the caller (render, /api/stats) treats the payload as read-only, but the
    # live keys below are per-request and must not be written into the shared cached dict.
    out = dict(payload)
    out["live"] = store.summary() if store is not None else {"turns": 0}
    out["recent"] = store.recent(30) if store is not None else []
    return out


def _capture_fingerprint(capture: Optional[Dict[str, Any]]) -> str:
    """A stable digest of the capture summary, for use in the build cache key.

    The summary feeds ``recommendations`` and is echoed into the payload, so two requests
    with different capture state must not share an entry. It is a small JSON-shaped dict;
    hashing its canonical form keeps the key hashable and bounded regardless of its depth.
    """
    if not capture:
        return ""
    try:
        blob = json.dumps(capture, sort_keys=True, default=str)
    except (TypeError, ValueError):
        return repr(sorted(capture))
    return hashlib.sha1(blob.encode("utf-8", "replace")).hexdigest()


def format_prometheus_metrics(d: Dict[str, Any]) -> str:
    """Format local telemetry metrics into standard Prometheus text exposition format (v0.0.4)."""
    lines: List[str] = []
    h = d.get("historical") or {}
    ab = d.get("agent_breakdown") or {}
    tb = d.get("time") or {}
    pk = d.get("parked") or {}
    fl = d.get("fleet") or {}
    cp = d.get("capture") or {}
    skills = d.get("installed_skills") or []

    # Sessions
    lines.append("# HELP ace_sessions_total Total observed agent sessions.")
    lines.append("# TYPE ace_sessions_total counter")
    lines.append(f'ace_sessions_total{{agent="all"}} {h.get("sessions", 0)}')
    for agent_id, info in ab.items():
        lines.append(f'ace_sessions_total{{agent="{agent_id}"}} {info.get("sessions", 0)}')

    # Turns / Requests
    lines.append("# HELP ace_turns_total Total AI agent API turns / requests.")
    lines.append("# TYPE ace_turns_total counter")
    lines.append(f'ace_turns_total{{agent="all"}} {h.get("turns", 0)}')
    for agent_id, info in ab.items():
        lines.append(f'ace_turns_total{{agent="{agent_id}"}} {info.get("turns", 0)}')

    # Token counters
    lines.append("# HELP ace_tokens_input_fresh_total Fresh prompt input tokens.")
    lines.append("# TYPE ace_tokens_input_fresh_total counter")
    lines.append(f'ace_tokens_input_fresh_total {h.get("tokens_fresh", 0)}')

    lines.append("# HELP ace_tokens_cache_read_total Input tokens served from prompt cache.")
    lines.append("# TYPE ace_tokens_cache_read_total counter")
    lines.append(f'ace_tokens_cache_read_total {h.get("tokens_cache_read", 0)}')

    lines.append("# HELP ace_tokens_cache_write_total Tokens written to prompt cache.")
    lines.append("# TYPE ace_tokens_cache_write_total counter")
    lines.append(f'ace_tokens_cache_write_total {h.get("tokens_cache_write", 0)}')

    lines.append("# HELP ace_tokens_output_total Model generated completion tokens.")
    lines.append("# TYPE ace_tokens_output_total counter")
    lines.append(f'ace_tokens_output_total {h.get("tokens_output", 0)}')

    # USD List-Price Cost
    lines.append("# HELP ace_cost_usd_total List-price valuation in USD.")
    lines.append("# TYPE ace_cost_usd_total counter")
    lines.append(f'ace_cost_usd_total{{agent="all"}} {round(h.get("cost_usd", 0.0), 4)}')
    for agent_id, info in ab.items():
        lines.append(f'ace_cost_usd_total{{agent="{agent_id}"}} {round(info.get("cost_usd", 0.0), 4)}')

    # Gauges
    lines.append("# HELP ace_peak_context_tokens Peak context tokens observed in a turn.")
    lines.append("# TYPE ace_peak_context_tokens gauge")
    lines.append(f'ace_peak_context_tokens {h.get("peak_context", 0)}')

    lines.append("# HELP ace_cache_read_share Ratio of input tokens served from cache.")
    lines.append("# TYPE ace_cache_read_share gauge")
    lines.append(f'ace_cache_read_share {round(h.get("cache_share", 0.0), 4)}')

    # Session Time Breakdown
    lines.append("# HELP ace_session_time_seconds Cumulative session time breakdown in seconds.")
    lines.append("# TYPE ace_session_time_seconds counter")
    lines.append(f'ace_session_time_seconds{{state="wall_clock"}} {round(tb.get("accounted_s", 0.0), 2)}')
    lines.append(f'ace_session_time_seconds{{state="active"}} {round(tb.get("active_s", 0.0), 2)}')
    lines.append(f'ace_session_time_seconds{{state="idle"}} {round(tb.get("idle_s", 0.0), 2)}')
    lines.append(f'ace_session_time_seconds{{state="parked"}} {round(pk.get("total_s", 0.0), 2)}')

    # Model Breakdown
    by_model = fl.get("by_model") or []
    if isinstance(by_model, list) and by_model:
        lines.append("# HELP ace_model_requests_total Total API requests by model.")
        lines.append("# TYPE ace_model_requests_total counter")
        for item in by_model:
            model_id = item.get("model", "unknown")
            lines.append(f'ace_model_requests_total{{model="{model_id}"}} {item.get("requests", 0)}')

        lines.append("# HELP ace_model_prompt_tokens_total Total prompt tokens by model.")
        lines.append("# TYPE ace_model_prompt_tokens_total counter")
        for item in by_model:
            model_id = item.get("model", "unknown")
            lines.append(f'ace_model_prompt_tokens_total{{model="{model_id}"}} {item.get("prompt_tokens", 0)}')

        lines.append("# HELP ace_model_output_tokens_total Total output tokens by model.")
        lines.append("# TYPE ace_model_output_tokens_total counter")
        for item in by_model:
            model_id = item.get("model", "unknown")
            lines.append(f'ace_model_output_tokens_total{{model="{model_id}"}} {item.get("output_tokens", 0)}')

        lines.append("# HELP ace_model_cost_usd_total Total cost in USD by model.")
        lines.append("# TYPE ace_model_cost_usd_total counter")
        for item in by_model:
            model_id = item.get("model", "unknown")
            lines.append(f'ace_model_cost_usd_total{{model="{model_id}"}} {round(item.get("cost", 0.0), 4)}')
    elif isinstance(by_model, dict) and by_model:
        lines.append("# HELP ace_model_requests_total Total API requests by model.")
        lines.append("# TYPE ace_model_requests_total counter")
        for model_id, m_data in by_model.items():
            lines.append(f'ace_model_requests_total{{model="{model_id}"}} {m_data.get("requests", 0)}')

        lines.append("# HELP ace_model_prompt_tokens_total Total prompt tokens by model.")
        lines.append("# TYPE ace_model_prompt_tokens_total counter")
        for model_id, m_data in by_model.items():
            lines.append(f'ace_model_prompt_tokens_total{{model="{model_id}"}} {m_data.get("prompt_tokens", 0)}')

        lines.append("# HELP ace_model_output_tokens_total Total output tokens by model.")
        lines.append("# TYPE ace_model_output_tokens_total counter")
        for model_id, m_data in by_model.items():
            lines.append(f'ace_model_output_tokens_total{{model="{model_id}"}} {m_data.get("output_tokens", 0)}')

        lines.append("# HELP ace_model_cost_usd_total Total cost in USD by model.")
        lines.append("# TYPE ace_model_cost_usd_total counter")
        for model_id, m_data in by_model.items():
            lines.append(f'ace_model_cost_usd_total{{model="{model_id}"}} {round(m_data.get("cost", 0.0), 4)}')

    # Tool declaration bytes
    if cp:
        lines.append("# HELP ace_tool_bytes Tool declaration payload size in bytes.")
        lines.append("# TYPE ace_tool_bytes gauge")
        lines.append(f'ace_tool_bytes{{type="total"}} {cp.get("tool_bytes_total", 0)}')
        lines.append(f'ace_tool_bytes{{type="unused"}} {cp.get("tool_bytes_unused", 0)}')

    # Installed Skills count
    lines.append("# HELP ace_installed_skills_total Number of active workflow skills installed.")
    lines.append("# TYPE ace_installed_skills_total gauge")
    lines.append(f'ace_installed_skills_total {len(skills)}')

    # Code Quality & Reliability Metrics
    qm = d.get("quality") or {}
    lines.append("# HELP ace_quality_score Composite code quality and verification score (0-100).")
    lines.append("# TYPE ace_quality_score gauge")
    lines.append(f'ace_quality_score{{agent="all"}} {qm.get("quality_score", 100)}')
    for agent_id, q_info in (qm.get("by_agent") or {}).items():
        lines.append(f'ace_quality_score{{agent="{agent_id}"}} {q_info.get("quality_score", 100)}')
    for m_info in (qm.get("by_model") or []):
        m_name = m_info.get("model", "unknown")
        lines.append(f'ace_quality_score{{model="{m_name}"}} {m_info.get("quality_score", 100)}')

    lines.append("# HELP ace_quality_verification_rate Share of edited sessions that ran automated tests or linters.")
    lines.append("# TYPE ace_quality_verification_rate gauge")
    lines.append(f'ace_quality_verification_rate{{agent="all"}} {qm.get("verification_rate", 1.0)}')
    for agent_id, q_info in (qm.get("by_agent") or {}).items():
        lines.append(f'ace_quality_verification_rate{{agent="{agent_id}"}} {q_info.get("verification_rate", 1.0)}')

    lines.append("# HELP ace_quality_first_pass_success_rate Share of tool calls that succeeded on first pass.")
    lines.append("# TYPE ace_quality_first_pass_success_rate gauge")
    lines.append(f'ace_quality_first_pass_success_rate{{agent="all"}} {qm.get("first_pass_success_rate", 1.0)}')
    for agent_id, q_info in (qm.get("by_agent") or {}).items():
        lines.append(f'ace_quality_first_pass_success_rate{{agent="{agent_id}"}} {q_info.get("first_pass_success_rate", 1.0)}')

    lines.append("# HELP ace_quality_tool_error_rate Share of tool executions that returned errors.")
    lines.append("# TYPE ace_quality_tool_error_rate gauge")
    lines.append(f'ace_quality_tool_error_rate {qm.get("tool_error_rate", 0.0)}')

    lines.append("# HELP ace_quality_thrashed_files_total Number of files edited 3 or more times in a single session.")
    lines.append("# TYPE ace_quality_thrashed_files_total counter")
    lines.append(f'ace_quality_thrashed_files_total {qm.get("thrashed_files_count", 0)}')

    lines.append("# HELP ace_quality_redundant_reads_total Count of consecutive duplicate file reads.")
    lines.append("# TYPE ace_quality_redundant_reads_total counter")
    lines.append(f'ace_quality_redundant_reads_total {qm.get("redundant_reads_count", 0)}')

    lines.append("# HELP ace_quality_error_recovery_turns_avg Average turns to recover from an execution error.")
    lines.append("# TYPE ace_quality_error_recovery_turns_avg gauge")
    lines.append(f'ace_quality_error_recovery_turns_avg {qm.get("avg_error_recovery_turns", 1.0)}')

    lines.append("# HELP ace_quality_test_to_code_ratio Ratio of test file edits to source file edits.")
    lines.append("# TYPE ace_quality_test_to_code_ratio gauge")
    lines.append(f'ace_quality_test_to_code_ratio {qm.get("test_to_code_ratio", 1.0)}')

    return "\n".join(lines) + "\n"



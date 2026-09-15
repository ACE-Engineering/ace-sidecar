"""ace.cli — ``ace up``, the local sidecar launcher (P0-5).

Goal: a developer goes from nothing to Claude Code running through ACE in two commands and
under five minutes, with their Anthropic key never leaving the machine.

    ace up --key sk-ant-...        # or put it in ~/.ace/config.json, or ANTHROPIC_API_KEY
    export ANTHROPIC_BASE_URL=http://127.0.0.1:8787

Config resolution, highest first: CLI flag → ``~/.ace/config.json`` → environment → built-in
default. JSON rather than TOML deliberately — ``tomllib`` is available now that the floor is
3.12, but the config stays JSON: developers hand-edit it, and switching format would break
every ``~/.ace/config.json`` already on disk.

**Every** ``ace up`` option goes through that chain, keyed by its long flag name with dashes
as underscores, so a developer's habitual invocation collapses to a bare ``ace up``::

    {"no_key": true, "port": 8788, "log_level": "warning"}    # ~/.ace/config.json

Flags that resolve to a default rather than being read straight off ``args`` must declare
``default=None`` in the parser — argparse's implicit ``False`` for ``store_true`` is
indistinguishable from the user passing the flag, and would silently outrank the config file.
``ace up --help`` lists each option with its config key, env var, and default.

Binding
-------
Defaults to ``127.0.0.1``. Binding anywhere else requires ``--allow-remote`` **and** still
does not grant access: loopback-trust auth independently checks the peer address and refuses
proxied requests, so an off-loopback sidecar answers 403 rather than relaying. The flag exists
for tunnels and container port-forwards where the operator knows what they are doing, not as
a way to share a key.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from typing import Any, Dict, Optional

CONFIG_PATH = os.path.expanduser("~/.ace/config.json")
DEFAULT_PORT = 8787
DEFAULT_HOST = "127.0.0.1"
DEFAULT_DB = os.path.expanduser("~/.ace/telemetry.db")

log = logging.getLogger("ace.cli")


def load_config(path: str = CONFIG_PATH) -> Dict[str, Any]:
    """Read ``~/.ace/config.json``. A missing or unreadable file is not an error."""
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except Exception:
        # Never print the file's contents on a parse error — it holds a provider key.
        log.warning("[ace] could not parse %s; ignoring it", path)
        return {}


def resolve(
    name: str,
    flag: Optional[Any],
    config: Dict[str, Any],
    env: str,
    default: Any = None,
) -> Any:
    """CLI flag → config file → environment → default."""
    if flag is not None:
        return flag
    if config.get(name) is not None:
        return config[name]
    value = os.environ.get(env)
    return value if value else default


def _as_bool(value: Any) -> bool:
    """Coerce a config/env value to a flag.

    A JSON config yields a real ``bool``, but an environment variable is always a string —
    and ``bool("false")`` is ``True``, which would turn ``ACE_SIDECAR_NO_KEY=false`` into the
    opposite of what it says.
    """
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _is_loopback(host: str) -> bool:
    import ipaddress

    if host in ("localhost", ""):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def cmd_up(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    host = str(resolve("host", args.host, config, "ACE_SIDECAR_HOST", DEFAULT_HOST))
    port = int(resolve("port", args.port, config, "ACE_SIDECAR_PORT", DEFAULT_PORT))
    api_key = resolve("anthropic_api_key", args.key, config, "ANTHROPIC_API_KEY")
    base_url = resolve("base_url", args.base_url, config, "ACE_ANTHROPIC_BASE_URL")
    # The rest resolve through the same chain so a developer's usual invocation can live in
    # ~/.ace/config.json and `ace up` can be typed bare. Subscription (OAuth) users have no
    # key to configure and would otherwise pass --no-key forever just to say "normal case".
    no_key = _as_bool(
        resolve("no_key", args.no_key, config, "ACE_SIDECAR_NO_KEY", False)
    )
    log_level = str(
        resolve("log_level", args.log_level, config, "ACE_SIDECAR_LOG_LEVEL", "info")
    )
    allow_remote = _as_bool(
        resolve(
            "allow_remote", args.allow_remote, config, "ACE_SIDECAR_ALLOW_REMOTE", False
        )
    )
    no_telemetry = _as_bool(
        resolve(
            "no_telemetry", args.no_telemetry, config, "ACE_SIDECAR_NO_TELEMETRY", False
        )
    )
    telemetry_db = str(
        resolve(
            "telemetry_db",
            args.telemetry_db,
            config,
            "ACE_SIDECAR_TELEMETRY_DB",
            DEFAULT_DB,
        )
    )
    antigravity_dir = resolve(
        "antigravity_dir",
        args.antigravity_dir,
        config,
        "ACE_SIDECAR_ANTIGRAVITY_DIR",
    )
    if antigravity_dir:
        import ace.sidecar.insights as insights_mod

        insights_mod.ANTIGRAVITY_ROOT = os.path.expanduser(antigravity_dir)

    codex_dir = resolve(
        "codex_dir",
        args.codex_dir,
        config,
        "ACE_SIDECAR_CODEX_DIR",
    )
    if codex_dir:
        import ace.sidecar.insights as insights_mod

        insights_mod.CODEX_ROOT = os.path.expanduser(codex_dir)

    capture = resolve("capture", args.capture, config, "ACE_SIDECAR_CAPTURE")

    if not _is_loopback(host) and not allow_remote:
        print(
            f"ace: refusing to bind {host} — the sidecar holds a provider key and is meant "
            f"for 127.0.0.1 only.\n"
            f"     Pass --allow-remote if you really mean it (requests still get 403 unless "
            f"they arrive from loopback without proxy headers).",
            file=sys.stderr,
        )
        return 2

    if not api_key and not no_key:
        print(
            "ace: no Anthropic key configured. Provide one of:\n"
            "       ace up --key sk-ant-...\n"
            f'       echo \'{{"anthropic_api_key": "sk-ant-..."}}\' > {args.config}\n'
            "       export ANTHROPIC_API_KEY=sk-ant-...\n"
            "     Subscription (OAuth) users need no key here — Claude Code sends its own\n"
            "     credential and the sidecar relays it. Start with:  ace up --no-key\n"
            f'     ...or make that the default:  {{"no_key": true}} in {args.config}',
            file=sys.stderr,
        )
        return 2

    try:
        import uvicorn
    except ImportError:
        print(
            "ace: uvicorn is not installed — pip install -r requirements-proxy.txt",
            file=sys.stderr,
        )
        return 2

    from ace.sidecar import build_sidecar_app

    writer = None
    if capture:
        from ace.gateway.capture import DEFAULT_CAPTURE_DIR, CaptureWriter

        # `--capture` bare yields True; `--capture DIR` and a config/env value yield a path.
        # A config `true` has to mean the same as the bare flag, not a directory named "True".
        target = DEFAULT_CAPTURE_DIR if _as_bool(capture) else str(capture)
        writer = CaptureWriter.for_run(target)

    store = None
    if not no_telemetry:
        from ace.gateway.local_store import LocalStore

        store = LocalStore(telemetry_db)

    app = build_sidecar_app(
        api_key=api_key, base_url=base_url, capture=writer, accountant=store
    )
    url = f"http://{host}:{port}"

    print(f"\n  Dashboard: {url}/dashboard")
    print(f"  Health:    {url}/healthz")
    if store:
        print(f"  Telemetry: {store.path}  (local SQLite, never uploaded)")
    if writer:
        print(f"\n  RECORDING -> {writer.path}")
        print("  Contains your prompts and source. Local only; never commit it.")
    print()

    uvicorn.run(app, host=host, port=port, log_level=log_level)
    return 0


def cmd_env(args: argparse.Namespace) -> int:
    """Print the export line alone, for ``eval "$(ace env)"``."""
    config = load_config(args.config)
    host = str(resolve("host", args.host, config, "ACE_SIDECAR_HOST", DEFAULT_HOST))
    port = int(resolve("port", args.port, config, "ACE_SIDECAR_PORT", DEFAULT_PORT))
    print(f"export ANTHROPIC_BASE_URL=http://{host}:{port}")
    return 0


def cmd_setup_claude(args: argparse.Namespace) -> int:
    """Configure Claude Code to use local optimized MCP tools and disable vanilla tools."""
    from ace.sidecar.tools.claude_config import install_claude_config

    res = install_claude_config(workspace_dir=args.workspace)
    print("✨ Configured Claude Code with ACE Optimized Tools:")
    print(f"  Settings: {res['settings_path']}")
    print(f"  Agent:    {res['agent_path']}")
    print("\nNext steps:")
    print("1. Start sidecar: ace up")
    print("2. Run Claude Code pointing to sidecar:")
    print("   export ANTHROPIC_BASE_URL=http://127.0.0.1:8787")
    print("   claude")
    return 0


def cmd_mcp(args: argparse.Namespace) -> int:
    """Run the local stdio MCP server for Claude Code."""
    from ace.sidecar.tools.mcp_server import run_mcp_server

    run_mcp_server()
    return 0


def cmd_optimize(args: argparse.Namespace) -> int:
    """Run compression dry-run on a JSON request payload to preview token savings."""
    from ace.sidecar.compression.engine import ContextOptimizer

    if not args.file:
        print("Usage: ace optimize --file <request_payload.json>")
        return 1

    with open(args.file, "r", encoding="utf-8") as fh:
        payload = json.load(fh)

    opt = ContextOptimizer(max_tool_bytes=args.max_tool_bytes)
    res = opt.optimize_request(payload, force_idle_compaction=args.force_idle)

    print("🔍 Context Optimization Dry-Run Results:")
    print(f"  Bytes Saved:             {res.saved_bytes} bytes")
    print(f"  Estimated Tokens Saved:  ~{res.saved_tokens_est} tokens")
    print(f"  Tool Results Truncated:  {res.tool_results_truncated}")
    print(f"  Reads Deduplicated:      {res.reads_deduped}")
    print(f"  Prose Turns Compressed:  {res.prose_turns_compressed}")
    print(f"  Idle Compacted:          {res.idle_compacted}")
    return 0


def cmd_routing(args: argparse.Namespace) -> int:
    """Inspect or test local model routing configuration."""
    from ace.sidecar.routing.config import load_routing_config
    cfg = load_routing_config()

    action = getattr(args, "routing_action", "list") or "list"
    if action == "list":
        print("✨ ACE Model Routing Configuration:")
        print(f"  Active Profile:    {cfg.active_profile}")
        print(f"  Fallback Behavior: {cfg.fallback_behavior}")
        print(f"\n  Allowed Models ({len(cfg.allowed_models)}):")
        for m in cfg.allowed_models:
            spec = cfg.get_model_spec(m)
            tier_str = f"[{spec.tier.upper()}]" if spec else ""
            print(f"    - {m} {tier_str}")
        profile = cfg.profiles.get(cfg.active_profile)
        if profile:
            print(f"\n  Active Profile ('{profile.name}'):")
            print(f"    Exploration Model: {profile.exploration_model}")
            print(f"    Authoring Model:   {profile.authoring_model}")
            print(f"    Reasoning Model:   {profile.reasoning_model} (thinking={profile.enable_thinking_on_reasoning})")
        return 0

    if action == "test":
        from ace.sidecar.routing.router import SidecarRouter
        router = SidecarRouter(config=cfg)
        prompt = getattr(args, "prompt", "find where database is initialized")
        payload = {
            "model": "claude-3-7-sonnet-20250219",
            "messages": [{"role": "user", "content": prompt}],
        }
        mutated, dec = router.route_request(payload)
        print("✨ Routing Evaluation:")
        print(f"  Prompt:          {prompt}")
        print(f"  Action:          {dec.action.value}")
        print(f"  Original Model:  {dec.original_model}")
        print(f"  Target Model:    {dec.target_model}")
        print(f"  Reason:          {dec.reason}")
        if dec.stripped_parameters:
            print(f"  Stripped Params: {dec.stripped_parameters}")
        return 0
    return 0


_UP_EPILOG = f"""\
Every option above can also be set in {CONFIG_PATH} or in the environment, so the
invocation you use every day can be typed as a bare `ace up`. Precedence is always:

    command-line flag  ->  {CONFIG_PATH}  ->  environment  ->  built-in default

Config keys are the long flag name with dashes as underscores. For a subscription (OAuth)
Claude Code on a non-default port, with uvicorn's per-request log quietened:

    {{"no_key": true, "port": 8788, "log_level": "warning"}}

`ace up` then needs no flags at all. Precedence still holds, so `ace up --port 9000`
overrides the file for one run without editing it.
"""


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="ace",
        description="ACE — run a local sidecar in front of Anthropic and see what your "
        "coding sessions actually cost.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = p.add_subparsers(dest="command", metavar="{up,env}")

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--config",
        default=CONFIG_PATH,
        metavar="PATH",
        help="config file to read (default: %(default)s)",
    )
    common.add_argument(
        "--host",
        default=None,
        help=f"address to bind (config: host, env: ACE_SIDECAR_HOST, default: {DEFAULT_HOST})",
    )
    common.add_argument(
        "--port",
        type=int,
        default=None,
        help=f"port to bind (config: port, env: ACE_SIDECAR_PORT, default: {DEFAULT_PORT})",
    )

    up = sub.add_parser(
        "up",
        parents=[common],
        help="run the local sidecar",
        description="Run the local sidecar. Point Claude Code at it with `ace env`.",
        epilog=_UP_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    up.add_argument(
        "--key",
        default=None,
        metavar="SK",
        help="Anthropic API key to pay with (config: anthropic_api_key, "
        "env: ANTHROPIC_API_KEY). Omit it and callers supply their own",
    )
    up.add_argument(
        "--base-url",
        default=None,
        metavar="URL",
        help="override the Anthropic endpoint (config: base_url, "
        "env: ACE_ANTHROPIC_BASE_URL, default: https://api.anthropic.com)",
    )
    up.add_argument(
        "--allow-remote",
        action="store_true",
        default=None,  # see --no-key
        help="permit binding a non-loopback address. Does NOT grant access: loopback-trust "
        "auth still 403s anything that is not a local, unproxied caller "
        "(config: allow_remote, env: ACE_SIDECAR_ALLOW_REMOTE, default: false)",
    )
    up.add_argument(
        "--no-key",
        action="store_true",
        # NOT store_true's implicit False: `resolve` treats any non-None as "the user said
        # so", which would make the flag's absence outrank the config file and silently
        # ignore {"no_key": true}.
        default=None,
        help="start without a stored key, relaying whatever credential the caller sends — "
        "the normal case for subscription (OAuth) Claude Code "
        "(config: no_key, env: ACE_SIDECAR_NO_KEY, default: false)",
    )
    up.add_argument(
        "--capture",
        nargs="?",
        const=True,
        default=None,
        metavar="DIR",
        help="record full request bodies for analysis (P0-4). Contains your prompts and "
        "source — local only, never commit it "
        "(config: capture, env: ACE_SIDECAR_CAPTURE, default: off)",
    )
    up.add_argument(
        "--antigravity-dir",
        default=None,
        metavar="PATH",
        help="Antigravity transcript brain directory (config: antigravity_dir, "
        "env: ACE_SIDECAR_ANTIGRAVITY_DIR, default: ~/.gemini/antigravity/brain)",
    )
    up.add_argument(
        "--codex-dir",
        default=None,
        metavar="PATH",
        help="Codex session transcripts directory (config: codex_dir, "
        "env: ACE_SIDECAR_CODEX_DIR, default: ~/.codex/sessions)",
    )
    up.add_argument(
        "--telemetry-db",
        default=None,
        metavar="PATH",
        help="local SQLite turn log; numbers only, never uploaded (config: telemetry_db, "
        f"env: ACE_SIDECAR_TELEMETRY_DB, default: {DEFAULT_DB})",
    )
    up.add_argument(
        "--no-telemetry",
        action="store_true",
        default=None,  # see --no-key
        help="record no turns at all (the dashboard then shows transcript history only) "
        "(config: no_telemetry, env: ACE_SIDECAR_NO_TELEMETRY, default: false)",
    )
    up.add_argument(
        "--log-level",
        default=None,
        choices=["critical", "error", "warning", "info", "debug", "trace"],
        help="uvicorn log level; `warning` drops the per-request access line "
        "(config: log_level, env: ACE_SIDECAR_LOG_LEVEL, default: info)",
    )
    up.set_defaults(func=cmd_up)

    env = sub.add_parser(
        "env",
        parents=[common],
        help="print the export line",
        description='Print `export ANTHROPIC_BASE_URL=...` for use with eval "$(ace env)".',
    )
    env.set_defaults(func=cmd_env)

    setup_parser = sub.add_parser(
        "setup-claude",
        help="configure Claude Code workspace to use ACE optimized tools",
        description="Write .claude/settings.local.json and agent prompt to use optimized MCP tools.",
    )
    setup_parser.add_argument(
        "--workspace",
        default=".",
        help="workspace directory to configure (default: .)",
    )
    setup_parser.set_defaults(func=cmd_setup_claude)

    mcp_parser = sub.add_parser(
        "mcp",
        help="run local stdio Model Context Protocol (MCP) server",
        description="Run the local stdio MCP server for Claude Code.",
    )
    mcp_parser.set_defaults(func=cmd_mcp)

    opt_parser = sub.add_parser(
        "optimize",
        help="preview token savings on a request JSON file",
        description="Run dry-run context optimization on a request payload file.",
    )
    opt_parser.add_argument("--file", required=True, help="path to request JSON payload file")
    opt_parser.add_argument("--max-tool-bytes", type=int, default=2048, help="tool output byte threshold")
    opt_parser.add_argument("--force-idle", action="store_true", help="simulate post-300s TTL idle compaction")
    opt_parser.set_defaults(func=cmd_optimize)

    routing_parser = sub.add_parser(
        "routing",
        help="inspect or test model routing configuration",
        description="Inspect allowed models, active profiles, and test routing decisions.",
    )
    routing_sub = routing_parser.add_subparsers(dest="routing_action", metavar="{list,test}")
    r_list = routing_sub.add_parser("list", help="list active routing profile and allowed models")
    r_list.set_defaults(func=cmd_routing)

    r_test = routing_sub.add_parser("test", help="dry-run routing evaluation on a prompt")
    r_test.add_argument("prompt", nargs="?", default="find where database is initialized", help="prompt text to evaluate")
    r_test.set_defaults(func=cmd_routing)
    routing_parser.set_defaults(func=cmd_routing)

    return p


def main(argv: Optional[list] = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s:     %(message)s")
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 1
    return int(args.func(args) or 0)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

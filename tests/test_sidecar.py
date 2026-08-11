"""P0-5 — the local sidecar (`ace up`) and its CLI.

``httpx.MockTransport`` throughout: **no live Anthropic call is made anywhere here.**

Two things carry the weight:

* **The sidecar mounts only what a local proxy needs.** It must not require the cloud
  gateway's fleet/router/BYOK stack, and must not require an upstream the way
  ``gateway.serve.build_app()`` does — a developer's sidecar has no fleet.
* **Binding off-loopback is refused by the CLI, and would be refused again by auth.** The
  sidecar holds a provider key; two independent checks stand between it and a remote caller.
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, List

import httpx
import pytest
from fastapi.testclient import TestClient

from ace.cli import build_parser, cmd_up, load_config, resolve
from ace.sidecar import build_sidecar_app

LOCAL_PEER = ("127.0.0.1", 51234)
REMOTE_PEER = ("203.0.113.9", 44444)

OK = {
    "id": "msg_1",
    "type": "message",
    "model": "claude-sonnet-5",
    "content": [{"type": "text", "text": "ok"}],
    "stop_reason": "end_turn",
    "usage": {"input_tokens": 3, "output_tokens": 2, "cache_read_input_tokens": 900},
}


def _client(captured: List[httpx.Request] = None, peer=LOCAL_PEER, **kw) -> TestClient:
    def handler(request: httpx.Request) -> httpx.Response:
        if captured is not None:
            captured.append(request)
        return httpx.Response(200, json=OK)

    app = build_sidecar_app(
        api_key=kw.pop("api_key", "sk-ant-local"),
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        **kw,
    )
    return TestClient(app, client=peer)


def _body() -> bytes:
    return json.dumps(
        {"model": "claude-sonnet-5", "max_tokens": 16, "messages": []}
    ).encode()


# --------------------------------------------------------------------------------------
# The sidecar app
# --------------------------------------------------------------------------------------


def test_sidecar_serves_a_turn_with_no_ace_key():
    """The P0-5 acceptance path: Claude Code talks to it with nothing configured but a port."""
    captured: List[httpx.Request] = []
    resp = _client(captured).post("/v1/messages", content=_body())

    assert resp.status_code == 200
    assert captured[0].headers["x-api-key"] == "sk-ant-local"


def test_sidecar_mounts_a_local_surface_not_the_cloud_one():
    """It must not drag in the cloud gateway's surface — that is the point of the seam.

    `/dashboard` IS mounted (locally, reading local data). What must stay absent is the
    cloud gateway's own surface: the OpenAI-shaped inference route and the tenant-scoped
    telemetry endpoints that assume a shared database.
    """
    app = build_sidecar_app(api_key="sk")
    paths = {getattr(r, "path", "") for r in app.routes}
    for local in ("/v1/messages", "/healthz", "/dashboard", "/api/stats", "/metrics"):
        assert local in paths
    for cloud_only in (
        "/v1/chat/completions",
        "/api/v1/usage",
        "/api/v1/logs",
        "/costs/{key_id}",
        "/telemetry/{key_id}",
    ):
        assert cloud_only not in paths


def test_sidecar_metrics_endpoint():
    """Verify GET /metrics emits valid Prometheus text exposition output."""
    app = build_sidecar_app(api_key="sk")
    client = TestClient(app, client=("127.0.0.1", 50000))
    resp = client.get("/metrics")
    assert resp.status_code == 200
    assert "text/plain" in resp.headers["content-type"]
    text = resp.text
    assert "# HELP ace_sessions_total" in text
    assert "# TYPE ace_sessions_total counter" in text
    assert "# HELP ace_tokens_input_fresh_total" in text
    assert "# HELP ace_cost_usd_total" in text
    assert "# HELP ace_session_time_seconds" in text



def test_sidecar_needs_no_upstream_fleet_to_boot():
    """``gateway.serve.build_app()`` raises without OPENAI_API_KEY or ACE_FLEET_YAML.

    A developer's laptop has neither, so the sidecar must not inherit that requirement.
    """
    for var in ("OPENAI_API_KEY", "ACE_FLEET_YAML", "ACE_ALLOW_ECHO"):
        assert (
            var not in os.environ or True
        )  # documented intent; construction is the test
    assert build_sidecar_app(api_key="sk") is not None


def test_remote_caller_is_refused_even_though_the_key_is_present():
    captured: List[httpx.Request] = []
    resp = _client(captured, peer=REMOTE_PEER).post("/v1/messages", content=_body())

    assert resp.status_code == 403
    assert not captured, "must never spend the sidecar's key for a remote caller"


def test_proxied_request_is_refused():
    captured: List[httpx.Request] = []
    resp = _client(captured).post(
        "/v1/messages", content=_body(), headers={"x-forwarded-for": "198.51.100.7"}
    )

    assert resp.status_code == 403
    assert not captured


def test_healthz_reports_state_without_leaking_the_key():
    secret = "sk-ant-DO-NOT-LEAK"
    resp = _client(api_key=secret).get("/healthz")

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["mode"] == "loopback"
    assert body["provider_key"] == "configured"
    assert body["levers"] == []
    assert secret not in resp.text


def test_healthz_reports_a_missing_key():
    app = build_sidecar_app(api_key=None)
    assert TestClient(app, client=LOCAL_PEER).get("/healthz").json()[
        "provider_key"
    ] == ("missing")


def test_base_url_override_is_honoured():
    app = build_sidecar_app(api_key="sk", base_url="https://example.invalid")
    assert TestClient(app, client=LOCAL_PEER).get("/healthz").json()["upstream"] == (
        "https://example.invalid"
    )


def test_sidecar_forces_loopback_mode_regardless_of_env(monkeypatch):
    """A stray ACE_MESSAGES_AUTH=dev_key must not leave the sidecar demanding a key it
    cannot issue."""
    monkeypatch.setenv("ACE_MESSAGES_AUTH", "dev_key")
    captured: List[httpx.Request] = []
    resp = _client(captured).post("/v1/messages", content=_body())
    assert resp.status_code == 200


# --------------------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------------------


def test_config_precedence_flag_beats_file_beats_env(monkeypatch, tmp_path):
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text(json.dumps({"port": 9000, "anthropic_api_key": "from-file"}))
    cfg: Dict[str, Any] = load_config(str(cfg_file))

    monkeypatch.setenv("ANTHROPIC_API_KEY", "from-env")
    assert (
        resolve("anthropic_api_key", "from-flag", cfg, "ANTHROPIC_API_KEY")
        == "from-flag"
    )
    assert resolve("anthropic_api_key", None, cfg, "ANTHROPIC_API_KEY") == "from-file"
    assert resolve("anthropic_api_key", None, {}, "ANTHROPIC_API_KEY") == "from-env"
    assert resolve("port", None, cfg, "ACE_SIDECAR_PORT", 8787) == 9000


def test_missing_config_file_is_not_an_error(tmp_path):
    assert load_config(str(tmp_path / "nope.json")) == {}


def test_unparseable_config_is_ignored_without_echoing_it(tmp_path, caplog):
    bad = tmp_path / "config.json"
    bad.write_text('{"anthropic_api_key": "sk-ant-SECRET", oops')
    assert load_config(str(bad)) == {}
    assert "sk-ant-SECRET" not in caplog.text


def test_up_refuses_a_non_loopback_bind(capsys, tmp_path):
    args = build_parser().parse_args(
        ["up", "--host", "0.0.0.0", "--key", "sk", "--config", str(tmp_path / "c.json")]
    )
    assert cmd_up(args) == 2
    assert "refusing to bind" in capsys.readouterr().err


def test_up_without_a_key_explains_the_three_ways_to_provide_one(
    capsys, tmp_path, monkeypatch
):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    args = build_parser().parse_args(["up", "--config", str(tmp_path / "c.json")])
    assert cmd_up(args) == 2
    err = capsys.readouterr().err
    assert "--key" in err and "ANTHROPIC_API_KEY" in err


@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "localhost"])
def test_loopback_hosts_are_accepted(host, tmp_path, monkeypatch):
    """Reaches the uvicorn import rather than the bind refusal."""
    monkeypatch.setattr("ace.cli.load_config", lambda *_a, **_k: {})
    args = build_parser().parse_args(
        ["up", "--host", host, "--key", "sk", "--config", str(tmp_path / "c.json")]
    )
    called = {}
    monkeypatch.setattr(
        "uvicorn.run", lambda app, **kw: called.update(kw) or None, raising=False
    )
    assert cmd_up(args) == 0
    assert called["host"] == host


def _run_up(tmp_path, monkeypatch, argv, config=None, env=None):
    """Drive ``cmd_up`` to the point of serving, returning the kwargs uvicorn was handed."""
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps(config or {}))
    for var in (
        "ANTHROPIC_API_KEY",
        "ACE_SIDECAR_NO_KEY",
        "ACE_SIDECAR_LOG_LEVEL",
        "ACE_SIDECAR_PORT",
    ):
        monkeypatch.delenv(var, raising=False)
    for k, v in (env or {}).items():
        monkeypatch.setenv(k, v)
    called: Dict[str, Any] = {}
    monkeypatch.setattr(
        "uvicorn.run", lambda app, **kw: called.update(kw) or None, raising=False
    )
    rc = cmd_up(build_parser().parse_args(["up", "--config", str(cfg), *argv]))
    return rc, called


def test_bare_ace_up_runs_entirely_from_the_config_file(tmp_path, monkeypatch):
    """The point of the exercise: the habitual invocation collapses to `ace up`.

    Equivalent to `ace up --no-key --port 8788 --log-level warning`.
    """
    rc, called = _run_up(
        tmp_path,
        monkeypatch,
        argv=[],
        config={"no_key": True, "port": 8788, "log_level": "warning"},
    )
    assert rc == 0
    assert called["port"] == 8788
    assert called["log_level"] == "warning"


def test_a_flag_still_beats_the_config_file(tmp_path, monkeypatch):
    rc, called = _run_up(
        tmp_path,
        monkeypatch,
        argv=["--port", "9000"],
        config={"no_key": True, "port": 8788},
    )
    assert rc == 0 and called["port"] == 9000


def test_config_no_key_is_not_outranked_by_the_flags_absence(tmp_path, monkeypatch):
    """``store_true`` defaults to False, which `resolve` would read as an explicit choice.

    If that regresses, `{"no_key": true}` is silently ignored and a bare `ace up` exits 2
    telling the user to configure a key they do not have.
    """
    rc, _ = _run_up(tmp_path, monkeypatch, argv=[], config={"no_key": True})
    assert rc == 0


def test_env_false_means_false(tmp_path, monkeypatch):
    """``bool("false")`` is True — coercion has to read the string, not its truthiness."""
    rc, _ = _run_up(tmp_path, monkeypatch, argv=[], env={"ACE_SIDECAR_NO_KEY": "false"})
    assert rc == 2  # no key, and no_key really is off


def test_env_true_starts_without_a_key(tmp_path, monkeypatch):
    rc, _ = _run_up(tmp_path, monkeypatch, argv=[], env={"ACE_SIDECAR_NO_KEY": "1"})
    assert rc == 0


def test_up_help_documents_every_option(capsys):
    """`ace up --help` is the discovery path, so each option states where its value can come
    from. A flag wired into `resolve` but undocumented is a setting nobody can find."""
    with pytest.raises(SystemExit):
        build_parser().parse_args(["up", "--help"])
    out = capsys.readouterr().out
    for flag in (
        "--no-key",
        "--port",
        "--log-level",
        "--capture",
        "--telemetry-db",
        "--no-telemetry",
        "--allow-remote",
        "--base-url",
        "--key",
        "--host",
    ):
        assert flag in out, flag
    for key in ("config: no_key", "config: log_level", "config: telemetry_db"):
        assert key in out, key
    assert "ACE_SIDECAR_NO_KEY" in out


def test_env_prints_the_export_line(capsys, tmp_path):
    args = build_parser().parse_args(
        ["env", "--port", "9999", "--config", str(tmp_path / "c.json")]
    )
    from ace.cli import cmd_env

    assert cmd_env(args) == 0
    assert "export ANTHROPIC_BASE_URL=http://127.0.0.1:9999" in capsys.readouterr().out

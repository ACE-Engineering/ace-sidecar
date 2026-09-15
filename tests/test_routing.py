"""tests/test_routing.py — Unit and integration tests for ACE Sidecar model routing."""

import httpx
import pytest
from ace.sidecar.proxy import create_sidecar_app
from ace.sidecar.routing import (
    DeploymentRoutingConfig,
    ModelRouter,
    ProfileConfig,
    SensitivityClassifier,
    SensitivityLevel,
    SidecarRouter,
    TriggerAction,
)


def test_sensitivity_classifier_detects_secrets():
    clf = SensitivityClassifier()
    assert clf.classify("Here is my secret AKIAIOSFODNN7EXAMPLE") == SensitivityLevel.RESTRICTED
    assert clf.classify("GitHub PAT: ghp_123456789012345678901234567890123456") == SensitivityLevel.RESTRICTED
    assert clf.classify("Just normal python code: def foo(): pass") == SensitivityLevel.INTERNAL


def test_sensitivity_classifier_detects_sensitive_paths():
    clf = SensitivityClassifier()
    assert clf.classify("read file", file_paths=[".env"]) == SensitivityLevel.RESTRICTED
    assert clf.classify("read file", file_paths=["secrets/credentials.yaml"]) == SensitivityLevel.RESTRICTED
    assert clf.classify("read file", file_paths=["id_ed25519"]) == SensitivityLevel.RESTRICTED
    assert clf.classify("read file", file_paths=["src/ace/proxy.py"]) == SensitivityLevel.INTERNAL


def test_cost_based_initial_selection():
    router = ModelRouter()

    # Exploration -> chooses lowest-cost Haiku ($0.80/1M)
    payload_explore = {
        "model": "claude-3-7-sonnet-20250219",
        "messages": [{"role": "user", "content": "where is the login handler defined?"}],
    }
    mutated_exp, dec_exp = router.route_turn(payload_explore, session_id="cost-sess-1")
    assert dec_exp.action == TriggerAction.INITIALIZE
    assert dec_exp.target_model == "claude-3-5-haiku-20241022"
    assert mutated_exp["model"] == "claude-3-5-haiku-20241022"

    # Deep reasoning -> chooses Sonnet 3.7
    payload_deep = {
        "model": "claude-3-7-sonnet-20250219",
        "messages": [{"role": "user", "content": "architect a distributed consensus protocol and analyze deadlock risks"}],
    }
    mutated_deep, dec_deep = router.route_turn(payload_deep, session_id="cost-sess-2")
    assert dec_deep.target_model == "claude-3-7-sonnet-20250219"


def test_session_stickiness_and_prompt_cache():
    router = ModelRouter()

    # Turn 1: simple grep -> anchors to Haiku
    p1 = {
        "model": "claude-3-7-sonnet-20250219",
        "messages": [{"role": "user", "content": "find all occurrences of calculate_hash"}],
    }
    m1, d1 = router.route_turn(p1, session_id="sticky-sess")
    assert d1.target_model == "claude-3-5-haiku-20241022"

    # Turn 2: another read -> STICKS to Haiku anchor without thrashing
    p2 = {
        "model": "claude-3-7-sonnet-20250219",
        "messages": [
            {"role": "user", "content": "find all occurrences of calculate_hash"},
            {"role": "assistant", "content": "Found in hash.py"},
            {"role": "user", "content": "read lines 20 to 50 in hash.py"},
        ],
    }
    m2, d2 = router.route_turn(p2, session_id="sticky-sess")
    assert d2.action == TriggerAction.MAINTAIN_ANCHOR
    assert d2.target_model == "claude-3-5-haiku-20241022"

    # Turn 3: third turn -> continues sticking to Haiku anchor
    p3 = {
        "model": "claude-3-7-sonnet-20250219",
        "messages": [
            {"role": "user", "content": "read lines 20 to 50 in hash.py"},
            {"role": "assistant", "content": "Displayed lines"},
            {"role": "user", "content": "also check util.py for imports"},
        ],
    }
    m3, d3 = router.route_turn(p3, session_id="sticky-sess")
    assert d3.action == TriggerAction.MAINTAIN_ANCHOR
    assert d3.target_model == "claude-3-5-haiku-20241022"


def test_explicit_user_override_escape_hatch():
    router = ModelRouter()

    # Anchor to Haiku first
    p1 = {
        "model": "claude-3-7-sonnet-20250219",
        "messages": [{"role": "user", "content": "find all tests"}],
    }
    router.route_turn(p1, session_id="override-sess")

    # User says "use opus" -> explicit override trigger fires, breaks stickiness
    p2 = {
        "model": "claude-3-7-sonnet-20250219",
        "messages": [{"role": "user", "content": "use opus to evaluate this architecture plan"}],
    }
    m2, d2 = router.route_turn(p2, session_id="override-sess")
    assert d2.action == TriggerAction.EXPLICIT_OVERRIDE
    assert d2.target_model == "claude-3-opus-20240229"
    assert m2["model"] == "claude-3-opus-20240229"


def test_complexity_jump_escalation_escape_hatch():
    router = ModelRouter()

    # Turn 1: Anchored to Haiku (C = 0.25)
    p1 = {
        "model": "claude-3-7-sonnet-20250219",
        "messages": [{"role": "user", "content": "where is the payment router?"}],
    }
    router.route_turn(p1, session_id="jump-sess")

    # Turn 2: Complexity jumps significantly (architect / concurrency keywords, C = 0.90)
    p2 = {
        "model": "claude-3-7-sonnet-20250219",
        "messages": [{"role": "user", "content": "architect a complete rewrite of the payment router to handle distributed deadlock resolution"}],
    }
    m2, d2 = router.route_turn(p2, session_id="jump-sess")
    assert d2.action == TriggerAction.ESCALATE
    assert d2.target_model == "claude-3-7-sonnet-20250219"
    assert "jumped" in d2.reason


def test_consecutive_failure_escalation():
    router = ModelRouter()

    # Anchor to Haiku
    p1 = {
        "model": "claude-3-7-sonnet-20250219",
        "messages": [{"role": "user", "content": "where is user_service.py?"}],
    }
    router.route_turn(p1, session_id="fail-sess")

    # Simulate 2 consecutive tool failures
    session = router.sessions["fail-sess"]
    session.consecutive_tool_failures = 2

    p2 = {
        "model": "claude-3-7-sonnet-20250219",
        "messages": [{"role": "user", "content": "the test is still failing"}],
    }
    m2, d2 = router.route_turn(p2, session_id="fail-sess")
    assert d2.action == TriggerAction.ESCALATE
    assert d2.target_model == "claude-3-7-sonnet-20250219"
    assert "consecutive failures" in d2.reason


def test_parameter_adaptation_on_haiku():
    router = ModelRouter()
    payload = {
        "model": "claude-3-7-sonnet-20250219",
        "max_tokens": 64000,
        "thinking": {"type": "enabled", "budget_tokens": 2048},
        "messages": [{"role": "user", "content": "grep for all tests in tests/"}],
    }
    mutated, dec = router.route_turn(payload, session_id="param-sess")
    assert dec.target_model == "claude-3-5-haiku-20241022"
    # thinking stripped
    assert "thinking" not in mutated
    assert "thinking" in dec.stripped_parameters
    # max_tokens clamped to 8192
    assert mutated["max_tokens"] == 8192


def test_customizable_deployment_config():
    # Customer config: adds AWS Bedrock Claude ARN
    cfg = DeploymentRoutingConfig(
        allowed_models=[
            "anthropic.claude-3-5-haiku-20241022-v1:0",
            "anthropic.claude-3-7-sonnet-20250219-v1:0",
        ],
        allowed_model_patterns=[r"^anthropic\.claude-.*"],
        profiles={
            "balanced": ProfileConfig(
                exploration_model="anthropic.claude-3-5-haiku-20241022-v1:0",
                authoring_model="anthropic.claude-3-7-sonnet-20250219-v1:0",
            )
        }
    )
    router = ModelRouter(config=cfg)

    payload = {
        "model": "anthropic.claude-3-7-sonnet-20250219-v1:0",
        "messages": [{"role": "user", "content": "where is the handler?"}],
    }
    mutated, dec = router.route_turn(payload, session_id="custom-sess")
    assert dec.target_model == "anthropic.claude-3-5-haiku-20241022-v1:0"
    assert mutated["model"] == "anthropic.claude-3-5-haiku-20241022-v1:0"


@pytest.mark.asyncio
async def test_proxy_end_to_end_routing():
    router = SidecarRouter()

    # Create mock upstream client
    def handler(request: httpx.Request) -> httpx.Response:
        # Echo back the model received by upstream
        import json
        body = json.loads(request.content.decode("utf-8"))
        model_used = body.get("model", "")
        return httpx.Response(
            200,
            json={"id": "msg_1", "type": "message", "role": "assistant", "model": model_used, "content": [{"type": "text", "text": "OK"}]},
        )

    mock_transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=mock_transport) as mock_client:
        app = create_sidecar_app(router=router, client=mock_client)

        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1") as client:
            resp = await client.post(
                "/v1/messages",
                json={
                    "model": "claude-3-7-sonnet-20250219",
                    "max_tokens": 1000,
                    "messages": [{"role": "user", "content": "find where config is loaded"}],
                },
                headers={"x-session-id": "proxy-sess-1"},
            )
            assert resp.status_code == 200
            data = resp.json()
            # Verify upstream was sent the routed model (Haiku)
            assert data["model"] == "claude-3-5-haiku-20241022"
            # Verify routing response headers
            assert resp.headers["x-ace-routed-model"] == "claude-3-5-haiku-20241022"
            assert resp.headers["x-ace-original-model"] == "claude-3-7-sonnet-20250219"
            assert resp.headers["x-ace-routing-action"] == "initialize"

            # Check stats
            stats_resp = await client.get("/api/stats")
            stats = stats_resp.json()["stats"]
            assert stats["routing"]["turns_routed"] == 1
            assert stats["routing"]["models_selected"]["claude-3-5-haiku-20241022"] == 1

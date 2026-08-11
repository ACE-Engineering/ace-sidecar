#!/usr/bin/env python3
"""E2E verification script for ACE Sidecar dashboard, routes, and agent filters."""

import json
import sys
import httpx
from fastapi.testclient import TestClient
from ace.sidecar import build_sidecar_app

def main():
    print("==> Starting E2E Verification for ACE Sidecar...")
    
    # 1. Instantiate live FastAPI application with mock transport
    def dummy_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"type": "message", "role": "assistant"})

    client_mock = httpx.AsyncClient(transport=httpx.MockTransport(dummy_handler))
    app = build_sidecar_app(api_key="sk-test-key", client=client_mock)
    client = TestClient(app, client=("127.0.0.1", 54321))

    # 2. Test /healthz endpoint
    print("  [1/6] Testing GET /healthz ...", end=" ")
    r = client.get("/healthz")
    assert r.status_code == 200, f"Expected 200, got {r.status_code}"
    health_data = r.json()
    assert health_data.get("status") == "ok", f"Health status invalid: {health_data}"
    print("PASSED ✓")

    # 3. Test default /dashboard route
    print("  [2/6] Testing GET /dashboard (default 30d, all agents) ...", end=" ")
    r = client.get("/dashboard")
    assert r.status_code == 200, f"Expected 200, got {r.status_code}"
    assert "Heterogeneous Coding Agent Observability" in r.text
    assert "All Agents" in r.text
    print("PASSED ✓")

    # 4. Test /dashboard with Agent Filtering (antigravity)
    print("  [3/6] Testing GET /dashboard?range=7d&agent=antigravity ...", end=" ")
    r = client.get("/dashboard?range=7d&agent=antigravity")
    assert r.status_code == 200, f"Expected 200, got {r.status_code}"
    assert "class=\"on\" href='?range=7d&agent=antigravity'" in r.text or "Antigravity (Google)" in r.text
    print("PASSED ✓")

    # 5. Test /dashboard with Agent Filtering (claude)
    print("  [4/6] Testing GET /dashboard?range=90d&agent=claude ...", end=" ")
    r = client.get("/dashboard?range=90d&agent=claude")
    assert r.status_code == 200, f"Expected 200, got {r.status_code}"
    assert "Claude Code" in r.text
    print("PASSED ✓")

    # 6. Test /api/stats JSON Endpoint
    print("  [5/6] Testing GET /api/stats?range=30d&agent=antigravity ...", end=" ")
    r = client.get("/api/stats?range=30d&agent=antigravity")
    assert r.status_code == 200, f"Expected 200, got {r.status_code}"
    stats_data = r.json()
    assert stats_data.get("agent") == "antigravity", f"Expected agent antigravity, got {stats_data.get('agent')}"
    assert "agent_breakdown" in stats_data
    print("PASSED ✓")

    # 7. Test /api/report JSON Endpoint
    print("  [6/6] Testing GET /api/report?range=30d&agent=claude ...", end=" ")
    r = client.get("/api/report?range=30d&agent=claude")
    assert r.status_code == 200, f"Expected 200, got {r.status_code}"
    report_data = r.json()
    assert "session_time_hours" in report_data
    assert "agent_breakdown" in report_data
    print("PASSED ✓")

    print("\n==> ALL 6 E2E ENDPOINT TESTS PASSED SUCCESSFULLY! 🎉")

if __name__ == "__main__":
    main()

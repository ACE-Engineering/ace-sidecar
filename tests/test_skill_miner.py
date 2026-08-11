from fastapi.testclient import TestClient

from ace.sidecar import build_sidecar_app
from ace.sidecar.skill_miner import (
    _scan_transcript_logs,
    install_local_skill,
    mine_local_skills,
)


def test_mine_local_skills_returns_curated_proposals():
    proposals = mine_local_skills()
    assert len(proposals) > 0
    top = proposals[0]
    assert "id" in top
    assert "name" in top
    assert top["occurrences"] > 0
    assert top["trigger_command"].startswith("/")
    assert "description" in top
    assert "---" in top["skill_md"]
    assert top["estimated_tokens_saved"] > 0


def test_scan_transcript_logs():
    seqs, prompts = _scan_transcript_logs()
    assert isinstance(seqs, list)
    assert isinstance(prompts, dict) or hasattr(prompts, "most_common")


def test_install_local_skill(tmp_path):
    workspace = str(tmp_path)
    skill_id = "dev-clean-env"
    skill_md = "---\nname: dev-clean-env\ndescription: Automated clean\n---\n# Dev Clean\n1. Run make clean\n"

    res = install_local_skill(workspace, skill_id, skill_md)
    assert res["status"] == "success"
    assert res["skill_id"] == "dev-clean-env"
    assert "installed_path" in res
    assert "trigger_instruction" in res

    expected_file = tmp_path / ".agents" / "skills" / "dev-clean-env" / "SKILL.md"
    assert expected_file.exists()
    assert expected_file.read_text(encoding="utf-8") == skill_md


def test_install_skill_api_endpoint(tmp_path):
    app = build_sidecar_app()
    client = TestClient(app)

    payload = {
        "skill_id": "test-auto-skill",
        "skill_md": "---\nname: test-auto-skill\n---\n# Test\n",
        "workspace_dir": str(tmp_path),
    }

    res = client.post("/api/skills/install", json=payload)
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "success"
    assert data["skill_id"] == "test-auto-skill"
    assert "Type `/test-auto-skill`" in data["trigger_instruction"]

    installed_file = tmp_path / ".agents" / "skills" / "test-auto-skill" / "SKILL.md"
    assert installed_file.exists()

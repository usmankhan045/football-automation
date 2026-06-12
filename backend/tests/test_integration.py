"""Integration audit for the human-in-the-loop workflow lifecycle.

Drives the real FastAPI app (in-process, via TestClient) through the full
sequence: start -> script-approval interrupt -> mock-approve edited script ->
asset-gathering interrupt -> upload clips -> rendering/completion.

Runs offline: GROQ_API_KEY is stripped so Node B uses its deterministic
fallback, and STORAGE_ROOT is redirected to a tmp dir so uploads don't pollute
the workspace.
"""

from __future__ import annotations

import importlib

import pytest
from fastapi.testclient import TestClient

from app.schemas import WorkflowStatus


@pytest.fixture
def client(monkeypatch, tmp_path):
    """A fresh app instance with an empty checkpointer and isolated storage."""
    # Strip every live integration so Node A/B run fully offline + deterministic.
    for var in ("GROQ_API_KEY", "HIGHLIGHTLY_API_KEY", "FOOTBALL_DATA_TOKEN"):
        monkeypatch.delenv(var, raising=False)

    # Reimport main so the module-level ENGINE/CHECKPOINTER start clean per test.
    import app.main as main

    importlib.reload(main)
    monkeypatch.setattr(main, "STORAGE_ROOT", tmp_path / "assets")

    return TestClient(main.app)


def _start(client, match_id: str) -> dict:
    resp = client.post("/api/workflow/start", json={"match_id": match_id})
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_start_triggers_script_interrupt(client):
    """Launching a thread halts at the human-validation checkpoint."""
    body = _start(client, "itg-start")

    assert body["interrupted"] is True
    assert body["status"] == WorkflowStatus.PENDING_APPROVAL.value
    assert body["interrupt_payload"]["checkpoint"] == "HUMAN_VALIDATION_REQUIRED"
    assert body["script_raw"]
    assert len(body["video_prompts"]) >= 4


def test_approve_with_edits_advances_to_asset_gathering(client):
    """The core HITL path: edit + approve -> PROCESSING_ASSETS asset interrupt."""
    match_id = "itg-approve"
    _start(client, match_id)

    edited_script = "Edited by the analyst: they got absolutely cooked on the counter."
    edited_prompts = [
        "Holographic counter-attack arrows, cyan wireframe pitch, no faces",
        "Glowing xG bloom collapsing as the defensive line fractures",
        "Floating neon stat panels orbiting a luminous ball",
        "Final scoreline igniting in volumetric light, electric shockwave",
    ]

    resp = client.post(
        f"/api/workflow/{match_id}/approve",
        json={"script_raw": edited_script, "visual_prompts": edited_prompts},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()

    # Edits were committed via update_state and survived the resume.
    assert body["script_raw"] == edited_script
    assert body["video_prompts"] == edited_prompts

    # Workflow advanced from approval into asset gathering and re-interrupted.
    assert body["status"] == WorkflowStatus.PROCESSING_ASSETS.value
    assert body["interrupted"] is True
    assert body["interrupt_payload"]["checkpoint"] == "ASSET_UPLOAD_REQUIRED"
    assert body["interrupt_payload"]["expected_clips"] == len(edited_prompts)
    assert "await_assets" in body["next_nodes"]

    # And it is observable through the polling endpoint too.
    state = client.get(f"/api/workflow/{match_id}/state").json()
    assert state["status"] == WorkflowStatus.PROCESSING_ASSETS.value
    assert state["interrupted"] is True


def test_partial_upload_stays_in_processing(client, tmp_path):
    """Uploading fewer clips than prompts keeps the thread in PROCESSING_ASSETS."""
    match_id = "itg-partial"
    _start(client, match_id)
    approve = client.post(f"/api/workflow/{match_id}/approve", json={}).json()
    expected = approve["interrupt_payload"]["expected_clips"]

    files = [("files", ("clip1.mp4", b"\x00\x00fake", "video/mp4"))]
    resp = client.post(f"/api/workflow/{match_id}/upload-assets", files=files)
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["uploaded_clips"] == 1
    assert body["expected_clips"] == expected
    assert body["complete"] is False
    assert body["status"] == WorkflowStatus.PROCESSING_ASSETS.value


def test_full_upload_advances_to_rendering(client):
    """Uploading one clip per prompt clears the asset gate and renders out."""
    match_id = "itg-full"
    _start(client, match_id)
    approve = client.post(f"/api/workflow/{match_id}/approve", json={}).json()
    expected = approve["interrupt_payload"]["expected_clips"]

    files = [
        ("files", (f"clip{i}.mp4", b"\x00\x00fake-veo-bytes", "video/mp4"))
        for i in range(expected)
    ]
    resp = client.post(f"/api/workflow/{match_id}/upload-assets", files=files)
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["uploaded_clips"] == expected
    assert body["complete"] is True
    # Passed through RENDERING into the final assembly node.
    assert body["status"] == WorkflowStatus.COMPLETED.value

    state = client.get(f"/api/workflow/{match_id}/state").json()
    assert state["status"] == WorkflowStatus.COMPLETED.value
    assert state["interrupted"] is False
    assert state["next_nodes"] == []


def test_non_mp4_rejected(client):
    """Non-.mp4 uploads are rejected with a 400."""
    match_id = "itg-badfile"
    _start(client, match_id)
    client.post(f"/api/workflow/{match_id}/approve", json={})

    files = [("files", ("notes.txt", b"hello", "text/plain"))]
    resp = client.post(f"/api/workflow/{match_id}/upload-assets", files=files)
    assert resp.status_code == 400


def test_approve_requires_active_interrupt(client):
    """Approving a thread that is not awaiting validation returns 409."""
    match_id = "itg-doubleapprove"
    _start(client, match_id)
    client.post(f"/api/workflow/{match_id}/approve", json={})  # now at asset gate

    # Second approve is invalid — no script-validation interrupt is active.
    resp = client.post(f"/api/workflow/{match_id}/approve", json={})
    assert resp.status_code == 409


def test_upload_before_approval_rejected(client):
    """Uploading assets before script approval returns 409."""
    match_id = "itg-early-upload"
    _start(client, match_id)

    files = [("files", ("clip1.mp4", b"data", "video/mp4"))]
    resp = client.post(f"/api/workflow/{match_id}/upload-assets", files=files)
    assert resp.status_code == 409


def test_list_endpoint_reflects_started_threads(client):
    """GET /api/workflow lists launched threads for the dashboard roster."""
    assert client.get("/api/workflow").json() == []

    _start(client, "itg-list-a")
    _start(client, "itg-list-b")

    listed = client.get("/api/workflow").json()
    ids = [t["match_id"] for t in listed]
    assert ids == ["itg-list-a", "itg-list-b"]
    assert all(t["status"] == "PENDING_APPROVAL" for t in listed)


def test_download_404_before_render(client):
    """The download endpoint 404s until a master .mp4 exists."""
    _start(client, "itg-dl")
    assert client.get("/api/workflow/itg-dl/download").status_code == 404


def test_unknown_match_returns_404(client):
    """Operating on an unknown thread returns 404."""
    assert client.post("/api/workflow/ghost/approve", json={}).status_code == 404
    files = [("files", ("clip1.mp4", b"data", "video/mp4"))]
    assert (
        client.post("/api/workflow/ghost/upload-assets", files=files).status_code == 404
    )

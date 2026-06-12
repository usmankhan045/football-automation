"""Execution audit for the LangGraph workflow.

These tests run fully offline (no GROQ_API_KEY required): Node B falls back to
a deterministic script, so the structural guarantees — clean compilation and a
safe interrupt at the human-validation checkpoint — can be verified in CI.
"""

from __future__ import annotations

import os

import pytest
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command

from app.graph import build_graph, compile_graph
from app.schemas import WorkflowStatus


@pytest.fixture(autouse=True)
def _force_offline(monkeypatch):
    """Guarantee the offline fallback path so tests never hit the network."""
    monkeypatch.delenv("GROQ_API_KEY", raising=False)


@pytest.fixture
def engine():
    return compile_graph(MemorySaver())


def _config(match_id: str) -> dict:
    return {"configurable": {"thread_id": match_id}}


def test_graph_compiles():
    """The graph must compile cleanly with a checkpointer attached."""
    compiled = build_graph().compile(checkpointer=MemorySaver())
    assert compiled is not None
    # Sanity: every declared node is present in the compiled graph.
    nodes = set(compiled.get_graph().nodes.keys())
    for expected in (
        "scrape_match_data",
        "generate_tactical_script",
        "human_approval",
        "process_rendering",
    ):
        assert expected in nodes


def test_interrupt_triggers_safely(engine):
    """Running the thread must halt at the human-validation interrupt."""
    config = _config("wc-test-interrupt")
    result = engine.invoke({"match_id": "wc-test-interrupt"}, config=config)

    # LangGraph surfaces pending interrupts under the __interrupt__ key.
    assert "__interrupt__" in result, "Graph did not pause at the interrupt checkpoint"

    interrupt_obj = result["__interrupt__"][0]
    assert interrupt_obj.value["checkpoint"] == "HUMAN_VALIDATION_REQUIRED"

    # State was scraped + scripted and is awaiting approval before rendering.
    snapshot = engine.get_state(config)
    assert snapshot.values["status"] == WorkflowStatus.PENDING_APPROVAL.value
    assert snapshot.values["script_raw"]  # a script was generated
    assert "human_approval" in snapshot.next  # paused right before the checkpoint


def test_resume_after_approval_halts_at_asset_gate(engine):
    """Approval advances to PROCESSING_ASSETS and re-interrupts for uploads."""
    config = _config("wc-test-resume")
    engine.invoke({"match_id": "wc-test-resume"}, config=config)

    after_approval = engine.invoke(Command(resume={"approved": True}), config=config)

    # Second checkpoint: awaiting Veo asset uploads.
    assert "__interrupt__" in after_approval
    assert after_approval["__interrupt__"][0].value["checkpoint"] == "ASSET_UPLOAD_REQUIRED"
    assert after_approval["status"] == WorkflowStatus.PROCESSING_ASSETS.value

    snapshot = engine.get_state(config)
    assert "await_assets" in snapshot.next


def test_full_lifecycle_resumes_to_completed(engine):
    """Approving then clearing the asset gate drives the thread to COMPLETED."""
    config = _config("wc-test-lifecycle")
    engine.invoke({"match_id": "wc-test-lifecycle"}, config=config)
    engine.invoke(Command(resume={"approved": True}), config=config)

    final = engine.invoke(Command(resume={"assets_ready": True}), config=config)

    assert final["status"] == WorkflowStatus.COMPLETED.value
    snapshot = engine.get_state(config)
    assert not snapshot.next  # graph reached END
    assert "__interrupt__" not in final


def test_rejection_parks_thread(engine):
    """Resuming with a rejection keeps the thread at PENDING_APPROVAL."""
    config = _config("wc-test-reject")
    engine.invoke({"match_id": "wc-test-reject"}, config=config)

    engine.invoke(Command(resume={"approved": False}), config=config)

    snapshot = engine.get_state(config)
    assert snapshot.values["status"] == WorkflowStatus.PENDING_APPROVAL.value

"""FastAPI surface for the football tactical automation engine.

Mounts the compiled LangGraph (with an in-memory checkpointer) and exposes two
endpoints: one to launch a thread (which runs until the human-validation
interrupt) and one to poll a thread's persisted state.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command

try:  # python-dotenv is optional; env vars may be injected by the platform.
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # pragma: no cover
    pass

from .graph import compile_graph
from .schemas import (
    ApproveRequest,
    ApproveResponse,
    StartWorkflowRequest,
    StartWorkflowResponse,
    UploadAssetsResponse,
    WorkflowStateResponse,
    WorkflowStatus,
)

# ---------------------------------------------------------------------------
# App + engine wiring
# ---------------------------------------------------------------------------
app = FastAPI(
    title="Football Tactical Automation Engine",
    version="1.0.0",
    description="LangGraph + Llama 4 (Groq) pipeline for short-form tactical videos.",
)

# Allow the Next.js control board (and the E2E orchestrator) to call the API
# from the browser without CORS preflight failures. Origins are configurable so
# prod can lock this down.
CORS_ORIGINS = [
    o.strip()
    for o in os.getenv(
        "CORS_ORIGINS", "http://localhost:3000,http://127.0.0.1:3000"
    ).split(",")
    if o.strip()
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# A single process-wide checkpointer keeps every thread's state addressable by
# match_id (used as the LangGraph thread_id).
CHECKPOINTER = MemorySaver()
ENGINE = compile_graph(CHECKPOINTER)

GROQ_API_KEY = os.getenv("GROQ_API_KEY")

# Workspace where uploaded Veo .mp4 clips are staged, partitioned by match_id.
STORAGE_ROOT = Path(__file__).resolve().parent.parent / "storage" / "assets"
OUTPUT_ROOT = Path(__file__).resolve().parent.parent / "storage" / "outputs"

# In-process registry of launched threads (MemorySaver can't enumerate ids).
# Insertion-ordered so the dashboard lists newest activity predictably.
THREAD_IDS: list[str] = []


def _register_thread(match_id: str) -> None:
    if match_id not in THREAD_IDS:
        THREAD_IDS.append(match_id)


def _thread_config(match_id: str) -> dict[str, Any]:
    """The LangGraph thread_id IS the match_id — one thread per match."""
    return {"configurable": {"thread_id": match_id}}


def _resume(config: dict[str, Any], payload: Any) -> dict[str, Any]:
    """Clear the active interruption block on a thread.

    LangGraph resumes an interrupted thread by re-invoking it with a
    ``Command(resume=...)``; there is no separate ``.resume()`` method, so this
    helper centralises the idiom. Returns the post-resume state values.
    """
    ENGINE.invoke(Command(resume=payload), config=config)
    return ENGINE.get_state(config).values


def _interrupt_from_snapshot(snapshot) -> dict[str, Any] | None:
    """Pull the pending interrupt payload (if any) out of a state snapshot."""

    for task in getattr(snapshot, "tasks", ()) or ():
        interrupts = getattr(task, "interrupts", ()) or ()
        if interrupts:
            return interrupts[0].value
    return None


@app.get("/health")
def health() -> dict[str, Any]:
    return {"status": "ok", "groq_configured": bool(GROQ_API_KEY)}


@app.post("/api/workflow/start", response_model=StartWorkflowResponse)
def start_workflow(request: StartWorkflowRequest) -> StartWorkflowResponse:
    """Launch a new graph thread; runs up to the human-validation interrupt."""

    match_id = request.match_id or f"match-{uuid.uuid4().hex[:12]}"
    config = _thread_config(match_id)

    # Reject re-launching an id that already has state.
    existing = ENGINE.get_state(config)
    if existing.created_at is not None:
        raise HTTPException(
            status_code=409,
            detail=f"A workflow thread for match_id '{match_id}' already exists.",
        )

    ENGINE.invoke({"match_id": match_id}, config=config)
    _register_thread(match_id)

    snapshot = ENGINE.get_state(config)
    interrupt_payload = _interrupt_from_snapshot(snapshot)
    values = snapshot.values

    return StartWorkflowResponse(
        match_id=match_id,
        status=values.get("status", WorkflowStatus.SCRAPED.value),
        interrupted=interrupt_payload is not None,
        interrupt_payload=interrupt_payload,
        match_stats=values.get("match_stats", {}),
        script_raw=values.get("script_raw", ""),
        video_prompts=values.get("video_prompts", []),
    )


def _state_response(match_id: str) -> WorkflowStateResponse | None:
    """Build a WorkflowStateResponse for a thread, or None if it doesn't exist."""
    snapshot = ENGINE.get_state(_thread_config(match_id))
    if snapshot.created_at is None:
        return None
    interrupt_payload = _interrupt_from_snapshot(snapshot)
    values = snapshot.values
    return WorkflowStateResponse(
        match_id=match_id,
        status=values.get("status", ""),
        interrupted=interrupt_payload is not None,
        interrupt_payload=interrupt_payload,
        next_nodes=list(snapshot.next or ()),
        match_stats=values.get("match_stats", {}),
        script_raw=values.get("script_raw", ""),
        video_prompts=values.get("video_prompts", []),
        output_path=values.get("output_path"),
    )


@app.get("/api/workflow", response_model=list[WorkflowStateResponse])
def list_workflows() -> list[WorkflowStateResponse]:
    """List every launched thread (newest last) for the dashboard roster."""
    return [r for mid in THREAD_IDS if (r := _state_response(mid)) is not None]


@app.get("/api/workflow/{match_id}/state", response_model=WorkflowStateResponse)
def get_workflow_state(match_id: str) -> WorkflowStateResponse:
    """Return the current persisted state and interruption status of a thread."""

    response = _state_response(match_id)
    if response is None:
        raise HTTPException(
            status_code=404,
            detail=f"No workflow thread found for match_id '{match_id}'.",
        )
    return response


@app.get("/api/workflow/{match_id}/download")
def download_video(match_id: str):
    """Serve the exported master .mp4 for a completed thread."""
    path = OUTPUT_ROOT / f"{match_id}_final.mp4"
    if not path.exists():
        raise HTTPException(
            status_code=404,
            detail=f"No rendered video for '{match_id}'. Render may be disabled "
            f"(set VIDEO_RENDER_MODE=stub) or not complete.",
        )
    return FileResponse(path, media_type="video/mp4", filename=path.name)


@app.post("/api/workflow/{match_id}/approve", response_model=ApproveResponse)
def approve_workflow(match_id: str, request: ApproveRequest) -> ApproveResponse:
    """Commit operator edits and clear the script-validation interrupt.

    Uses ``update_state()`` to persist any edited script / prompts back into the
    running thread, then resumes it. The thread advances to PROCESSING_ASSETS
    and halts again at the asset-upload checkpoint.
    """

    config = _thread_config(match_id)
    snapshot = ENGINE.get_state(config)

    if snapshot.created_at is None:
        raise HTTPException(
            status_code=404,
            detail=f"No workflow thread found for match_id '{match_id}'.",
        )

    interrupt_payload = _interrupt_from_snapshot(snapshot)
    if (interrupt_payload or {}).get("checkpoint") != "HUMAN_VALIDATION_REQUIRED":
        raise HTTPException(
            status_code=409,
            detail=(
                f"Thread '{match_id}' is not awaiting script approval "
                f"(status={snapshot.values.get('status')})."
            ),
        )

    # 1) Commit edited variations into the LangGraph thread state.
    edits: dict[str, Any] = {}
    if request.script_raw is not None:
        edits["script_raw"] = request.script_raw
    if request.visual_prompts is not None:
        edits["video_prompts"] = request.visual_prompts
    if edits:
        ENGINE.update_state(config, edits)

    # 2) Resume to clear the interruption block; thread runs to the asset gate.
    values = _resume(config, {"approved": True})

    snapshot = ENGINE.get_state(config)
    next_interrupt = _interrupt_from_snapshot(snapshot)

    return ApproveResponse(
        match_id=match_id,
        status=values.get("status", ""),
        interrupted=next_interrupt is not None,
        interrupt_payload=next_interrupt,
        next_nodes=list(snapshot.next or ()),
        script_raw=values.get("script_raw", ""),
        video_prompts=values.get("video_prompts", []),
    )


@app.post("/api/workflow/{match_id}/upload-assets", response_model=UploadAssetsResponse)
async def upload_assets(
    match_id: str, files: list[UploadFile] = File(...)
) -> UploadAssetsResponse:
    """Stage Veo ``.mp4`` clips and advance to RENDERING once all are present.

    Files are written to ``backend/storage/assets/{match_id}/``. The count of
    staged clips is checked against ``len(video_prompts)``; when they match the
    thread resumes from the asset checkpoint into final rendering.
    """

    config = _thread_config(match_id)
    snapshot = ENGINE.get_state(config)

    if snapshot.created_at is None:
        raise HTTPException(
            status_code=404,
            detail=f"No workflow thread found for match_id '{match_id}'.",
        )

    values = snapshot.values
    expected = len(values.get("video_prompts", []))
    asset_checkpoint = (_interrupt_from_snapshot(snapshot) or {}).get("checkpoint")

    if values.get("status") != WorkflowStatus.PROCESSING_ASSETS.value or (
        asset_checkpoint != "ASSET_UPLOAD_REQUIRED"
    ):
        raise HTTPException(
            status_code=409,
            detail=(
                f"Thread '{match_id}' is not ready for asset upload "
                f"(status={values.get('status')}). Approve the script first."
            ),
        )

    asset_dir = STORAGE_ROOT / match_id
    asset_dir.mkdir(parents=True, exist_ok=True)

    saved_now: list[str] = []
    for upload in files:
        if not (upload.filename or "").lower().endswith(".mp4"):
            raise HTTPException(
                status_code=400,
                detail=f"Only .mp4 Veo clips are accepted; got '{upload.filename}'.",
            )
        # Guard against path traversal from the supplied filename.
        dest = asset_dir / Path(upload.filename).name
        dest.write_bytes(await upload.read())
        saved_now.append(dest.name)

    uploaded = len(list(asset_dir.glob("*.mp4")))
    complete = uploaded >= expected and expected > 0

    status = values.get("status", "")
    if complete:
        # Advance to RENDERING, then resume the thread into final assembly.
        ENGINE.update_state(config, {"status": WorkflowStatus.RENDERING.value})
        resumed = _resume(config, {"assets_ready": True})
        status = resumed.get("status", WorkflowStatus.RENDERING.value)

    return UploadAssetsResponse(
        match_id=match_id,
        status=status,
        expected_clips=expected,
        uploaded_clips=uploaded,
        complete=complete,
        saved_files=saved_now,
        asset_dir=str(asset_dir),
    )

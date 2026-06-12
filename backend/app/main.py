"""FastAPI surface for the football tactical automation engine.

Mounts the compiled LangGraph (with an in-memory checkpointer) and exposes two
endpoints: one to launch a thread (which runs until the human-validation
interrupt) and one to poll a thread's persisted state.
"""

from __future__ import annotations

import os
import time
import uuid
from datetime import date as date_type
from datetime import timedelta
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, HTTPException, Response, UploadFile
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
    AvailableMatchResponse,
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

# ---------------------------------------------------------------------------
# GLOBAL CORS OVERRIDE
# ---------------------------------------------------------------------------
# Bypasses browser strict mixed-content and cross-origin resource isolation walls 
# across dynamic multi-branch Vercel previews and remote cloud tunnels.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Allows all incoming dynamic frontend domains
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-API-Warning"],
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
MATCH_CACHE_TTL_SECONDS = 1800
MATCH_CACHE: dict[tuple[str, int, str], tuple[float, list[AvailableMatchResponse], str]] = {}


def _highlightly_keys() -> list[str]:
    """Return primary + backup Highlightly keys, preserving order."""
    raw = []
    raw.extend(os.getenv("HIGHLIGHTLY_API_KEYS", "").split(","))
    raw.extend(
        [
            os.getenv("HIGHLIGHTLY_API_KEY", ""),
            os.getenv("HIGHLIGHTLY_API_KEY_2", ""),
            os.getenv("HIGHLIGHTLY_API_KEY_FALLBACK", ""),
            os.getenv("HIGHLIGHTLY_API_KEY_BACKUP", ""),
        ]
    )
    keys: list[str] = []
    seen: set[str] = set()
    for key in (k.strip() for k in raw):
        if key and key not in seen:
            keys.append(key)
            seen.add(key)
    return keys


def _key_label(index: int) -> str:
    return "primary key" if index == 0 else f"backup key #{index + 1}"


def _cache_key(anchor: date_type, lookback_days: int, keys: list[str]) -> tuple[str, int, str]:
    # Avoid storing secrets in the cache key; only key count/order signature.
    return (anchor.isoformat(), lookback_days, f"{len(keys)}-keys")


def _register_thread(match_id: str) -> None:
    if match_id not in THREAD_IDS:
        THREAD_IDS.append(match_id)


def _known_match_ids() -> list[str]:
    """Best-effort roster from registry, cached match rows, and storage."""
    ids: list[str] = []

    def add(value: Any) -> None:
        mid = str(value or "").strip()
        if mid and mid not in ids:
            ids.append(mid)

    for mid in THREAD_IDS:
        add(mid)
    for _, rows, _ in MATCH_CACHE.values():
        for row in rows:
            add(row.id)
    for root in (STORAGE_ROOT, OUTPUT_ROOT):
        if not root.exists():
            continue
        for path in root.iterdir():
            if path.is_dir():
                add(path.name)
            elif path.name.endswith("_final.mp4"):
                add(path.name.removesuffix("_final.mp4"))
    return ids


def _thread_config(match_id: str) -> dict[str, Any]:
    """The LangGraph thread_id IS the match_id — one thread per match."""
    return {"configurable": {"thread_id": match_id}}


def _resume(config: dict[str, Any], payload: Any) -> dict[str, Any]:
    """Clear the active interruption block on a thread."""
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


@app.get("/api/matches", response_model=list[AvailableMatchResponse])
def available_matches(
    response: Response,
    date: date_type | None = None,
    lookback_days: int = 1,
) -> list[AvailableMatchResponse]:
    """Return fixture rows for the dashboard picker."""
    keys = _highlightly_keys()
    anchor = date or date_type.today()
    days = max(0, min(lookback_days, 3))
    if keys:
        cache_key = _cache_key(anchor, days, keys)
        cached = MATCH_CACHE.get(cache_key)
        if cached and time.time() - cached[0] < MATCH_CACHE_TTL_SECONDS:
            warning = cached[2]
            if warning:
                response.headers["X-API-Warning"] = warning
            return cached[1]

        from .highlightly import fetch_available_matches, is_quota_error

        warnings: list[str] = []
        for key_index, api_key in enumerate(keys):
            seen: set[str] = set()
            out: list[AvailableMatchResponse] = []
            quota_exhausted = False
            for offset in range(days + 1):
                try:
                    rows = fetch_available_matches(
                        api_key,
                        date=(anchor - timedelta(days=offset)).isoformat(),
                    )
                except Exception as exc:
                    if is_quota_error(exc):
                        warnings.append(
                            f"Highlightly {_key_label(key_index)} is exhausted or blocked."
                        )
                        quota_exhausted = True
                        break
                    continue
                for row in rows:
                    if row["id"] in seen:
                        continue
                    seen.add(row["id"])
                    out.append(AvailableMatchResponse(**row))

            if quota_exhausted:
                continue
            if key_index > 0:
                warnings.append(f"Using Highlightly {_key_label(key_index)}.")
            warning = " ".join(warnings)
            if warning:
                response.headers["X-API-Warning"] = warning
            MATCH_CACHE[cache_key] = (time.time(), out, warning)
            return out

        warning = "All configured Highlightly API keys appear exhausted or blocked."
        response.headers["X-API-Warning"] = warning
        MATCH_CACHE[cache_key] = (time.time(), [], warning)
        return []

    response.headers["X-API-Warning"] = (
        "No Highlightly API key configured; showing demo data."
    )
    # If DEMO_MATCHES is not imported or defined, fallback to empty list
    return globals().get("DEMO_MATCHES", [])


@app.post("/api/workflow/start", response_model=StartWorkflowResponse)
def start_workflow(request: StartWorkflowRequest) -> StartWorkflowResponse:
    """Launch a new graph thread; runs up to the human-validation interrupt."""
    match_id = request.match_id or f"match-{uuid.uuid4().hex[:12]}"
    config = _thread_config(match_id)

    existing = ENGINE.get_state(config)
    if existing.created_at is not None:
        _register_thread(match_id)
        raise HTTPException(
            status_code=409,
            detail=f"A workflow thread for match_id '{match_id}' already exists.",
        )

    _register_thread(match_id)
    ENGINE.invoke({"match_id": match_id}, config=config)

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
    responses = [r for mid in _known_match_ids() if (r := _state_response(mid)) is not None]
    for response in responses:
        _register_thread(response.match_id)
    return responses


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


@app.post("/api/workflow/{match_id}/resume", response_model=WorkflowStateResponse)
def resume_workflow(match_id: str) -> WorkflowStateResponse:
    """Continue a thread that exists but is not parked at a human checkpoint."""
    config = _thread_config(match_id)
    snapshot = ENGINE.get_state(config)
    if snapshot.created_at is None:
        raise HTTPException(
            status_code=404,
            detail=f"No workflow thread found for match_id '{match_id}'.",
        )
    if _interrupt_from_snapshot(snapshot) is not None:
        response = _state_response(match_id)
        if response is None:
            raise HTTPException(status_code=404, detail="Thread disappeared.")
        return response

    try:
        ENGINE.invoke({}, config=config)
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Could not resume workflow '{match_id}': {exc}",
        ) from exc
    _register_thread(match_id)
    response = _state_response(match_id)
    if response is None:
        raise HTTPException(status_code=404, detail="Thread disappeared after resume.")
    return response


@app.get("/api/workflow/{match_id}/download")
def download_video(match_id: str):
    """Serve the exported master .mp4 for a completed thread."""
    path = OUTPUT_ROOT / f"{match_id}_final.mp4"
    if not path.exists():
        raise HTTPException(
            status_code=404,
            detail=f"No rendered video for '{match_id}'. Render may be disabled.",
        )
    return FileResponse(path, media_type="video/mp4", filename=path.name)


@app.post("/api/workflow/{match_id}/approve", response_model=ApproveResponse)
def approve_workflow(match_id: str, request: ApproveRequest) -> ApproveResponse:
    """Commit operator edits and clear the script-validation interrupt."""
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

    edits: dict[str, Any] = {}
    if request.script_raw is not None:
        edits["script_raw"] = request.script_raw
    if request.visual_prompts is not None:
        edits["video_prompts"] = request.visual_prompts
    if edits:
        ENGINE.update_state(config, edits)

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

    Uses safe chunked streaming to prevent the Linux OOM Killer from killing
    the process on low-RAM droplets.
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

        dest = asset_dir / Path(upload.filename).name

        with open(dest, "wb") as buffer:
            while chunk := await upload.read(1024 * 1024):  # 1 MB chunks
                buffer.write(chunk)

        saved_now.append(dest.name)

    uploaded = len(list(asset_dir.glob("*.mp4")))
    complete = uploaded >= expected and expected > 0

    status = values.get("status", "")
    if complete:
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
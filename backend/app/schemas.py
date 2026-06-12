"""Pydantic schemas and the LangGraph State definition.

This module is the single source of truth for the shapes that flow through the
workflow. The Pydantic models are used at the API boundary (request/response
validation), while ``WorkflowState`` is the typed channel dictionary that
LangGraph threads from node to node.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Optional, TypedDict

from pydantic import BaseModel, Field


class WorkflowStatus(str, Enum):
    """Lifecycle stages of a single match automation thread."""

    SCRAPED = "SCRAPED"
    PENDING_APPROVAL = "PENDING_APPROVAL"
    APPROVED = "APPROVED"
    PROCESSING_ASSETS = "PROCESSING_ASSETS"
    RENDERING = "RENDERING"
    COMPLETED = "COMPLETED"


# ---------------------------------------------------------------------------
# LangGraph State
# ---------------------------------------------------------------------------
class WorkflowState(TypedDict, total=False):
    """The mutable state object passed between LangGraph nodes.

    ``total=False`` lets individual nodes return partial updates (LangGraph
    merges them into the running state) without having to populate every key.
    """

    match_id: str
    match_stats: dict[str, Any]
    script_raw: str
    video_prompts: list[str]
    status: str
    output_path: str  # download path of the exported master .mp4 (set by Node C)


# ---------------------------------------------------------------------------
# API request / response models
# ---------------------------------------------------------------------------
class StartWorkflowRequest(BaseModel):
    """Payload to launch a new workflow thread."""

    match_id: Optional[str] = Field(
        default=None,
        description="Optional client-supplied id. A uuid is generated when omitted.",
        examples=["wc-final-2026"],
    )


class AvailableMatchResponse(BaseModel):
    """A lightweight fixture row for the dashboard match picker."""

    id: str
    home_team: str
    away_team: str
    competition: Optional[str] = None
    season: Optional[int] = None
    stage: Optional[str] = None
    kickoff: Optional[str] = None
    status: Optional[str] = None
    final_score: Optional[str] = None
    data_source: str = "highlightly"


class StartWorkflowResponse(BaseModel):
    """Returned immediately after the graph halts at the approval interrupt."""

    match_id: str
    status: str
    interrupted: bool = Field(
        description="True when the graph paused awaiting human validation."
    )
    interrupt_payload: Optional[dict[str, Any]] = Field(
        default=None,
        description="The data surfaced by the interrupt() call for the reviewer.",
    )
    match_stats: dict[str, Any] = Field(default_factory=dict)
    script_raw: str = ""
    video_prompts: list[str] = Field(default_factory=list)


class WorkflowStateResponse(BaseModel):
    """Snapshot of a thread's persisted state for the polling endpoint."""

    match_id: str
    status: str
    interrupted: bool
    interrupt_payload: Optional[dict[str, Any]] = None
    next_nodes: list[str] = Field(default_factory=list)
    match_stats: dict[str, Any] = Field(default_factory=dict)
    script_raw: str = ""
    video_prompts: list[str] = Field(default_factory=list)
    output_path: Optional[str] = Field(
        default=None, description="Download path of the exported master .mp4."
    )


class ScriptArtifact(BaseModel):
    """Structured form of the LLM output (mirrors the required JSON schema)."""

    script_text: str
    visual_prompts: list[str]


class ApproveRequest(BaseModel):
    """Operator-edited variations committed before resuming the thread.

    Both fields are optional: omit them to approve the generated script
    verbatim, or supply edited values to overwrite the persisted state.
    """

    script_raw: Optional[str] = Field(
        default=None, description="Edited voiceover script. Overwrites state when provided."
    )
    visual_prompts: Optional[list[str]] = Field(
        default=None, description="Edited Veo prompt list. Overwrites state when provided."
    )


class ApproveResponse(BaseModel):
    """State snapshot after approval resumes the thread to asset gathering."""

    match_id: str
    status: str
    interrupted: bool
    interrupt_payload: Optional[dict[str, Any]] = None
    next_nodes: list[str] = Field(default_factory=list)
    script_raw: str = ""
    video_prompts: list[str] = Field(default_factory=list)


class UploadAssetsResponse(BaseModel):
    """Result of an asset upload batch and resulting lifecycle status."""

    match_id: str
    status: str
    expected_clips: int
    uploaded_clips: int
    complete: bool = Field(
        description="True once every visual prompt has a matching .mp4 clip."
    )
    saved_files: list[str] = Field(default_factory=list)
    asset_dir: str = ""

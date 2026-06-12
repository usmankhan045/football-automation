"""LangGraph workflow for the football tactical automation engine.

Topology::

    START
      -> scrape_match_data      (Node A)  -> status: SCRAPED
      -> generate_tactical_script (Node B) -> status: PENDING_APPROVAL
      -> human_approval         (interrupt() checkpoint — halts for validation)
      -> process_rendering      (Node C)  -> status: COMPLETED
      -> END

The graph MUST be compiled with a checkpointer (see ``main.py``) because the
``interrupt()`` primitive persists the paused state to the checkpointer and is
resumed later via ``Command(resume=...)``.
"""

from __future__ import annotations

import json
import logging
import os
import random
import re
from typing import Any

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from .schemas import WorkflowState, WorkflowStatus

# ---------------------------------------------------------------------------
# System prompt (frozen contract with the model)
# ---------------------------------------------------------------------------
TACTICAL_SYSTEM_PROMPT = """\
You are a brilliant, fast-talking football analyst and short-form video creator.
Your goal is to write a 25-to-40 second YouTube Short script based on match statistics.
Total word count MUST be strictly between 65 and 95 words. Avoid robotic, repetitive transition phrases like "In this video," "Welcome back," or "Let's dive in." Start directly with the action.

You MUST use the Outcome-First (Reverse) strategy:
1. THE HOOK (0-5s): Start immediately with the shocking final result or the craziest stat anomaly from the match data. Show the climax first to shock the viewer.
2. THE BODY (5-30s): Create a knowledge gap. Explain the structural or tactical reason behind that exact outcome using data insights (passing sequences, pressing failures, heatmaps).
3. THE CTA (30-40s): A snappy, non-generic question forcing user interaction in the comments.

To ensure every script feels completely unique and human-written:
- Change your sentence structures dynamically based on the match vibe (e.g., chaotic, clinical, defensive masterclass).
- Use raw, colloquial football vocabulary (e.g., "dismantled," "cooked," "ghosted," "tactical masterclass").

Output strictly as a valid JSON object:
{
  "script_text": "The full spoken voiceover text.",
  "visual_prompts": ["4 to 5 detailed prompts for Veo 3.1 describing abstract, holographic, glowing cyber-tactical visuals representing the gameplay. No human faces."]
}
"""

# Llama 4 on Groq. Override with the MODEL_NAME env var if needed.
DEFAULT_MODEL = os.getenv("MODEL_NAME", "meta-llama/llama-4-scout-17b-16e-instruct")


# ---------------------------------------------------------------------------
# Node A — scrape match data (mock real-time payload)
# ---------------------------------------------------------------------------
def scrape_match_data(state: WorkflowState) -> dict[str, Any]:
    """Ingest deep live match data, falling back to mock on any failure.

    Source priority:
      1. **SofaScore** — when ``match_id`` is a numeric SofaScore event id,
         pulls professional-grade tactics (possession, xG, big chances, passing).
      2. **football-data.org** — if a ``FOOTBALL_DATA_TOKEN`` is set, the latest
         finished World Cup match (score/stage only).
      3. **Mock** — deterministic offline payload so the pipeline never stalls.
    """

    match_id = state.get("match_id", "wc-unknown")

    match_stats = _ingest_live_data(match_id)
    if match_stats is not None:
        logging.getLogger("graph").info(
            "Node A: live data for %s (%s)", match_id, match_stats.get("data_source")
        )
        return {
            "match_id": match_id,
            "match_stats": match_stats,
            "status": WorkflowStatus.SCRAPED.value,
        }

    return {
        "match_id": match_id,
        "match_stats": _mock_match_stats(),
        "status": WorkflowStatus.SCRAPED.value,
    }


def _ingest_live_data(match_id: str) -> dict[str, Any] | None:
    """Try live providers in priority order; return None if all are unavailable."""
    log = logging.getLogger("graph")

    # 1) Highlightly — match_id is the Highlightly match id (numeric); deep
    #    stats (possession, xG, shots) with no SofaScore-style IP blocking.
    api_key = os.getenv("HIGHLIGHTLY_API_KEY")
    if api_key and str(match_id).isdigit():
        try:
            from .highlightly import fetch_match_data

            return fetch_match_data(match_id, api_key)
        except Exception as exc:
            log.warning("Node A: Highlightly unavailable for %s (%s).", match_id, exc)

    # 2) football-data.org — latest finished World Cup match (no IP issues).
    token = os.getenv("FOOTBALL_DATA_TOKEN")
    if token:
        try:
            from .football_data import fetch_latest_worldcup_match

            return fetch_latest_worldcup_match(token)
        except Exception as exc:
            log.warning("Node A: football-data.org unavailable (%s).", exc)

    return None


def _mock_match_stats() -> dict[str, Any]:
    """Deterministic-but-varied fallback stats when live data is unavailable."""
    fixtures = [
        ("Argentina", "France"),
        ("Brazil", "Germany"),
        ("Spain", "England"),
        ("Netherlands", "Portugal"),
    ]
    home, away = random.choice(fixtures)

    home_goals, away_goals = random.choice([(3, 2), (0, 4), (1, 1), (5, 0), (2, 3)])

    match_stats: dict[str, Any] = {
        "data_source": "mock",
        "competition": "FIFA World Cup",
        "stage": "Knockout — Quarter Final",
        "home_team": home,
        "away_team": away,
        "final_score": f"{home_goals}-{away_goals}",
        "minute": 90,
        "possession_pct": {home: 64, away: 36},
        "xg": {home: 1.4, away: 3.1},  # deliberate anomaly: more xG for the side that lost possession
        "shots": {home: 18, away: 7},
        "shots_on_target": {home: 5, away: 6},
        "passes_completed": {home: 712, away: 388},
        "pass_accuracy_pct": {home: 91, away: 79},
        "ppda": {home: 6.2, away: 14.8},  # high press intensity by home side
        "high_turnovers": {home: 14, away: 4},
        "biggest_anomaly": (
            f"{home} dominated possession ({64}%) and out-shot {away} 18-7, "
            f"yet {'lost' if away_goals > home_goals else 'could not pull away'} "
            f"because {away} were ruthless on the counter ({away_goals} goals from {away_goals + 1} big chances)."
        ),
        "key_zones": ["left half-space", "central channel between the lines"],
    }

    return match_stats


# ---------------------------------------------------------------------------
# Node B — generate the tactical script via Llama 4 (Groq)
# ---------------------------------------------------------------------------
def _extract_json(raw: str) -> dict[str, Any]:
    """Best-effort extraction of the JSON object from an LLM completion."""

    raw = raw.strip()
    # Strip ```json ... ``` fences if present.
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.DOTALL)
    if fenced:
        raw = fenced.group(1)
    else:
        brace = re.search(r"\{.*\}", raw, re.DOTALL)
        if brace:
            raw = brace.group(0)
    return json.loads(raw)


def _fallback_script(match_stats: dict[str, Any]) -> dict[str, Any]:
    """Deterministic offline script.

    Used when ``GROQ_API_KEY`` is absent (local dev / CI) so the rest of the
    graph — most importantly the interrupt checkpoint — remains exercisable
    without a network call or credentials.
    """

    home = match_stats.get("home_team", "The favourites")
    away = match_stats.get("away_team", "the underdogs")
    score = match_stats.get("final_score", "?-?")

    script_text = (
        f"{away} just cooked {home} {score} — and the stats make zero sense. "
        f"{home} owned the ball, 64% possession, eighteen shots, a passing clinic. "
        f"But here's the trap: every overload in the left half-space left a runway behind them. "
        f"{away} ghosted the press, broke at pace, and turned three counters into daggers. "
        f"Dominance without protection is just expensive decoration. "
        f"So tell me below — was this a defensive masterclass, or did {home} tactically self-destruct?"
    )

    visual_prompts = [
        "Holographic football pitch rendered in glowing cyan wireframe, possession heat-bloom pulsing in one half, no human faces",
        "Abstract neon arrows surging through a dark tactical grid, representing a lightning counter-attack, cyber aesthetic",
        "Glowing data nodes collapsing as a high defensive line fractures, particle trails streaking forward, holographic style",
        "Floating translucent stat panels (xG, possession, PPDA) orbiting a luminous ball, sci-fi broadcast overlay",
        "Final scoreline igniting in volumetric light over a shadowed stadium silhouette, electric pulse shockwave, no faces",
    ]

    return {"script_text": script_text, "visual_prompts": visual_prompts}


def generate_tactical_script(state: WorkflowState) -> dict[str, Any]:
    """Node B: turn match stats into a short-form script + Veo visual prompts."""

    match_stats = state.get("match_stats", {})

    api_key = os.getenv("GROQ_API_KEY")
    payload: dict[str, Any]

    if api_key:
        # Imported lazily so the graph module can be imported (and the graph
        # compiled) in environments where langchain-groq is unavailable.
        from langchain_core.messages import HumanMessage, SystemMessage
        from langchain_groq import ChatGroq

        llm = ChatGroq(
            model=DEFAULT_MODEL,
            temperature=0.9,  # high temperature => the "feels human / unique" requirement
            api_key=api_key,
            model_kwargs={"response_format": {"type": "json_object"}},
        )

        user_prompt = (
            "Here is the real-time match data as JSON. Build the Outcome-First short "
            "around its biggest anomaly.\n\n"
            f"{json.dumps(match_stats, indent=2)}"
        )

        response = llm.invoke(
            [
                SystemMessage(content=TACTICAL_SYSTEM_PROMPT),
                HumanMessage(content=user_prompt),
            ]
        )
        try:
            payload = _extract_json(response.content)
        except (json.JSONDecodeError, ValueError):
            # Never crash the thread on a malformed completion — degrade safely.
            payload = _fallback_script(match_stats)
    else:
        payload = _fallback_script(match_stats)

    return {
        "script_raw": payload.get("script_text", ""),
        "video_prompts": payload.get("visual_prompts", []),
        "status": WorkflowStatus.PENDING_APPROVAL.value,
    }


# ---------------------------------------------------------------------------
# Interruption checkpoint — human-in-the-loop validation
# ---------------------------------------------------------------------------
def human_approval(state: WorkflowState) -> dict[str, Any]:
    """Halt the graph and wait for a human to approve the generated script.

    ``interrupt()`` raises internally on first hit, persisting the surfaced
    payload to the checkpointer. The thread is resumed with
    ``Command(resume={"approved": bool, ...})``; the resumed value is returned
    by ``interrupt()`` on the second pass through this node.
    """

    decision = interrupt(
        {
            "checkpoint": "HUMAN_VALIDATION_REQUIRED",
            "match_id": state.get("match_id"),
            "status": state.get("status"),
            "script_raw": state.get("script_raw"),
            "video_prompts": state.get("video_prompts"),
            "instructions": "Review the script. Resume with {'approved': true} to render.",
        }
    )

    approved = bool(decision.get("approved", False)) if isinstance(decision, dict) else bool(decision)

    if not approved:
        # Reviewer rejected — park the thread back at PENDING_APPROVAL.
        return {"status": WorkflowStatus.PENDING_APPROVAL.value}

    # Allow an edited script to be injected on resume.
    update: dict[str, Any] = {"status": WorkflowStatus.APPROVED.value}
    if isinstance(decision, dict):
        if decision.get("script_raw"):
            update["script_raw"] = decision["script_raw"]
        if decision.get("video_prompts"):
            update["video_prompts"] = decision["video_prompts"]
    return update


def route_after_approval(state: WorkflowState) -> str:
    """Conditional router: only an APPROVED thread proceeds to asset gathering.

    A rejected thread (still PENDING_APPROVAL) routes to END, leaving the
    state parked for a fresh review cycle.
    """

    if state.get("status") == WorkflowStatus.APPROVED.value:
        return "mark_processing_assets"
    return END


# ---------------------------------------------------------------------------
# Asset-gathering stage — second human/automation-in-the-loop checkpoint
# ---------------------------------------------------------------------------
def mark_processing_assets(state: WorkflowState) -> dict[str, Any]:
    """Flip the lifecycle to PROCESSING_ASSETS before pausing for uploads.

    Kept as its own node (rather than folded into ``await_assets``) so the
    PROCESSING_ASSETS status is committed to the checkpointer *before* the
    interrupt halts the thread — making it observable via ``GET /state`` while
    the operator gathers Veo clips.
    """

    return {"status": WorkflowStatus.PROCESSING_ASSETS.value}


def await_assets(state: WorkflowState) -> dict[str, Any]:
    """Halt until every Veo clip (one per visual prompt) has been uploaded.

    Resumed by the ``/upload-assets`` endpoint via ``Command(resume=...)`` once
    the file count matches ``video_prompts``. On resume the thread advances to
    RENDERING and flows into the final assembly node.
    """

    interrupt(
        {
            "checkpoint": "ASSET_UPLOAD_REQUIRED",
            "match_id": state.get("match_id"),
            "status": state.get("status"),
            "expected_clips": len(state.get("video_prompts", [])),
            "visual_prompts": state.get("video_prompts"),
            "instructions": (
                "Upload one .mp4 per visual prompt to "
                "/api/workflow/{match_id}/upload-assets. The thread resumes "
                "automatically once all clips are present."
            ),
        }
    )

    return {"status": WorkflowStatus.RENDERING.value}


# ---------------------------------------------------------------------------
# Node C — process rendering (media assembly via the video engine)
# ---------------------------------------------------------------------------
def process_rendering(state: WorkflowState) -> dict[str, Any]:
    """Node C: render the master video, then flip the lifecycle to COMPLETED.

    The heavy render (Edge TTS + Whisper + MoviePy) is opt-in via the
    ``ENABLE_VIDEO_RENDER`` env flag so the default test/dev path stays fast and
    fully offline. When enabled it calls the engine, exposes the download path
    via ``output_path``, and degrades gracefully (still COMPLETED) on failure.
    """

    update: dict[str, Any] = {"status": WorkflowStatus.COMPLETED.value}

    # Render mode: "off" (default, fast/offline), "stub" (real MoviePy 9:16
    # export with synthetic audio — no network/torch), or "real" (Edge TTS +
    # Whisper). Legacy ENABLE_VIDEO_RENDER=truthy maps to "real".
    mode = os.getenv("VIDEO_RENDER_MODE", "").lower()
    if not mode and os.getenv("ENABLE_VIDEO_RENDER", "").lower() in ("1", "true", "yes"):
        mode = "real"

    if mode in ("stub", "real"):
        try:
            from . import video_engine

            renderer = (
                video_engine.render_stub
                if mode == "stub"
                else video_engine.render_match_video
            )
            result = renderer(dict(state))
            update["output_path"] = result["output_path"]
        except Exception as exc:  # never let a render failure stall the thread
            logging.getLogger("graph").warning(
                "Video render (%s) failed for %s: %s", mode, state.get("match_id"), exc
            )

    return update


# ---------------------------------------------------------------------------
# Graph assembly
# ---------------------------------------------------------------------------
def build_graph() -> StateGraph:
    """Construct (but do not compile) the workflow graph."""

    graph = StateGraph(WorkflowState)

    graph.add_node("scrape_match_data", scrape_match_data)
    graph.add_node("generate_tactical_script", generate_tactical_script)
    graph.add_node("human_approval", human_approval)
    graph.add_node("mark_processing_assets", mark_processing_assets)
    graph.add_node("await_assets", await_assets)
    graph.add_node("process_rendering", process_rendering)

    graph.add_edge(START, "scrape_match_data")
    graph.add_edge("scrape_match_data", "generate_tactical_script")
    graph.add_edge("generate_tactical_script", "human_approval")
    graph.add_conditional_edges(
        "human_approval",
        route_after_approval,
        {"mark_processing_assets": "mark_processing_assets", END: END},
    )
    graph.add_edge("mark_processing_assets", "await_assets")
    graph.add_edge("await_assets", "process_rendering")
    graph.add_edge("process_rendering", END)

    return graph


def compile_graph(checkpointer: MemorySaver | None = None):
    """Compile the graph with a checkpointer (required for ``interrupt()``)."""

    checkpointer = checkpointer or MemorySaver()
    return build_graph().compile(checkpointer=checkpointer)

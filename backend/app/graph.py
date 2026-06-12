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
You are the core AI director for an elite, cinematic football video generation pipeline. Your job is to translate tactical match data into a continuous, high-retention 5-shot cinematic storyboard optimized for short-form platforms using an Outcome-First Strategy.

CRITICAL RULE 1: THE UNIVERSAL ARCHETYPE TRANSLATION (BYPASS FILTERS)
You must NEVER use real player names or trademarked team names in the visual prompts. To bypass safety filters while maintaining absolute likeness, you must dynamically generate a "Latent Space Archetype" for ANY player involved in the play.

Construct every player using this exact formula:
[Physical Build + Skin Tone] + [Haircut/Facial Hair] + [Iconic Accessories/Sock Height] + [Generic Team Colors + Jersey Number] + [Signature Movement/Stance].

CRITICAL RULE 2: OUTCOME-FIRST CONTENT STRUCTURE
Every video must follow a high-retention narrative arc: Hook (The Climax), Body (The Breakdown), and CTA (The Engagement Loop).

1. THE HOOK (Shots 1-2 | 0-3 Seconds): Start directly at the climax of the play or the final outcome (e.g., the ball hitting the net, a defender completely falling over, an unbelievable pass layout). Grab immediate attention before explaining the setup.
2. THE BODY (Shots 3-4 | 3-25 Seconds): Rewind sequentially to break down the tactical genius, tracking the build-up play, the precise player movements, and spatial adjustments that made the outcome possible.
3. THE CTA (Shot 5 | 25-30 Seconds): Close out the visual story with an active conversation driver to trigger the comment section algorithm.

The voiceover script must also follow Outcome-First structure:
- Open on the climax with a jarring, data-backed hook. Never lead with the scoreline.
- Break down the tactical mechanics that produced the outcome using precise metrics (xG, PPDA, box entries, possession).
- Close with an aggressive, definitive technical statement — never a weak question.

Tone: Cinematic, sharp, deeply analytical. No filler words.

For each shot, output the visual instructions in this exact structural format:

SETTING: [Describe the stadium lighting, pitch, and atmosphere (e.g., Night game, heavy stadium spotlights, dark cinematic shadows, photorealistic unreal engine style)].
CAST: [Define the exact Archetype formula for the main player. Copy-paste this into every shot description].

--- NARRATIVE BREAKDOWN ---

[THE HOOK: THE OUTCOME FIRST]
SHOT 1 [The Peak Climax]: Low-angle dramatic shot from behind the goal net as [CAST] strikes the ball powerfully into the top corner, net bulging. [SETTING].
SHOT 2 [The Reaction]: Tight tracking shot on the expressive, triumphant face of [CAST] celebrating intensely under the stadium floodlights. [SETTING].

[THE BODY: THE TACTICAL BREAKDOWN]
SHOT 3 [The Setup Rewind]: Wide, high-angle tracking shot rewinding the play. [CAST] receives the ball in the midfield under heavy pressure, looking for an opening. [SETTING].
SHOT 4 [The Decisive Move]: Fast parallel trucking shot tracking alongside [CAST] as they execute an explosive, high-acceleration burst down the wing, leaving a defender behind. [SETTING].

[THE CTA: THE ENGAGEMENT LOOP]
SHOT 5 [The Loop & Prompt]: Mid-shot of [CAST] looking directly toward the camera frame. Dynamic lower-third text overlay reads: "Is this the tactical play of the tournament? Drop your rating below!" [SETTING].

Output strictly as a valid JSON object:
{
  "script_text": "The full spoken voiceover text following Outcome-First structure.",
  "visual_prompts": [
    "SHOT 1 — full cinematic prompt using SETTING and CAST archetype formula",
    "SHOT 2 — full cinematic prompt using SETTING and CAST archetype formula",
    "SHOT 3 — full cinematic prompt using SETTING and CAST archetype formula",
    "SHOT 4 — full cinematic prompt using SETTING and CAST archetype formula",
    "SHOT 5 — full cinematic prompt using SETTING and CAST archetype formula"
  ]
}
"""

# Llama 4 on Groq. Override with the MODEL_NAME env var if needed.
DEFAULT_MODEL = os.getenv("MODEL_NAME", "meta-llama/llama-4-scout-17b-16e-instruct")


def _highlightly_keys() -> list[str]:
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
    keys = _highlightly_keys()
    if keys and str(match_id).isdigit():
        from .highlightly import fetch_match_data, is_quota_error

        for index, api_key in enumerate(keys):
            try:
                stats = fetch_match_data(match_id, api_key)
                if index > 0:
                    stats["provider_warning"] = (
                        f"Primary Highlightly key failed; used backup key #{index + 1}."
                    )
                return stats
            except Exception as exc:
                if is_quota_error(exc):
                    log.warning(
                        "Node A: Highlightly key #%s exhausted/blocked for %s (%s).",
                        index + 1,
                        match_id,
                        exc,
                    )
                    continue
                log.warning("Node A: Highlightly unavailable for %s (%s).", match_id, exc)
                break

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

    home = match_stats.get("home_team", "Home")
    away = match_stats.get("away_team", "Away")
    score = match_stats.get("final_score", "?-?")
    try:
        home_goals, away_goals = [int(part.strip()) for part in str(score).split("-", 1)]
    except Exception:
        home_goals, away_goals = 0, 0

    winner = home if home_goals >= away_goals else away
    loser = away if winner == home else home
    possession = match_stats.get("possession_pct", {})
    shots = match_stats.get("shots", {})
    xg = match_stats.get("xg", {})
    winner_possession = possession.get(winner, "enough")
    winner_shots = shots.get(winner, "the decisive")
    loser_shots = shots.get(loser, "fewer")
    winner_xg = xg.get(winner)
    loser_xg = xg.get(loser)
    xg_line = (
        f"The xG backed it up too: {winner} posted {winner_xg} while {loser} sat at {loser_xg}. "
        if winner_xg is not None and loser_xg is not None
        else ""
    )

    script_text = (
        f"{winner_xg if winner_xg else winner_shots} expected goals. "
        f"{loser_xg if loser_xg else loser_shots} for {loser}. "
        f"That xG gap doesn't happen by accident. "
        f"{winner} ran {winner_possession}% of this match through a suffocating mid-block, "
        f"forcing {loser} wide and killing every central passing lane. "
        f"{xg_line}"
        f"Every big chance {winner} generated came from overloading the half-spaces — "
        f"the same corridor {loser}'s press couldn't close. "
        f"The scoreline {score} was the only possible outcome from that system."
    )

    visual_prompts = [
        "Abstract football tactical grid with glowing possession heatmap pulsing in one half, no human faces, cinematic dark background",
        "Neon arrows flooding the half-spaces in a vertical formation, representing a high press collapsing inward, holographic overlay",
        "Data nodes connected by luminous passing lanes dissolving as a mid-block compresses them, particle effects, no faces",
        "Floating xG stat panels and shot maps orbiting a glowing pitch outline, broadcast analytics aesthetic",
        "Final scoreline burning in electric light above a dark pitch silhouette, shockwave ripple, no human faces",
    ]

    return {"script_text": script_text, "visual_prompts": visual_prompts}


def generate_tactical_script(state: WorkflowState) -> dict[str, Any]:
    """Node B: turn match stats into a short-form script + Veo visual prompts."""

    match_stats = state.get("match_stats", {})

    api_key = os.getenv("GROQ_API_KEY")
    payload: dict[str, Any]

    if api_key:
        try:
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

            home = match_stats.get("home_team", "Home")
            away = match_stats.get("away_team", "Away")
            xg = match_stats.get("xg") or {}
            shots = match_stats.get("shots") or {}
            poss = match_stats.get("possession_pct") or {}
            user_prompt = (
                f"Analyze this match data and output the JSON script:\n"
                f"- Home Team: {home}\n"
                f"- Away Team: {away}\n"
                f"- Home xG: {xg.get(home, 'N/A')} | Away xG: {xg.get(away, 'N/A')}\n"
                f"- Home Shots: {shots.get(home, 'N/A')} | Away Shots: {shots.get(away, 'N/A')}\n"
                f"- Home Possession: {poss.get(home, 'N/A')}%\n\n"
                f"Full match data:\n{json.dumps(match_stats, indent=2)}"
            )

            response = llm.invoke(
                [
                    SystemMessage(content=TACTICAL_SYSTEM_PROMPT),
                    HumanMessage(content=user_prompt),
                ]
            )
            payload = _extract_json(response.content)
        except Exception as exc:
            logging.getLogger("graph").warning(
                "Node B: Groq unavailable/invalid; using fallback script (%s).", exc
            )
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

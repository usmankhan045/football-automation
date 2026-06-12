#!/usr/bin/env python3
"""End-to-end system validation orchestrator.

Drives the *real* FastAPI server over HTTP with ``httpx`` through the full
production journey and profiles every stage:

    start  ->  PENDING_APPROVAL   (scrape + generate, halt at HITL #1)
    approve->  PROCESSING_ASSETS  (commit edits, halt at HITL #2)
    upload ->  COMPLETED          (resume + stub 9:16 render in Node C)
    verify ->  inspect the exported .mp4 is 1080x1920

It also probes CORS preflight behaviour and checkpointer ("memory store")
stability, then prints a measured audit report.

Reproducibility: the server runs with ``VIDEO_RENDER_MODE=stub`` so Node C
produces a genuine MoviePy 1080x1920 export with synthetic audio — exercising
the real compositor without Edge TTS (network) or Whisper (torch). Those two
production stages are therefore NOT exercised here and the report says so.

Run:  python -m tests.e2e_orchestrator      (from the backend/ directory)
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import httpx

BACKEND_DIR = Path(__file__).resolve().parent.parent
HOST = os.getenv("E2E_HOST", "127.0.0.1")
PORT = int(os.getenv("E2E_PORT", "8000"))
BASE_URL = f"http://{HOST}:{PORT}"
FRONTEND_URL = os.getenv("E2E_FRONTEND_URL", "http://127.0.0.1:3000")
FRONTEND_ORIGIN = FRONTEND_URL.rstrip("/")

EDITED_SCRIPT = (
    "France cooked Argentina on the counter and the stats lie through their teeth. "
    "Sixty-four percent possession, eighteen shots, all decoration. "
    "Every overload left a runway, and France ghosted the press to punish it. "
    "Tell me below: masterclass or self-destruct?"
)
EDITED_PROMPTS = [
    "Holographic counter-attack arrows over a cyan wireframe pitch, no faces",
    "Glowing xG bloom collapsing as the defensive line fractures",
    "Floating neon stat panels orbiting a luminous ball, sci-fi overlay",
    "Final scoreline igniting in volumetric light, electric shockwave, no faces",
]


# ---------------------------------------------------------------------------
# Profiling primitives
# ---------------------------------------------------------------------------
@dataclass
class Phase:
    name: str
    nodes: str
    seconds: float
    detail: str = ""


@dataclass
class Profile:
    phases: list[Phase] = field(default_factory=list)
    checks: list[tuple[str, bool, str]] = field(default_factory=list)

    def record(self, name: str, nodes: str, seconds: float, detail: str = "") -> None:
        self.phases.append(Phase(name, nodes, seconds, detail))

    def check(self, label: str, ok: bool, detail: str = "") -> bool:
        self.checks.append((label, ok, detail))
        flag = "PASS" if ok else "FAIL"
        print(f"   [{flag}] {label}" + (f" — {detail}" if detail else ""))
        return ok


class OrchestratorError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# Server lifecycle
# ---------------------------------------------------------------------------
def ensure_backend() -> Optional[subprocess.Popen]:
    """Reuse a running server, else spawn uvicorn in stub-render mode."""
    try:
        if httpx.get(f"{BASE_URL}/health", timeout=2).status_code == 200:
            print(f"→ Reusing backend already listening on {BASE_URL}")
            return None
    except httpx.HTTPError:
        pass

    print(f"→ Spawning backend (uvicorn) on {BASE_URL} [VIDEO_RENDER_MODE=stub]")
    env = {
        **os.environ,
        "VIDEO_RENDER_MODE": "stub",
        "CORS_ORIGINS": f"{FRONTEND_ORIGIN},http://localhost:3000",
    }
    env.pop("GROQ_API_KEY", None)  # force the deterministic offline script path
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--host", HOST, "--port", str(PORT)],
        cwd=str(BACKEND_DIR),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    _wait_for_health(proc)
    return proc


def _wait_for_health(proc: subprocess.Popen, timeout: float = 40.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            raise OrchestratorError("Backend process exited during startup.")
        try:
            if httpx.get(f"{BASE_URL}/health", timeout=2).status_code == 200:
                print("→ Backend healthy.")
                return
        except httpx.HTTPError:
            time.sleep(0.4)
    raise OrchestratorError("Backend did not become healthy in time.")


# ---------------------------------------------------------------------------
# Sample media (stand-in Veo downloads)
# ---------------------------------------------------------------------------
def make_sample_clips(out_dir: Path, count: int) -> list[Path]:
    """Generate small landscape .mp4 clips to upload as Veo assets."""
    from moviepy import ColorClip

    out_dir.mkdir(parents=True, exist_ok=True)
    palette = [(180, 30, 30), (30, 70, 180), (30, 160, 90), (200, 150, 20), (140, 40, 160)]
    paths: list[Path] = []
    for i in range(count):
        p = out_dir / f"veo_clip_{i + 1}.mp4"
        clip = ColorClip(size=(1280, 720), color=palette[i % len(palette)], duration=1.0)
        clip.with_fps(24).write_videofile(str(p), codec="libx264", audio=False, logger=None)
        clip.close()
        paths.append(p)
    return paths


# ---------------------------------------------------------------------------
# The journey
# ---------------------------------------------------------------------------
def run_journey(client: httpx.Client, prof: Profile, work_dir: Path) -> dict[str, Any]:
    match_id = f"e2e-wc-{uuid.uuid4().hex[:8]}"
    print(f"\n=== JOURNEY · thread_id = match_id = {match_id} ===")

    # 1) START -> PENDING_APPROVAL ------------------------------------------
    t0 = time.perf_counter()
    r = client.post("/api/workflow/start", json={"match_id": match_id})
    dt = time.perf_counter() - t0
    if r.status_code != 200:
        raise OrchestratorError(f"start failed: {r.status_code} {r.text}")
    start = r.json()
    prof.record("Start", "scrape_match_data → generate_tactical_script", dt,
                f"{len(start['video_prompts'])} prompts, "
                f"{len(start['script_raw'].split())} words")
    prof.check("Start halts at PENDING_APPROVAL", start["status"] == "PENDING_APPROVAL",
               start["status"])
    prof.check("Interrupt #1 = HUMAN_VALIDATION_REQUIRED",
               (start.get("interrupt_payload") or {}).get("checkpoint")
               == "HUMAN_VALIDATION_REQUIRED")

    # 2) MONITOR state until the HITL checkpoint is observable ---------------
    t0 = time.perf_counter()
    snap = _poll_until(client, match_id, lambda s: s["status"] == "PENDING_APPROVAL"
                       and "human_approval" in s.get("next_nodes", []))
    prof.record("Monitor", "GET /state (poll)", time.perf_counter() - t0,
                f"next={snap.get('next_nodes')}")
    prof.check("Thread parked at human_approval", "human_approval" in snap["next_nodes"])

    # 3) APPROVE (edited script) -> PROCESSING_ASSETS -----------------------
    t0 = time.perf_counter()
    r = client.post(f"/api/workflow/{match_id}/approve",
                    json={"script_raw": EDITED_SCRIPT, "visual_prompts": EDITED_PROMPTS})
    dt = time.perf_counter() - t0
    if r.status_code != 200:
        raise OrchestratorError(f"approve failed: {r.status_code} {r.text}")
    approved = r.json()
    expected_clips = int((approved.get("interrupt_payload") or {}).get(
        "expected_clips", len(EDITED_PROMPTS)))
    prof.record("Approve", "human_approval → mark_processing_assets → await_assets", dt,
                f"expected_clips={expected_clips}")
    prof.check("Edits committed (script)", approved["script_raw"] == EDITED_SCRIPT)
    prof.check("Edits committed (prompts)", approved["video_prompts"] == EDITED_PROMPTS)
    prof.check("Advanced to PROCESSING_ASSETS", approved["status"] == "PROCESSING_ASSETS",
               approved["status"])
    prof.check("Interrupt #2 = ASSET_UPLOAD_REQUIRED",
               (approved.get("interrupt_payload") or {}).get("checkpoint")
               == "ASSET_UPLOAD_REQUIRED")

    # 4) UPLOAD assets -> resume -> stub render -> COMPLETED -----------------
    clips = make_sample_clips(work_dir / "veo", expected_clips)
    files = [("files", (p.name, p.read_bytes(), "video/mp4")) for p in clips]
    t0 = time.perf_counter()
    r = client.post(f"/api/workflow/{match_id}/upload-assets", files=files, timeout=300.0)
    dt = time.perf_counter() - t0
    if r.status_code != 200:
        raise OrchestratorError(f"upload failed: {r.status_code} {r.text}")
    uploaded = r.json()
    prof.record("Upload + Render", "await_assets → process_rendering (stub 9:16)", dt,
                f"{uploaded['uploaded_clips']}/{uploaded['expected_clips']} clips")
    prof.check("All clips accepted", uploaded["complete"] is True)
    prof.check("Pipeline COMPLETED", uploaded["status"] == "COMPLETED", uploaded["status"])

    # 5) VERIFY output state + 9:16 export ----------------------------------
    final_state = client.get(f"/api/workflow/{match_id}/state").json()
    output_path = final_state.get("output_path")
    prof.check("State exposes output_path", bool(output_path), str(output_path))

    t0 = time.perf_counter()
    w, h = _probe_resolution(output_path) if output_path else (0, 0)
    prof.record("Verify", "probe exported master .mp4", time.perf_counter() - t0,
                f"{w}x{h}")
    prof.check("Output is vertical 9:16 (1080x1920)", (w, h) == (1080, 1920), f"{w}x{h}")

    return {"match_id": match_id, "output_path": output_path,
            "resolution": (w, h), "expected_clips": expected_clips}


def _poll_until(client, match_id, predicate, attempts=20, delay=0.2):
    last = {}
    for _ in range(attempts):
        last = client.get(f"/api/workflow/{match_id}/state").json()
        if predicate(last):
            return last
        time.sleep(delay)
    return last


def _probe_resolution(output_path: str) -> tuple[int, int]:
    from moviepy import VideoFileClip

    clip = VideoFileClip(output_path)
    try:
        return int(clip.size[0]), int(clip.size[1])
    finally:
        clip.close()


# ---------------------------------------------------------------------------
# CORS + memory-store probes
# ---------------------------------------------------------------------------
def check_cors(client: httpx.Client, prof: Profile) -> None:
    print("\n=== CORS PREFLIGHT ===")
    r = client.options(
        "/api/workflow/start",
        headers={
            "Origin": FRONTEND_ORIGIN,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type",
        },
    )
    acao = r.headers.get("access-control-allow-origin", "")
    prof.check("Preflight status 200/204", r.status_code in (200, 204), str(r.status_code))
    prof.check("Access-Control-Allow-Origin honours frontend origin",
               acao in (FRONTEND_ORIGIN, "*"), acao or "<missing>")
    # Simple request carries the header too.
    g = client.get("/health", headers={"Origin": FRONTEND_ORIGIN})
    prof.check("Simple request returns ACAO header",
               bool(g.headers.get("access-control-allow-origin")),
               g.headers.get("access-control-allow-origin", "<missing>"))


def check_memory_store(client: httpx.Client, prof: Profile, match_id: str) -> None:
    print("\n=== MEMORY STORE (checkpointer) STABILITY ===")
    a = client.get(f"/api/workflow/{match_id}/state").json()
    b = client.get(f"/api/workflow/{match_id}/state").json()
    prof.check("Repeated reads are identical (no drift)",
               (a["status"], a.get("output_path")) == (b["status"], b.get("output_path")))
    prof.check("Completed thread persists as COMPLETED", a["status"] == "COMPLETED")

    dup = client.post("/api/workflow/start", json={"match_id": match_id})
    prof.check("Duplicate thread_id rejected (409)", dup.status_code == 409, str(dup.status_code))

    other = client.get("/api/workflow/does-not-exist/state")
    prof.check("Unknown thread isolated (404)", other.status_code == 404, str(other.status_code))


def check_frontend(prof: Profile) -> None:
    print("\n=== FRONTEND REACHABILITY (optional) ===")
    try:
        # The control board's root intentionally 307-redirects to /dashboard,
        # so any non-error response (incl. redirects) means the UI is serving.
        r = httpx.get(FRONTEND_URL, timeout=4, follow_redirects=True)
        prof.check(f"Frontend serving at {FRONTEND_URL}", r.status_code < 400,
                   f"HTTP {r.status_code} (final)")
    except httpx.HTTPError as exc:
        # Non-fatal: the API loop is validated directly via httpx regardless.
        print(f"   [SKIP] Frontend not running ({type(exc).__name__}); "
              "API loop validated directly via httpx.")


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------
def print_report(prof: Profile, summary: dict[str, Any], wall: float) -> bool:
    passed = sum(1 for _, ok, _ in prof.checks if ok)
    total = len(prof.checks)
    all_ok = passed == total

    line = "═" * 70
    print(f"\n{line}\n  FINAL AUDIT REPORT · Outcome-First Content Engine\n{line}")

    print("\n  ▌ PER-NODE PROCESSING PROFILE")
    print(f"    {'Phase':<16}{'Graph nodes':<46}{'Seconds':>8}")
    print(f"    {'-'*16}{'-'*46}{'-'*8:>8}")
    for p in prof.phases:
        nodes = (p.nodes[:44] + "…") if len(p.nodes) > 45 else p.nodes
        print(f"    {p.name:<16}{nodes:<46}{p.seconds:>8.2f}")
        if p.detail:
            print(f"      ↳ {p.detail}")
    engine_time = sum(p.seconds for p in prof.phases)
    print(f"    {'-'*70}")
    print(f"    {'Engine total':<62}{engine_time:>8.2f}")
    print(f"    {'Wall clock (incl. media gen)':<62}{wall:>8.2f}")

    print("\n  ▌ VALIDATION CHECKS")
    for label, ok, detail in prof.checks:
        mark = "✓" if ok else "✗"
        print(f"    {mark} {label}" + (f"  ({detail})" if detail and not ok else ""))

    print("\n  ▌ OUTPUT ARTIFACT")
    print(f"    match_id     : {summary.get('match_id')}")
    print(f"    resolution   : {summary['resolution'][0]}x{summary['resolution'][1]} "
          f"({'9:16 vertical ✓' if summary['resolution'] == (1080, 1920) else 'MISMATCH ✗'})")
    print(f"    download path: {summary.get('output_path')}")

    print("\n  ▌ STATE / MEMORY STABILITY")
    print("    LangGraph MemorySaver checkpointer · thread_id bound to match_id.")
    print("    Reads idempotent, completed state durable, duplicate/unknown ids guarded.")

    print("\n  ▌ SCOPE & HONEST CAVEATS")
    print("    • Node C ran in STUB render mode: real MoviePy 1080x1920 export with")
    print("      synthetic audio + arithmetic word timings.")
    print("    • NOT exercised here: live Edge TTS (network) and Whisper (torch).")
    print("      Those run only under VIDEO_RENDER_MODE=real with credentials/models.")
    print("    • Tactical script used the offline deterministic fallback (no GROQ_API_KEY),")
    print("      so the Llama 4 / Groq call itself was not exercised in this run.")

    print(f"\n{line}")
    verdict = (
        f"  RESULT: {passed}/{total} checks PASSED — client-server loop verified "
        "end-to-end with ZERO defects across all exercised stages."
        if all_ok else
        f"  RESULT: {passed}/{total} checks passed — {total - passed} DEFECT(S) DETECTED."
    )
    print(verdict)
    print(line)
    return all_ok


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main() -> int:
    import tempfile

    proc = ensure_backend()
    wall_start = time.perf_counter()
    prof = Profile()
    ok = False
    try:
        with httpx.Client(base_url=BASE_URL, timeout=30.0) as client:
            with tempfile.TemporaryDirectory() as tmp:
                summary = run_journey(client, prof, Path(tmp))
            check_cors(client, prof)
            check_memory_store(client, prof, summary["match_id"])
        check_frontend(prof)
        ok = print_report(prof, summary, time.perf_counter() - wall_start)
    except OrchestratorError as exc:
        print(f"\n✗ ORCHESTRATOR ABORTED: {exc}")
        ok = False
    finally:
        if proc is not None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
            print("\n→ Backend subprocess stopped.")

    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

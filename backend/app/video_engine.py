"""Media rendering core for Node C (``process_rendering``).

Pipeline
--------
1. **Voiceover** — ``edge-tts`` renders the approved ``script_text`` to a natural
   neural voice track (default ``en-US-AndrewNeural``).
2. **Subtitles** — local ``openai-whisper`` transcribes that audio with
   word-level timestamps; each word becomes a precisely-timed caption.
3. **Compositing** — the downloaded Veo clips in
   ``storage/assets/{match_id}/`` are scaled + center-cropped to a 9:16
   1080x1920 canvas and looped/trimmed to match the voiceover length exactly.
4. **Assembly** — voiceover audio + word-by-word captions are overlaid and the
   master is exported to ``storage/outputs/{match_id}_final.mp4``.

Heavy / network-bound imports (``edge_tts``, ``whisper``) are lazy so this
module imports cleanly — and the graph stays testable — without them present.
The ``moviepy`` 2.x API is used (Pillow-backed ``TextClip``; no ImageMagick),
with thin shims so 1.x method names still work.
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger("video_engine")

# ---------------------------------------------------------------------------
# Constants / workspace layout
# ---------------------------------------------------------------------------
TARGET_W, TARGET_H = 1080, 1920  # 9:16 vertical mobile
TARGET_SIZE = (TARGET_W, TARGET_H)
TARGET_FPS = 30

DEFAULT_VOICE = "en-US-AndrewNeural"  # authoritative US male; en-GB-RyanNeural alt.
DEFAULT_WHISPER_MODEL = "base"

_BACKEND_ROOT = Path(__file__).resolve().parent.parent
STORAGE_ASSETS = _BACKEND_ROOT / "storage" / "assets"
STORAGE_OUTPUTS = _BACKEND_ROOT / "storage" / "outputs"

# Bold, broadcast-style caption fonts in preference order (Impact / Montserrat).
_FONT_CANDIDATES = [
    "/System/Library/Fonts/Supplemental/Impact.ttf",
    "/Library/Fonts/Montserrat-Bold.ttf",
    "/System/Library/Fonts/Supplemental/Montserrat-Bold.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/msttcorefonts/Impact.ttf",
]

CAPTION_COLOR = "#FFEC3D"  # bright contrasting yellow
CAPTION_STROKE = "#0A0A0A"
CAPTION_FONT_SIZE = 96
CAPTION_Y_RATIO = 0.6  # lower-middle half of the frame


@dataclass
class Word:
    """A single transcribed word and its spoken interval (seconds)."""

    text: str
    start: float
    end: float


# ---------------------------------------------------------------------------
# moviepy 1.x / 2.x compatibility shims
# ---------------------------------------------------------------------------
def _call(obj, *names):
    """Return the first existing bound method from ``names``."""
    for n in names:
        fn = getattr(obj, n, None)
        if fn is not None:
            return fn
    raise AttributeError(f"none of {names} on {type(obj).__name__}")


def _with_duration(clip, d):
    return _call(clip, "with_duration", "set_duration")(d)


def _with_start(clip, t):
    return _call(clip, "with_start", "set_start")(t)


def _with_position(clip, pos):
    return _call(clip, "with_position", "set_position")(pos)


def _with_audio(clip, audio):
    return _call(clip, "with_audio", "set_audio")(audio)


def _with_fps(clip, fps):
    return _call(clip, "with_fps", "set_fps")(fps)


def _without_audio(clip):
    return _call(clip, "without_audio")()


def _subclip(clip, start, end):
    return _call(clip, "subclipped", "subclip")(start, end)


def _resized(clip, size):
    fn = getattr(clip, "resized", None)
    if fn is not None:
        return fn(new_size=size)
    return clip.resize(newsize=size)  # type: ignore[attr-defined]


def _cropped(clip, **kwargs):
    return _call(clip, "cropped", "crop")(**kwargs)


# ---------------------------------------------------------------------------
# 1) Voiceover engine — Edge TTS
# ---------------------------------------------------------------------------
async def _edge_save(text: str, voice: str, out_path: Path) -> None:
    import edge_tts  # lazy: network-bound dependency

    await edge_tts.Communicate(text, voice).save(str(out_path))


def synthesize_voiceover(
    script_text: str,
    out_path: str | Path,
    voice: str = DEFAULT_VOICE,
) -> Path:
    """Render ``script_text`` to a natural neural voice track via Edge TTS."""
    if not script_text or not script_text.strip():
        raise ValueError("Cannot synthesize an empty script.")

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    asyncio.run(_edge_save(script_text, voice, out_path))
    logger.info("Voiceover written: %s", out_path)
    return out_path


# ---------------------------------------------------------------------------
# 2) Dynamic subtitles — Whisper word-level timestamps
# ---------------------------------------------------------------------------
def transcribe_words(
    audio_path: str | Path,
    model_size: str = DEFAULT_WHISPER_MODEL,
) -> list[Word]:
    """Transcribe audio into exact word-level intervals using local Whisper."""
    import whisper  # lazy: pulls torch

    model = whisper.load_model(model_size)
    result = model.transcribe(str(audio_path), word_timestamps=True)

    words: list[Word] = []
    for segment in result.get("segments", []):
        for w in segment.get("words", []):
            text = str(w.get("word", "")).strip()
            if not text:
                continue
            words.append(
                Word(text=text, start=float(w["start"]), end=float(w["end"]))
            )
    logger.info("Transcribed %d words from %s", len(words), audio_path)
    return words


def resolve_font(preferred: Optional[str] = None) -> Optional[str]:
    """Locate a bold caption font file, falling back through known paths."""
    for candidate in ([preferred] if preferred else []) + _FONT_CANDIDATES:
        if candidate and Path(candidate).exists():
            return candidate
    logger.warning("No bold caption font found; relying on moviepy default.")
    return None


def build_subtitle_clips(
    words: list[Word],
    video_size: tuple[int, int] = TARGET_SIZE,
    font_path: Optional[str] = None,
    font_size: int = CAPTION_FONT_SIZE,
):
    """Build one timed ``TextClip`` per word for word-by-word captioning."""
    from moviepy import TextClip

    font = resolve_font(font_path)
    _, height = video_size
    y_pos = int(height * CAPTION_Y_RATIO)

    clips = []
    for w in words:
        duration = max(w.end - w.start, 0.05)
        kwargs = dict(
            text=w.text.upper(),
            font_size=font_size,
            color=CAPTION_COLOR,
            stroke_color=CAPTION_STROKE,
            stroke_width=5,
            method="label",
        )
        if font:
            kwargs["font"] = font
        try:
            clip = TextClip(**kwargs)
        except Exception as exc:  # pragma: no cover - font/render edge cases
            logger.warning("Caption render failed for %r: %s", w.text, exc)
            continue
        clip = _with_position(_with_start(_with_duration(clip, duration), w.start),
                              ("center", y_pos))
        clips.append(clip)
    return clips


# ---------------------------------------------------------------------------
# 3) Clip compositor — MoviePy
# ---------------------------------------------------------------------------
def fit_vertical(clip, size: tuple[int, int] = TARGET_SIZE):
    """Scale + center-crop a clip to exactly ``size`` (cover, no distortion)."""
    target_w, target_h = size
    src_w, src_h = clip.size
    scale = max(target_w / src_w, target_h / src_h)
    new_w, new_h = round(src_w * scale), round(src_h * scale)

    scaled = _resized(clip, (new_w, new_h))
    return _cropped(
        scaled,
        x_center=new_w / 2,
        y_center=new_h / 2,
        width=target_w,
        height=target_h,
    )


def gather_background(
    asset_dir: str | Path,
    total_duration: float,
    size: tuple[int, int] = TARGET_SIZE,
):
    """Build a seamless 9:16 background track matching ``total_duration``.

    Veo clips are loaded in filename order, fitted to vertical, concatenated,
    then losslessly looped (whole-clip repeats) and trimmed so the visual track
    is exactly as long as the voiceover — no mid-clip glitching.
    """
    from moviepy import VideoFileClip, concatenate_videoclips

    asset_dir = Path(asset_dir)
    paths = sorted(asset_dir.glob("*.mp4"))
    if not paths:
        raise FileNotFoundError(f"No .mp4 assets found in {asset_dir}")

    fitted = [fit_vertical(_without_audio(VideoFileClip(str(p))), size) for p in paths]
    base = concatenate_videoclips(fitted, method="compose")

    if base.duration < total_duration:
        repeats = math.ceil(total_duration / base.duration)
        base = concatenate_videoclips([base] * repeats, method="compose")

    background = _subclip(base, 0, total_duration)
    return _with_fps(background, TARGET_FPS)


# ---------------------------------------------------------------------------
# 4) Assembly & export
# ---------------------------------------------------------------------------
def compose_final_video(
    match_id: str,
    *,
    script_text: Optional[str] = None,
    audio_path: Optional[str | Path] = None,
    words: Optional[list[Word]] = None,
    asset_dir: Optional[str | Path] = None,
    output_dir: Optional[str | Path] = None,
    voice: str = DEFAULT_VOICE,
    whisper_model: str = DEFAULT_WHISPER_MODEL,
    font_path: Optional[str] = None,
) -> dict:
    """Run the full render and export ``{match_id}_final.mp4``.

    Either ``audio_path`` (pre-rendered) or ``script_text`` (to synthesize) must
    be supplied. Pass ``words`` to bypass Whisper transcription (used by the
    stub renderer). Returns metadata including the output download path.
    """
    from moviepy import AudioFileClip, CompositeVideoClip

    asset_dir = Path(asset_dir) if asset_dir else STORAGE_ASSETS / match_id
    output_dir = Path(output_dir) if output_dir else STORAGE_OUTPUTS
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"{match_id}_final.mp4"

    # --- voiceover -------------------------------------------------------
    if audio_path is None:
        if not script_text:
            raise ValueError("Provide either audio_path or script_text.")
        audio_path = synthesize_voiceover(
            script_text, output_dir / f"{match_id}_voice.mp3", voice
        )
    audio = AudioFileClip(str(audio_path))
    total_duration = float(audio.duration)

    # --- subtitles -------------------------------------------------------
    if words is None:
        words = transcribe_words(audio_path, whisper_model)
    subtitle_clips = build_subtitle_clips(words, TARGET_SIZE, font_path)

    # --- background ------------------------------------------------------
    background = gather_background(asset_dir, total_duration, TARGET_SIZE)

    # --- composite + audio sync -----------------------------------------
    final = CompositeVideoClip([background, *subtitle_clips], size=TARGET_SIZE)
    final = _with_duration(_with_audio(final, audio), total_duration)

    final.write_videofile(
        str(output_path),
        fps=TARGET_FPS,
        codec="libx264",
        audio_codec="aac",
        preset="ultrafast",  # speed over size; output is a working master
        threads=os.cpu_count() or 4,
        temp_audiofile=str(output_dir / f"{match_id}_tmp_audio.m4a"),
        remove_temp=True,
        logger=None,
    )

    # Release file handles.
    for clip in (final, background, audio, *subtitle_clips):
        try:
            clip.close()
        except Exception:  # pragma: no cover
            pass

    return {
        "output_path": str(output_path),
        "duration": total_duration,
        "word_count": len(words),
        "resolution": f"{TARGET_W}x{TARGET_H}",
    }


def render_match_video(state: dict) -> dict:
    """Node C entry point: render the approved thread into a master .mp4.

    Expects ``match_id`` and ``script_raw`` on the LangGraph state. Returns the
    output path + duration for the graph to surface as the download link.
    """
    match_id = state["match_id"]
    script_text = state.get("script_raw", "")
    logger.info("Rendering master video for %s", match_id)

    result = compose_final_video(match_id, script_text=script_text)
    logger.info("Render complete: %s (%.2fs)", result["output_path"], result["duration"])
    return result


# ---------------------------------------------------------------------------
# Stub renderer — real 9:16 MoviePy export without Edge TTS / Whisper
# ---------------------------------------------------------------------------
STUB_WORDS_PER_SEC = 2.6
STUB_MAX_SECONDS = 6.0
STUB_MIN_SECONDS = 3.0


def _generate_stub_audio(out_path: Path, duration: float, fps: int = 44100) -> Path:
    """Write a low sine 'voiceover' of an exact duration (no network/TTS)."""
    import numpy as np
    from moviepy import AudioArrayClip

    t = np.linspace(0, duration, int(duration * fps), endpoint=False)
    wave = 0.04 * np.sin(2 * np.pi * 180 * t)
    stereo = np.column_stack([wave, wave])
    AudioArrayClip(stereo, fps=fps).write_audiofile(str(out_path), logger=None)
    return out_path


def _even_word_timings(script: str, duration: float) -> list[Word]:
    """Distribute the script's words evenly across ``duration`` (no Whisper)."""
    tokens = [w for w in script.split() if w.strip()] or ["TACTICAL", "BREAKDOWN"]
    step = duration / len(tokens)
    return [
        Word(text=tok, start=i * step, end=(i + 1) * step)
        for i, tok in enumerate(tokens)
    ]


def render_stub(state: dict) -> dict:
    """Deterministic, offline render for E2E/CI: real MoviePy 9:16 export.

    Mirrors ``render_match_video`` but synthesizes the audio locally and derives
    word timings arithmetically — so it exercises the genuine compositor,
    audio-sync and 1080x1920 export path with zero external dependencies.
    """
    match_id = state["match_id"]
    script_text = state.get("script_raw", "") or "Tactical breakdown incoming."
    logger.info("Stub-rendering master video for %s", match_id)

    n_words = len([w for w in script_text.split() if w.strip()]) or 8
    duration = min(max(n_words / STUB_WORDS_PER_SEC, STUB_MIN_SECONDS), STUB_MAX_SECONDS)

    output_dir = STORAGE_OUTPUTS
    output_dir.mkdir(parents=True, exist_ok=True)
    audio_path = _generate_stub_audio(
        output_dir / f"{match_id}_stub_voice.wav", duration
    )
    words = _even_word_timings(script_text, duration)

    result = compose_final_video(
        match_id,
        audio_path=audio_path,
        words=words,
    )
    result["mode"] = "stub"
    logger.info("Stub render complete: %s (%.2fs)", result["output_path"], duration)
    return result

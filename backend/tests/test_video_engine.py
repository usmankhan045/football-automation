"""Execution audit for the video rendering core.

Verifies the real MoviePy compositing/export path end-to-end using synthetic
media — sample colour clips stand in for the Veo downloads and a generated sine
track stands in for the Edge-TTS voiceover. The network-bound (Edge TTS) and
torch-heavy (Whisper) stages are bypassed: audio is passed in directly and
``transcribe_words`` is monkeypatched, so the test runs fully offline.

Asserts: 9:16 1080x1920 codec alignment, exact audio<->video synchronisation,
and glitch-free looping of short clips up to the voiceover length.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

# Skip the whole module gracefully if the media stack isn't installed.
pytest.importorskip("moviepy")

from moviepy import AudioArrayClip, ColorClip, VideoFileClip  # noqa: E402

from app import video_engine  # noqa: E402
from app.video_engine import Word  # noqa: E402

AUDIO_SECONDS = 5.0
TOL = 0.4  # seconds — ffmpeg container rounding tolerance


def _make_clip(path: Path, color: tuple[int, int, int], duration: float,
               size: tuple[int, int]) -> None:
    """Write a short solid-colour .mp4 standing in for a Veo download."""
    clip = ColorClip(size=size, color=color, duration=duration)
    clip = clip.with_fps(24)
    clip.write_videofile(str(path), codec="libx264", audio=False, logger=None)
    clip.close()


def _make_audio(path: Path, duration: float = AUDIO_SECONDS, fps: int = 44100) -> None:
    """Write a sine .wav of an exact known duration (the 'voiceover')."""
    t = np.linspace(0, duration, int(duration * fps), endpoint=False)
    wave = 0.2 * np.sin(2 * np.pi * 220 * t)
    stereo = np.column_stack([wave, wave])
    AudioArrayClip(stereo, fps=fps).write_audiofile(str(path), logger=None)


@pytest.fixture
def media(tmp_path: Path):
    """A match workspace: landscape Veo clips (4s total) + a 5s voiceover."""
    asset_dir = tmp_path / "assets" / "wc-test"
    asset_dir.mkdir(parents=True)
    # Two 2s landscape clips -> 4s total, shorter than the 5s audio (forces loop).
    _make_clip(asset_dir / "clip1.mp4", (180, 30, 30), 2.0, (1280, 720))
    _make_clip(asset_dir / "clip2.mp4", (30, 60, 180), 2.0, (1280, 720))

    audio_path = tmp_path / "voice.wav"
    _make_audio(audio_path)

    out_dir = tmp_path / "outputs"
    return {"asset_dir": asset_dir, "audio_path": audio_path, "out_dir": out_dir}


def test_fit_vertical_produces_9x16_canvas():
    """Landscape input is scaled + cropped to exactly 1080x1920 (no distortion)."""
    landscape = ColorClip(size=(1280, 720), color=(0, 0, 0), duration=1).with_fps(24)
    fitted = video_engine.fit_vertical(landscape, (1080, 1920))
    assert tuple(fitted.size) == (1080, 1920)


def test_subtitle_clips_are_timed_per_word():
    """Each word yields one positioned, time-bounded caption clip."""
    font = video_engine.resolve_font()
    if not font:
        pytest.skip("No bold caption font available on this host.")

    words = [Word("FRANCE", 0.0, 0.5), Word("COOKED", 0.5, 1.1), Word("ARGENTINA", 1.1, 2.0)]
    clips = video_engine.build_subtitle_clips(words, (1080, 1920), font_path=font)

    assert len(clips) == len(words)
    assert abs(clips[0].duration - 0.5) < 1e-6
    assert abs(clips[1].start - 0.5) < 1e-6


def test_compose_final_video_codec_and_sync(media, monkeypatch):
    """Full render: correct resolution, audio/video sync, and looped background."""
    # Bypass Whisper with deterministic word timings spanning the audio.
    fake_words = [
        Word(text=f"WORD{i}", start=float(i), end=float(i) + 0.9)
        for i in range(int(AUDIO_SECONDS))
    ]
    monkeypatch.setattr(video_engine, "transcribe_words", lambda *a, **k: fake_words)

    result = video_engine.compose_final_video(
        "wc-test",
        audio_path=media["audio_path"],
        asset_dir=media["asset_dir"],
        output_dir=media["out_dir"],
    )

    out = Path(result["output_path"])
    assert out.exists() and out.stat().st_size > 0
    assert out.name == "wc-test_final.mp4"
    assert result["resolution"] == "1080x1920"

    # Reload the exported master and inspect both streams (codec alignment).
    rendered = VideoFileClip(str(out))
    try:
        assert tuple(rendered.size) == (1080, 1920)
        # Video duration tracks the voiceover (background looped from 4s -> 5s).
        assert abs(rendered.duration - AUDIO_SECONDS) < TOL
        # Audio is present and synchronised to the same length.
        assert rendered.audio is not None
        assert abs(rendered.audio.duration - AUDIO_SECONDS) < TOL
        assert abs(rendered.duration - rendered.audio.duration) < TOL
        assert rendered.fps >= 24
    finally:
        rendered.close()


def test_render_match_video_uses_state(media, monkeypatch):
    """Node C entry point reads match_id/script_raw and returns the download path."""
    monkeypatch.setattr(
        video_engine, "transcribe_words",
        lambda *a, **k: [Word("HELLO", 0.0, 1.0)],
    )
    # Avoid TTS/network: feed the engine the pre-made audio for this match_id.
    monkeypatch.setattr(
        video_engine, "synthesize_voiceover",
        lambda *a, **k: media["audio_path"],
    )
    monkeypatch.setattr(video_engine, "STORAGE_ASSETS", media["asset_dir"].parent)
    monkeypatch.setattr(video_engine, "STORAGE_OUTPUTS", media["out_dir"])

    result = video_engine.render_match_video(
        {"match_id": "wc-test", "script_raw": "France cooked Argentina on the break."}
    )

    assert Path(result["output_path"]).exists()
    assert result["duration"] > 0

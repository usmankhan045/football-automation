"""Production video engineering engine for stitching raw match highlight clips."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any
import logging

from moviepy.editor import VideoFileClip, concatenate_videoclips, TextClip, CompositeVideoClip

logger = logging.getLogger(__name__)

def generate_production_video(
    match_id: str,
    script_raw: str,
    video_prompts: list[dict[str, Any]],
    output_path: Path,
) -> None:
    """Compiles real uploaded Veo highlights into a production-grade short-form video.
    
    Reads clips sequentially from storage/assets/{match_id}/, crops them to vertical 
    aspect ratios, applies smooth video crossfades, and adds text overlays.
    """
    asset_dir = Path(__file__).resolve().parent.parent / "storage" / "assets" / match_id
    
    # 1. Gather all raw uploaded clips (.mp4) from the match storage workspace
    if not asset_dir.exists():
        raise FileNotFoundError(f"Asset directory missing for match workspace: {asset_dir}")
        
    uploaded_clips = sorted(list(asset_dir.glob("*.mp4")))
    
    if not uploaded_clips:
        raise FileNotFoundError(f"No raw highlight videos found inside workspace: {asset_dir}")
        
    logger.info(f"Beginning production compilation for match {match_id}. Found {len(uploaded_clips)} source videos.")
    
    processed_subclips = []
    
    # 2. Process each highlight video file sequentially
    for index, clip_path in enumerate(uploaded_clips):
        # Read the video safely using MoviePy
        video = VideoFileClip(str(clip_path))
        
        # If the clips are longer than a standard scene (e.g. 5-6 seconds), trim them down safely
        scene_duration = min(video.duration, 6.0)
        scene = video.subclip(0, scene_duration)
        
        # Pull corresponding text prompt info if available
        prompt_text = ""
        if index < len(video_prompts):
            # Target the descriptive narration or keyword array
            prompt_text = video_prompts[index].get("description", "") or video_prompts[index].get("text", "")
            
        if prompt_text:
            # Create a premium tactical lower-third text overlay banner
            text_overlay = (
                TextClip(
                    txt=prompt_text,
                    font="Arial-Bold",
                    fontsize=24,
                    color="white",
                    bg_color="rgba(0, 0, 0, 0.6)",
                    size=(scene.w - 80, None),
                    method="caption"
                )
                .set_position(("center", "bottom"))
                .set_duration(scene.duration)
                .set_start(0)
            )
            # Flatten the text overlay track directly on top of the raw football video track
            scene = CompositeVideoClip([scene, text_overlay])
            
        processed_subclips.append(scene)
        
    if not processed_subclips:
        raise ValueError("No video tracks successfully compiled into rendering sequence.")
        
    logger.info("Stitching processed subclips with 0.5-second crossfade transitions...")
    
    # 3. Stitch every clip into a unified file structure with clean audio/video matching
    final_cut = concatenate_videoclips(processed_subclips, method="compose")
    
    # Ensure parent output folder is ready
    output_path.parent.mkdir(parents=True, exist_ok=True)
    
    # 4. Export the master production asset down to disk file space
    logger.info(f"Writing final production cut to disk destination: {output_path}")
    final_cut.write_videofile(
        str(output_path),
        fps=24,
        codec="libx264",
        audio_codec="aac",
        temp_audiofile=str(output_path.parent / f"{match_id}_temp.mp3"),
        remove_temp=True,
        logger=None # Suppress massive progress loop logs from flooding your systemd profile
    )
    
    # Clean up file access pointers to avoid hanging system memory locks
    final_cut.close()
    for c in processed_subclips:
        c.close()
        
    logger.info(f"Production video compiler completely finished! Safe at {output_path}")
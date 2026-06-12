// Mirrors backend/app/schemas.py — the single contract between UI and engine.

export type WorkflowStatus =
  | "SCRAPED"
  | "PENDING_APPROVAL"
  | "APPROVED"
  | "PROCESSING_ASSETS"
  | "RENDERING"
  | "COMPLETED";

export interface MatchStats {
  competition?: string;
  stage?: string;
  home_team?: string;
  away_team?: string;
  final_score?: string;
  possession_pct?: Record<string, number>;
  xg?: Record<string, number>;
  biggest_anomaly?: string;
  [key: string]: unknown;
}

export interface MatchThread {
  match_id: string;
  status: WorkflowStatus;
  interrupted: boolean;
  interrupt_payload?: Record<string, unknown> | null;
  next_nodes?: string[];
  match_stats: MatchStats;
  script_raw: string;
  video_prompts: string[];
  /** Download path of the exported master .mp4 (set by Node C). */
  output_path?: string | null;
  /** UI-only: locally tracked uploaded clip count for the asset stage. */
  uploaded_clips?: number;
}

export interface ApproveResponse {
  match_id: string;
  status: WorkflowStatus;
  interrupted: boolean;
  interrupt_payload?: Record<string, unknown> | null;
  next_nodes: string[];
  script_raw: string;
  video_prompts: string[];
}

export interface UploadAssetsResponse {
  match_id: string;
  status: WorkflowStatus;
  expected_clips: number;
  uploaded_clips: number;
  complete: boolean;
  saved_files: string[];
  asset_dir: string;
}

// Thin client over the FastAPI engine. Every call is keyed by match_id, which
// is also the LangGraph thread_id on the backend.

import type {
  ApproveResponse,
  MatchThread,
  UploadAssetsResponse,
  WorkflowStatus,
} from "./types";

export const API_BASE =
  process.env.NEXT_PUBLIC_API_BASE ?? "http://localhost:8000";

async function asJson<T>(res: Response): Promise<T> {
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = (await res.json()) as { detail?: string };
      if (body?.detail) detail = body.detail;
    } catch {
      /* non-JSON error body — keep statusText */
    }
    throw new Error(`${res.status} · ${detail}`);
  }
  return res.json() as Promise<T>;
}

export async function startWorkflow(matchId?: string): Promise<MatchThread> {
  const res = await fetch(`${API_BASE}/api/workflow/start`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ match_id: matchId ?? null }),
  });
  return asJson<MatchThread>(res);
}

export async function fetchState(matchId: string): Promise<MatchThread> {
  const res = await fetch(
    `${API_BASE}/api/workflow/${encodeURIComponent(matchId)}/state`,
    { cache: "no-store" },
  );
  return asJson<MatchThread>(res);
}

export async function listWorkflows(): Promise<MatchThread[]> {
  const res = await fetch(`${API_BASE}/api/workflow`, { cache: "no-store" });
  return asJson<MatchThread[]>(res);
}

export function downloadUrl(matchId: string): string {
  return `${API_BASE}/api/workflow/${encodeURIComponent(matchId)}/download`;
}

export async function approveWorkflow(
  matchId: string,
  payload: { script_raw?: string; visual_prompts?: string[] },
): Promise<ApproveResponse> {
  const res = await fetch(
    `${API_BASE}/api/workflow/${encodeURIComponent(matchId)}/approve`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    },
  );
  return asJson<ApproveResponse>(res);
}

export async function uploadAssets(
  matchId: string,
  files: File[],
): Promise<UploadAssetsResponse> {
  const form = new FormData();
  for (const file of files) form.append("files", file, file.name);

  const res = await fetch(
    `${API_BASE}/api/workflow/${encodeURIComponent(matchId)}/upload-assets`,
    { method: "POST", body: form },
  );
  return asJson<UploadAssetsResponse>(res);
}

/** Narrowing guard used when reconciling API payloads into UI state. */
export function isWorkflowStatus(value: unknown): value is WorkflowStatus {
  return (
    typeof value === "string" &&
    [
      "SCRAPED",
      "PENDING_APPROVAL",
      "APPROVED",
      "PROCESSING_ASSETS",
      "RENDERING",
      "COMPLETED",
    ].includes(value)
  );
}

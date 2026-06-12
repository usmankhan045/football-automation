// Thin client over the FastAPI engine. Every call is keyed by match_id, which
// is also the LangGraph thread_id on the backend.

import type {
  ApproveResponse,
  AvailableMatch,
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

export async function resumeWorkflow(matchId: string): Promise<MatchThread> {
  const res = await fetch(`${API_BASE}/api/workflow/${encodeURIComponent(matchId)}/resume`, {
    method: "POST",
  });
  return asJson<MatchThread>(res);
}

export async function listWorkflows(): Promise<MatchThread[]> {
  const res = await fetch(`${API_BASE}/api/workflow`, { cache: "no-store" });
  return asJson<MatchThread[]>(res);
}

export async function listAvailableMatches(
  date?: string,
  lookbackDays = 0,
): Promise<{ matches: AvailableMatch[]; warning?: string }> {
  const params = new URLSearchParams();
  if (date) params.set("date", date);
  params.set("lookback_days", String(lookbackDays));
  const query = params.toString() ? `?${params.toString()}` : "";
  const res = await fetch(`${API_BASE}/api/matches${query}`, { cache: "no-store" });
  const matches = await asJson<AvailableMatch[]>(res);
  return { matches, warning: res.headers.get("X-API-Warning") ?? undefined };
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
  onProgress?: (percent: number) => void,
): Promise<UploadAssetsResponse> {
  const form = new FormData();
  for (const file of files) form.append("files", file, file.name);

  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open(
      "POST",
      `${API_BASE}/api/workflow/${encodeURIComponent(matchId)}/upload-assets`,
    );
    xhr.upload.onprogress = (event) => {
      if (!event.lengthComputable) return;
      onProgress?.(Math.round((event.loaded / event.total) * 100));
    };
    xhr.onload = () => {
      try {
        const body = JSON.parse(xhr.responseText || "{}") as UploadAssetsResponse & {
          detail?: string;
        };
        if (xhr.status < 200 || xhr.status >= 300) {
          reject(new Error(`${xhr.status} · ${body.detail ?? xhr.statusText}`));
          return;
        }
        onProgress?.(100);
        resolve(body);
      } catch {
        reject(new Error(`${xhr.status} · Invalid upload response`));
      }
    };
    xhr.onerror = () => reject(new Error("Upload failed. Check backend connection."));
    xhr.send(form);
  });
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

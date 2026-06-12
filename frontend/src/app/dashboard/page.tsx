"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ScriptReviewPanel } from "@/components/ScriptReviewPanel";
import { AssetDropzone } from "@/components/AssetDropzone";
import { StatusBadge } from "@/components/StatusBadge";
import { StatusStepper } from "@/components/StatusStepper";
import {
  API_BASE,
  approveWorkflow,
  downloadUrl,
  fetchState,
  listAvailableMatches,
  listWorkflows,
  resumeWorkflow,
  startWorkflow,
  uploadAssets,
} from "@/lib/api";
import { isActionable, STATUS_META } from "@/lib/status";
import type { AvailableMatch, MatchThread, WorkflowStatus } from "@/lib/types";

interface Notice {
  tone: "ok" | "warn";
  msg: string;
}

const POLL_MS = 8000;

export default function DashboardPage() {
  const [threads, setThreads] = useState<MatchThread[]>([]);
  const [selectedId, setSelectedId] = useState<string>("");
  const [availableMatches, setAvailableMatches] = useState<AvailableMatch[]>([]);
  const [selectedMatchId, setSelectedMatchId] = useState<string>("");
  const [matchDate, setMatchDate] = useState(todayInputValue);
  const [busy, setBusy] = useState(false);
  const [matchesLoading, setMatchesLoading] = useState(false);
  const [connected, setConnected] = useState<boolean | null>(null);
  const [notice, setNotice] = useState<Notice | null>(null);
  const [clock, setClock] = useState<string>("--:--:--");
  const [mounted, setMounted] = useState(false);
  const busyRef = useRef(false);

  useEffect(() => {
    busyRef.current = busy;
  }, [busy]);

  const flash = useCallback((n: Notice) => {
    setNotice(n);
    window.setTimeout(() => setNotice(null), 3800);
  }, []);

  const loadThreads = useCallback(async () => {
    try {
      const list = await listWorkflows();
      setConnected(true);
      setThreads(list);
      setSelectedId((prev) =>
        prev && list.some((t) => t.match_id === prev)
          ? prev
          : list[list.length - 1]?.match_id ?? "",
      );
    } catch {
      setConnected(false);
    }
  }, []);

  const loadMatches = useCallback(async () => {
    setMatchesLoading(true);
    try {
      const result = await listAvailableMatches(matchDate);
      const list = result.matches;
      setConnected(true);
      setAvailableMatches(list);
      setSelectedMatchId((prev) =>
        prev && list.some((m) => m.id === prev) ? prev : list[0]?.id ?? "",
      );
      if (result.warning) {
        flash({ tone: "warn", msg: result.warning });
      }
    } catch (err) {
      setConnected(false);
      setAvailableMatches([]);
      setSelectedMatchId("");
      flash({ tone: "warn", msg: `Match list failed — ${errText(err)}` });
    } finally {
      setMatchesLoading(false);
    }
  }, [flash, matchDate]);

  // Initial load + live polling (paused while an action is in-flight).
  useEffect(() => {
    loadThreads();
    const id = window.setInterval(() => {
      if (!busyRef.current) loadThreads();
    }, POLL_MS);
    return () => window.clearInterval(id);
  }, [loadThreads]);

  useEffect(() => {
    loadMatches();
  }, [loadMatches]);

  // Live clock — client-only to avoid hydration mismatch.
  useEffect(() => {
    setMounted(true);
    const tick = () => setClock(new Date().toLocaleTimeString("en-GB", { hour12: false }));
    tick();
    const id = window.setInterval(tick, 1000);
    return () => window.clearInterval(id);
  }, []);

  const selected = useMemo(
    () => threads.find((t) => t.match_id === selectedId) ?? null,
    [threads, selectedId],
  );
  const selectedMatch = useMemo(
    () => availableMatches.find((m) => m.id === selectedMatchId) ?? null,
    [availableMatches, selectedMatchId],
  );
  const demoMode = availableMatches.some((m) => m.data_source === "mock");
  const shiftDate = useCallback((days: number) => {
    setMatchDate((prev) => addDays(prev, days));
  }, []);

  const refreshOne = useCallback(async (matchId: string) => {
    try {
      const fresh = await fetchState(matchId);
      setThreads((prev) => prev.map((t) => (t.match_id === matchId ? fresh : t)));
    } catch {
      /* ignore — next poll reconciles */
    }
  }, []);

  const recoverExistingThread = useCallback(
    async (matchId: string) => {
      let existing = await fetchState(matchId);
      if (!existing.interrupted && existing.next_nodes?.length) {
        existing = await resumeWorkflow(matchId);
      }
      setThreads((prev) =>
        prev.some((t) => t.match_id === existing.match_id)
          ? prev.map((t) => (t.match_id === existing.match_id ? existing : t))
          : [...prev, existing],
      );
      setSelectedId(existing.match_id);
      return existing;
    },
    [],
  );

  const metrics = useMemo(() => {
    const by = (s: WorkflowStatus) => threads.filter((t) => t.status === s).length;
    return {
      total: threads.length,
      review: by("PENDING_APPROVAL"),
      assets: by("PROCESSING_ASSETS"),
      done: by("COMPLETED"),
    };
  }, [threads]);

  // --- Actions -------------------------------------------------------------
  async function handleStart(e: React.FormEvent) {
    e.preventDefault();
    const id = selectedMatch?.id.trim();
    if (!id || busy) return;
    setBusy(true);
    try {
      const thread = await startWorkflow(id);
      await loadThreads();
      setSelectedId(thread.match_id);
      flash({
        tone: "ok",
        msg: `Started ${thread.match_id} · ${thread.match_stats?.home_team ?? "?"} ${
          thread.match_stats?.final_score ?? ""
        } ${thread.match_stats?.away_team ?? ""}`.trim(),
      });
    } catch (err) {
      if (err instanceof Error && err.message.startsWith("409")) {
        try {
          const existing = await recoverExistingThread(id);
          flash({ tone: "warn", msg: `Thread already exists · opened ${existing.match_id}` });
          return;
        } catch {
          /* fall through to the normal error */
        }
      }
      flash({ tone: "warn", msg: `Start failed — ${errText(err)}` });
    } finally {
      setBusy(false);
    }
  }

  async function handleApprove(payload: { script_raw: string; visual_prompts: string[] }) {
    if (!selected) return;
    setBusy(true);
    try {
      const res = await approveWorkflow(selected.match_id, payload);
      await refreshOne(selected.match_id);
      flash({ tone: "ok", msg: `Approved · ${selected.match_id} → ${res.status}` });
    } catch (err) {
      flash({ tone: "warn", msg: `Approve failed — ${errText(err)}` });
    } finally {
      setBusy(false);
    }
  }

  async function handleResume(matchId = selected?.match_id) {
    if (!matchId || busy) return;
    setBusy(true);
    try {
      const resumed = await resumeWorkflow(matchId);
      setThreads((prev) =>
        prev.some((t) => t.match_id === resumed.match_id)
          ? prev.map((t) => (t.match_id === resumed.match_id ? resumed : t))
          : [...prev, resumed],
      );
      setSelectedId(resumed.match_id);
      flash({ tone: "ok", msg: `Continued · ${resumed.match_id} → ${resumed.status}` });
    } catch (err) {
      flash({ tone: "warn", msg: `Continue failed — ${errText(err)}` });
    } finally {
      setBusy(false);
    }
  }

  async function handleUpload(files: File[], onProgress?: (percent: number) => void) {
    if (!selected) return;
    setBusy(true);
    try {
      const res = await uploadAssets(selected.match_id, files, onProgress);
      await refreshOne(selected.match_id);
      setThreads((prev) =>
        prev.map((t) =>
          t.match_id === selected.match_id
            ? { ...t, status: res.status, uploaded_clips: res.uploaded_clips }
            : t,
        ),
      );
      flash({
        tone: "ok",
        msg: `Uploaded ${res.uploaded_clips}/${res.expected_clips} · ${res.status}`,
      });
    } catch (err) {
      flash({ tone: "warn", msg: `Upload failed — ${errText(err)}` });
      throw err;
    } finally {
      setBusy(false);
    }
  }

  // --- Render --------------------------------------------------------------
  return (
    <main className="mx-auto flex min-h-screen max-w-[1480px] flex-col px-5 lg:px-8">
      {/* ===== Top bar ===== */}
      <header className="flex flex-col gap-4 border-b border-line py-5 lg:flex-row lg:items-center lg:justify-between">
        <div className="flex items-center gap-4">
          <div
            className="flex h-9 w-9 items-center justify-center border border-line text-accent"
            style={{ boxShadow: "inset 0 0 18px rgba(0,240,200,0.12)" }}
          >
            <span className="font-mono text-sm">◈</span>
          </div>
          <div>
            <h1 className="text-[15px] font-semibold uppercase tracking-[0.28em] text-ink">
              Tactical&nbsp;Ops
            </h1>
            <p className="label mt-1">Human-in-the-Loop · Control Board</p>
          </div>
        </div>

        <div className="flex flex-wrap items-center gap-x-6 gap-y-2">
          <Metric value={metrics.total} label="Threads" />
          <Metric value={metrics.review} label="Awaiting Review" hex="#ffb000" />
          <Metric value={metrics.assets} label="Awaiting Assets" hex="#b06cff" />
          <Metric value={metrics.done} label="Completed" hex="#2fe6a0" />
          <div className="hidden h-8 w-px bg-line lg:block" />
          <div className="text-right">
            <div className="flex items-center justify-end gap-2">
              <span
                className="h-1.5 w-1.5 rounded-full"
                style={{
                  background: connected ? "var(--accent)" : "#ff5d5d",
                  boxShadow: `0 0 8px ${connected ? "var(--accent)" : "#ff5d5d"}`,
                }}
              />
              <span className="font-mono text-[15px] tabular-nums text-ink" suppressHydrationWarning>
                {mounted ? clock : "--:--:--"}
              </span>
            </div>
            <p className="label mt-1 text-right">
              {connected === false ? "ENGINE · OFFLINE" : `ENGINE · ${hostOf(API_BASE)}`}
            </p>
          </div>
        </div>
      </header>

      {/* ===== Notice ===== */}
      {notice && (
        <div
          className="mt-4 border px-4 py-2.5 font-mono text-[11px] tracking-[0.06em] animate-rise"
          style={{
            color: notice.tone === "ok" ? "#2fe6a0" : "#ff7a3c",
            borderColor: notice.tone === "ok" ? "#2fe6a055" : "#ff7a3c55",
            background: notice.tone === "ok" ? "#2fe6a00d" : "#ff7a3c0d",
          }}
        >
          {notice.tone === "ok" ? "✓ " : "⚠ "}
          {notice.msg}
        </div>
      )}

      {connected === false && (
        <div
          className="mt-4 border px-4 py-2.5 font-mono text-[11px] tracking-[0.06em]"
          style={{ color: "#ff7a3c", borderColor: "#ff7a3c55", background: "#ff7a3c0d" }}
        >
          ⚠ Backend unreachable at {API_BASE} — start it with{" "}
          <span className="text-ink">uvicorn app.main:app --port 8000</span>
        </div>
      )}

      {/* ===== Body: master / detail ===== */}
      <div className="grid min-h-0 flex-1 grid-cols-1 gap-5 py-5 lg:grid-cols-[340px_1fr]">
        {/* Sidebar — start form + thread roster */}
        <aside className="flex flex-col gap-3">
          {/* Start a workflow */}
          <form onSubmit={handleStart} className="panel corner p-3.5">
            <div className="flex items-center justify-between gap-3">
              <label className="label">Pick Match</label>
              <button
                type="button"
                onClick={loadMatches}
                disabled={matchesLoading || busy}
                className="font-mono text-[10px] uppercase tracking-[0.16em] text-dim transition-colors hover:text-accent disabled:opacity-40"
              >
                {matchesLoading ? "Loading" : "Refresh"}
              </button>
            </div>

            <div className="mt-2.5 grid grid-cols-[42px_1fr_42px] gap-2">
              <button
                type="button"
                onClick={() => shiftDate(-1)}
                className="btn px-2 py-2"
                aria-label="Previous date"
              >
                ←
              </button>
              <input
                type="date"
                value={matchDate}
                onChange={(e) => setMatchDate(e.target.value)}
                className="min-w-0 border border-line bg-[#070a0e] px-3 py-2 font-mono text-[12px] text-ink outline-none transition-colors focus:border-[rgba(0,240,200,0.45)]"
              />
              <button
                type="button"
                onClick={() => shiftDate(1)}
                className="btn px-2 py-2"
                aria-label="Next date"
              >
                →
              </button>
            </div>

            <button
              type="button"
              onClick={() => setMatchDate(todayInputValue())}
              className="mt-2 font-mono text-[10px] uppercase tracking-[0.16em] text-dim transition-colors hover:text-accent"
            >
              Today · checking previous day
            </button>

            {demoMode && (
              <p className="mt-2 border border-[rgba(255,176,0,0.26)] bg-[rgba(255,176,0,0.06)] px-3 py-2 font-mono text-[10px] leading-relaxed text-[#ffb000]">
                Demo mode. Set HIGHLIGHTLY_API_KEY and refresh for live matches.
              </p>
            )}

            <div className="scroll-thin mt-2.5 flex max-h-[210px] flex-col gap-2 overflow-y-auto pr-1">
              {availableMatches.length === 0 && (
                <div className="border border-line px-3 py-4 text-center font-mono text-[11px] text-mute">
                  {matchesLoading
                    ? "Scanning finished matches..."
                    : "No completed World Cup 2026 matches found in this date window."}
                </div>
              )}

              {availableMatches.map((match) => {
                const active = match.id === selectedMatchId;
                return (
                  <button
                    key={match.id}
                    type="button"
                    onClick={() => setSelectedMatchId(match.id)}
                    className="border bg-[#070a0e] p-3 text-left transition-colors hover:border-[rgba(0,240,200,0.45)]"
                    style={{
                      borderColor: active ? "rgba(0,240,200,0.58)" : "var(--line)",
                      boxShadow: active ? "inset 0 0 22px rgba(0,240,200,0.1)" : undefined,
                    }}
                  >
                    <div className="flex items-start justify-between gap-2">
                      <div className="min-w-0">
                        <p className="truncate text-[12px] text-ink">
                          {match.home_team} v {match.away_team}
                        </p>
                        <p className="mt-1 truncate font-mono text-[10px] uppercase tracking-[0.08em] text-mute">
                          {match.competition ?? match.data_source}
                          {match.stage ? ` · ${match.stage}` : ""}
                        </p>
                      </div>
                      <span className="shrink-0 font-mono text-[11px] text-dim">
                        {match.final_score ?? match.status ?? "TBD"}
                      </span>
                    </div>
                  </button>
                );
              })}
            </div>

            <button
              type="submit"
              disabled={busy || !selectedMatch}
              className="btn mt-3 w-full px-3 py-2"
            >
              {busy ? "Starting..." : "Start Selected"}
            </button>
          </form>

          <div className="flex items-center justify-between pt-1">
            <span className="label">Active Threads</span>
            <button
              type="button"
              onClick={loadThreads}
              disabled={busy}
              className="font-mono text-[10px] uppercase tracking-[0.16em] text-dim transition-colors hover:text-accent disabled:opacity-40"
            >
              ↻ Sync
            </button>
          </div>

          <div className="scroll-thin flex max-h-[calc(100vh-330px)] flex-col gap-2.5 overflow-y-auto pr-1">
            {threads.length === 0 && (
              <div className="panel corner p-4 text-center font-mono text-[11px] leading-relaxed text-mute">
                {connected === false
                  ? "No connection to engine."
                  : "No threads yet. Start one above ↑"}
              </div>
            )}
            {threads.map((t, i) => {
              const meta = STATUS_META[t.status];
              const active = t.match_id === selectedId;
              return (
                <button
                  key={t.match_id}
                  type="button"
                  onClick={() => setSelectedId(t.match_id)}
                  className="panel corner group animate-rise p-3.5 text-left transition-colors"
                  style={{
                    animationDelay: `${Math.min(i, 8) * 45}ms`,
                    borderColor: active ? `${meta.hex}66` : undefined,
                    boxShadow: active ? `inset 0 0 26px ${meta.hex}14` : undefined,
                  }}
                >
                  <div className="flex items-start justify-between gap-2">
                    <div className="min-w-0">
                      <p className="truncate font-mono text-[12px] tracking-[0.04em] text-ink">
                        {t.match_id}
                      </p>
                      <p className="mt-0.5 truncate text-[12px] text-dim">
                        {t.match_stats?.home_team ?? "—"} {t.match_stats?.final_score ?? ""}{" "}
                        {t.match_stats?.away_team ?? ""}
                      </p>
                    </div>
                    {isActionable(t.status) && (
                      <span
                        className="mt-1 h-1.5 w-1.5 shrink-0 animate-pulseDot rounded-full"
                        style={{ background: meta.hex, boxShadow: `0 0 8px ${meta.hex}` }}
                      />
                    )}
                  </div>
                  <div className="mt-3">
                    <StatusStepper status={t.status} compact />
                  </div>
                  <div className="mt-3">
                    <StatusBadge status={t.status} pulse={isActionable(t.status)} />
                  </div>
                </button>
              );
            })}
          </div>
        </aside>

        {/* Workspace */}
        <div key={selected?.match_id ?? "empty"} className="flex min-h-[560px] flex-col">
          {!selected ? (
            <EmptyWorkspace connected={connected} />
          ) : selected.status === "PENDING_APPROVAL" ? (
            <ScriptReviewPanel thread={selected} busy={busy} onApprove={handleApprove} />
          ) : selected.status === "PROCESSING_ASSETS" ? (
            <AssetDropzone
              matchId={selected.match_id}
              expectedClips={
                (selected.interrupt_payload?.expected_clips as number) ??
                selected.video_prompts.length
              }
              uploadedClips={selected.uploaded_clips ?? 0}
              prompts={selected.video_prompts}
              busy={busy}
              onUpload={handleUpload}
            />
          ) : (
            <TelemetryView thread={selected} busy={busy} onResume={() => handleResume()} />
          )}
        </div>
      </div>
    </main>
  );
}

// --- Empty workspace --------------------------------------------------------
function EmptyWorkspace({ connected }: { connected: boolean | null }) {
  return (
    <section className="panel corner flex h-full flex-col items-center justify-center gap-3 p-10 text-center animate-rise">
      <span className="font-mono text-3xl text-dim">◈</span>
      <h2 className="font-mono text-[13px] uppercase tracking-[0.22em] text-ink">
        No thread selected
      </h2>
      <p className="max-w-sm font-mono text-[11px] leading-relaxed text-mute">
        {connected === false
          ? "The engine is offline. Launch the backend, then start a workflow."
          : "Pick a match on the left and start the Outcome-First pipeline."}
      </p>
    </section>
  );
}

// --- Detail / completed view -----------------------------------------------
function TelemetryView({
  thread,
  busy,
  onResume,
}: {
  thread: MatchThread;
  busy?: boolean;
  onResume: () => void;
}) {
  const s = thread.match_stats;
  const completed = thread.status === "COMPLETED";
  const canContinue =
    !completed &&
    !thread.interrupted &&
    Array.isArray(thread.next_nodes) &&
    thread.next_nodes.length > 0;
  return (
    <section className="panel corner flex h-full flex-col animate-rise">
      <header className="flex items-center justify-between border-b border-line px-5 py-3.5">
        <h2 className="font-mono text-[12px] uppercase tracking-[0.22em] text-ink">
          {completed ? "Render Complete" : "Thread Telemetry"}
        </h2>
        <div className="flex items-center gap-3">
          {canContinue && (
            <button
              type="button"
              onClick={onResume}
              disabled={busy}
              className="btn px-4 py-2"
              style={{ borderColor: "rgba(0,240,200,0.55)", color: "#eafffa" }}
            >
              {busy ? "Continuing..." : "Continue Pipeline ->"}
            </button>
          )}
          {completed && thread.output_path && (
            <a
              href={downloadUrl(thread.match_id)}
              className="btn px-4 py-2 no-underline"
              style={{ borderColor: "rgba(0,240,200,0.55)", color: "#eafffa" }}
            >
              ↓ Download .mp4
            </a>
          )}
          <StatusBadge status={thread.status} pulse={isActionable(thread.status)} />
        </div>
      </header>

      <div className="flex-1 space-y-6 p-5">
        <StatusStepper status={thread.status} />
        <div className="hairline" />

        {canContinue && (
          <div className="border border-[rgba(0,240,200,0.28)] bg-[rgba(0,240,200,0.05)] px-4 py-3">
            <span className="label">Next Step</span>
            <p className="mt-1.5 font-mono text-[12px] leading-relaxed text-ink">
              Ready to continue into {thread.next_nodes?.join(", ")}.
            </p>
          </div>
        )}

        <div className="grid grid-cols-2 gap-x-8 gap-y-4 sm:grid-cols-3">
          <Field label="Match ID" value={thread.match_id} mono />
          <Field label="Fixture" value={`${s?.home_team ?? "—"} v ${s?.away_team ?? "—"}`} />
          <Field label="Stage" value={String(s?.stage ?? "—")} />
          <Field label="Final Score" value={s?.final_score ?? "—"} mono />
          <Field
            label="Possession"
            value={
              s?.possession_pct
                ? Object.entries(s.possession_pct).map(([k, v]) => `${k} ${v}%`).join(" · ")
                : "—"
            }
          />
          <Field
            label="Expected Goals"
            value={
              s?.xg
                ? Object.entries(s.xg).map(([k, v]) => `${k} ${v}`).join(" · ")
                : "—"
            }
            mono
          />
        </div>

        {completed && (
          <div
            className="border-l-2 pl-4"
            style={{ borderColor: thread.output_path ? "#2fe6a0" : "#ffb000" }}
          >
            <span className="label">Output</span>
            <p className="mt-1.5 break-all font-mono text-[12px] leading-relaxed text-dim">
              {thread.output_path
                ? thread.output_path
                : "Rendering disabled — restart backend with VIDEO_RENDER_MODE=stub to export the .mp4."}
            </p>
          </div>
        )}

        {typeof s?.biggest_anomaly === "string" && (
          <div className="border-l-2 pl-4" style={{ borderColor: "#ffb000" }}>
            <span className="label">Stat Anomaly</span>
            <p className="mt-1.5 text-[13px] leading-relaxed text-dim">{s.biggest_anomaly}</p>
          </div>
        )}

        {thread.script_raw && (
          <div>
            <span className="label">Script Draft</span>
            <p className="mt-1.5 max-w-3xl text-[13px] leading-relaxed text-dim">
              {thread.script_raw}
            </p>
          </div>
        )}
      </div>
    </section>
  );
}

// --- Small presentational helpers -------------------------------------------
function Metric({ value, label, hex }: { value: number; label: string; hex?: string }) {
  return (
    <div className="text-center">
      <div
        className="font-mono text-[18px] leading-none tabular-nums"
        style={{ color: hex ?? "var(--text)" }}
      >
        {String(value).padStart(2, "0")}
      </div>
      <p className="label mt-1.5">{label}</p>
    </div>
  );
}

function Field({ label, value, mono = false }: { label: string; value: string; mono?: boolean }) {
  return (
    <div>
      <span className="label">{label}</span>
      <p className={`mt-1 text-[13px] text-ink ${mono ? "font-mono tracking-[0.04em]" : ""}`}>
        {value}
      </p>
    </div>
  );
}

function errText(err: unknown): string {
  return err instanceof Error ? err.message : "unknown error";
}

function hostOf(url: string): string {
  try {
    return new URL(url).host;
  } catch {
    return url;
  }
}

function todayInputValue(): string {
  return new Date().toISOString().slice(0, 10);
}

function addDays(value: string, days: number): string {
  const date = value ? new Date(`${value}T00:00:00`) : new Date();
  date.setDate(date.getDate() + days);
  return date.toISOString().slice(0, 10);
}

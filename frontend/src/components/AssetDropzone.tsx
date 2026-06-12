"use client";

import { useCallback, useEffect, useRef, useState } from "react";

interface Props {
  matchId: string;
  expectedClips: number;
  uploadedClips: number;
  prompts: string[];
  busy?: boolean;
  /** Pushes the staged .mp4 files to the backend upload-assets endpoint. */
  onUpload: (files: File[], onProgress?: (percent: number) => void) => Promise<void>;
}

// Drag-and-drop interface for the Veo .mp4 clips downloaded from Google Flow,
// routed to the match's storage directory on the backend.
export function AssetDropzone({
  matchId,
  expectedClips,
  uploadedClips,
  prompts,
  busy = false,
  onUpload,
}: Props) {
  const [dragging, setDragging] = useState(false);
  const [staged, setStaged] = useState<File[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [promptIndex, setPromptIndex] = useState(0);
  const [copiedIndex, setCopiedIndex] = useState<number | null>(null);
  const [uploadProgress, setUploadProgress] = useState<number | null>(null);
  const [uploadComplete, setUploadComplete] = useState(false);
  const [localConfirmed, setLocalConfirmed] = useState(uploadedClips);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    setLocalConfirmed((count) => Math.max(count, uploadedClips));
  }, [uploadedClips]);

  const accept = useCallback((fileList: FileList | null) => {
    if (!fileList) return;
    const incoming = Array.from(fileList);
    const valid = incoming.filter((f) => f.name.toLowerCase().endsWith(".mp4"));
    if (valid.length !== incoming.length) {
      setError("Only .mp4 clips are accepted.");
    } else {
      setError(null);
    }
    setUploadComplete(false);
    setStaged((prev) => {
      const seen = new Set(prev.map((f) => f.name));
      return [...prev, ...valid.filter((f) => !seen.has(f.name))];
    });
  }, []);

  const onDrop = useCallback(
    (e: React.DragEvent) => {
      e.preventDefault();
      setDragging(false);
      accept(e.dataTransfer.files);
    },
    [accept],
  );

  const remaining = Math.max(expectedClips - uploadedClips, 0);
  const progress = expectedClips > 0 ? Math.min(uploadedClips / expectedClips, 1) : 0;
  const clientConfirmed = Math.min(Math.max(uploadedClips, localConfirmed), expectedClips);
  const displayProgress =
    expectedClips > 0 ? Math.min(clientConfirmed / expectedClips, 1) : progress;
  const visiblePrompts = prompts.length ? prompts : Array.from({ length: expectedClips }, () => "");
  const activePrompt = visiblePrompts[promptIndex] ?? "";

  async function copyPrompt(index: number) {
    const prompt = visiblePrompts[index] ?? "";
    if (!prompt.trim()) return;
    try {
      await navigator.clipboard.writeText(prompt);
      setCopiedIndex(index);
      window.setTimeout(
        () => setCopiedIndex((current) => (current === index ? null : current)),
        1400,
      );
    } catch {
      setError("Clipboard access was blocked by the browser.");
    }
  }

  return (
    <section className="panel corner flex h-full flex-col animate-rise">
      <header className="flex items-center justify-between border-b border-line px-5 py-3.5">
        <div className="flex items-center gap-3">
          <span
            className="h-1.5 w-1.5 animate-pulseDot rounded-full"
            style={{ background: "#b06cff", boxShadow: "0 0 8px #b06cff" }}
          />
          <h2 className="font-mono text-[12px] uppercase tracking-[0.22em] text-ink">
            Asset Ingest
          </h2>
        </div>
        <span className="label">CHK · ASSET_UPLOAD_REQUIRED</span>
      </header>

      <div className="flex min-h-0 flex-1 flex-col gap-4 p-5">
        {/* Progress track */}
        <div>
          <div className="mb-2 flex items-center justify-between">
            <span className="label">Render Clips · {matchId}</span>
            <span className="font-mono text-[11px] tracking-[0.1em] text-dim">
              {clientConfirmed} / {expectedClips} CONFIRMED
            </span>
          </div>
          <div className="h-1.5 w-full bg-[#0b0f14]">
            <div
              className="h-full transition-[width] duration-500"
              style={{
                width: `${displayProgress * 100}%`,
                background: "linear-gradient(90deg,#b06cff,#00f0c8)",
                boxShadow: "0 0 12px rgba(176,108,255,0.55)",
              }}
            />
          </div>
        </div>

        <div className="grid min-h-0 flex-1 grid-cols-1 gap-4 xl:grid-cols-[0.85fr_1.15fr]">
          <div className="flex min-h-0 flex-col border border-line bg-[#070a0e]">
            <div className="flex items-center justify-between border-b border-line px-4 py-3">
              <span className="label">Prompts · Upload Order</span>
              <span className="font-mono text-[11px] tracking-[0.1em] text-dim">
                {promptIndex + 1} / {visiblePrompts.length}
              </span>
            </div>

            <div className="flex items-center gap-2 border-b border-line p-3">
              <button
                type="button"
                onClick={() => setPromptIndex((i) => Math.max(i - 1, 0))}
                disabled={promptIndex === 0}
                className="btn px-3 py-2"
                aria-label="Previous prompt"
              >
                ←
              </button>
              <div className="min-w-0 flex-1 text-center font-mono text-[10px] uppercase tracking-[0.16em] text-dim">
                Prompt {String(promptIndex + 1).padStart(2, "0")}
              </div>
              <button
                type="button"
                onClick={() =>
                  setPromptIndex((i) => Math.min(i + 1, visiblePrompts.length - 1))
                }
                disabled={promptIndex >= visiblePrompts.length - 1}
                className="btn px-3 py-2"
                aria-label="Next prompt"
              >
                →
              </button>
            </div>

            <div className="border-b border-line p-4">
              <div className="mb-3 flex items-center justify-between gap-3">
                <span className="font-mono text-[10px] uppercase tracking-[0.18em] text-mute">
                  Active Prompt
                </span>
                <button
                  type="button"
                  onClick={() => copyPrompt(promptIndex)}
                  disabled={!activePrompt.trim()}
                  className="font-mono text-[10px] uppercase tracking-[0.16em] text-dim transition-colors hover:text-accent disabled:opacity-40"
                >
                  {copiedIndex === promptIndex ? "Copied" : "Copy"}
                </button>
              </div>
              <p className="font-mono text-[12px] leading-relaxed text-ink">
                {activePrompt || "Prompt unavailable for this clip."}
              </p>
            </div>

            <ol className="scroll-thin min-h-0 flex-1 space-y-2 overflow-y-auto p-3">
              {visiblePrompts.map((prompt, i) => (
                <li key={`${prompt}-${i}`}>
                  <button
                    type="button"
                    onClick={() => setPromptIndex(i)}
                    className="w-full border p-3 text-left transition-colors hover:border-[rgba(0,240,200,0.45)]"
                    style={{
                      borderColor: i === promptIndex ? "rgba(176,108,255,0.7)" : "var(--line)",
                      background: i === promptIndex ? "rgba(176,108,255,0.08)" : "#080b10",
                    }}
                  >
                    <div className="mb-1 flex items-center justify-between gap-2">
                      <span className="font-mono text-[10px] uppercase tracking-[0.18em] text-mute">
                        Clip {String(i + 1).padStart(2, "0")}
                      </span>
                      <div className="flex shrink-0 items-center gap-3">
                        <button
                          type="button"
                          onClick={(event) => {
                            event.stopPropagation();
                            copyPrompt(i);
                          }}
                          disabled={!prompt.trim()}
                          className="font-mono text-[10px] uppercase tracking-[0.14em] text-dim transition-colors hover:text-accent disabled:opacity-40"
                        >
                          {copiedIndex === i ? "Copied" : "Copy"}
                        </button>
                        <span className="font-mono text-[10px] text-dim">
                          {i < clientConfirmed ? "Uploaded" : i < staged.length ? "Staged" : "Needed"}
                        </span>
                      </div>
                    </div>
                    <p className="line-clamp-2 text-[12px] leading-relaxed text-dim">
                      {prompt || "Prompt unavailable."}
                    </p>
                  </button>
                </li>
              ))}
            </ol>
          </div>

          {/* Dropzone */}
          <div
            onDragOver={(e) => {
              e.preventDefault();
              setDragging(true);
            }}
            onDragLeave={() => setDragging(false)}
            onDrop={onDrop}
            onClick={() => inputRef.current?.click()}
            role="button"
            tabIndex={0}
            className="relative flex min-h-[260px] cursor-pointer flex-col items-center justify-center gap-2 border border-dashed p-6 text-center transition-colors"
            style={{
              borderColor: dragging ? "rgba(0,240,200,0.6)" : "var(--line-2)",
              background: dragging ? "rgba(0,240,200,0.05)" : "transparent",
            }}
          >
            {dragging && (
              <span className="pointer-events-none absolute inset-x-0 top-0 h-px animate-sweep bg-accent/70" />
            )}
            <span className="font-mono text-2xl text-dim">⬡</span>
            <p className="font-mono text-[12px] uppercase tracking-[0.18em] text-ink">
              Drop .mp4 clips here
            </p>
            <p className="font-mono text-[10px] tracking-[0.1em] text-mute">
              upload files in prompt order · {remaining} remaining
            </p>
            <input
              ref={inputRef}
              type="file"
              accept="video/mp4,.mp4"
              multiple
              className="hidden"
              onChange={(e) => accept(e.target.files)}
            />
          </div>
        </div>

        {error && (
          <p className="font-mono text-[11px] tracking-[0.08em]" style={{ color: "#ff7a3c" }}>
            ⚠ {error}
          </p>
        )}

        {uploadProgress !== null && (
          <div className="border border-line bg-[#080b10] px-3 py-3">
            <div className="mb-2 flex items-center justify-between">
              <span className="font-mono text-[10px] uppercase tracking-[0.16em] text-dim">
                {uploadComplete ? "Upload Complete" : "Uploading"}
              </span>
              <span className="font-mono text-[11px] text-ink">{uploadProgress}%</span>
            </div>
            <div className="h-1.5 bg-[#0b0f14]">
              <div
                className="h-full transition-[width] duration-300"
                style={{
                  width: `${uploadProgress}%`,
                  background: uploadComplete ? "#2fe6a0" : "linear-gradient(90deg,#b06cff,#00f0c8)",
                }}
              />
            </div>
            {uploadComplete && (
              <p className="mt-2 font-mono text-[10px] uppercase tracking-[0.14em] text-[#2fe6a0]">
                Files received by backend. Waiting for thread status sync.
              </p>
            )}
          </div>
        )}

        {/* Staged file list */}
        {staged.length > 0 && (
          <ul className="scroll-thin max-h-32 space-y-1.5 overflow-y-auto pr-1">
            {staged.map((file, i) => (
              <li
                key={`${file.name}-${i}`}
                className="flex items-center justify-between border border-line bg-[#080b10] px-3 py-2"
              >
                <span className="truncate font-mono text-[11px] text-dim">
                  ▸ {file.name}
                </span>
                <button
                  type="button"
                  onClick={() =>
                    setStaged((prev) => prev.filter((_, idx) => idx !== i))
                  }
                  className="ml-3 font-mono text-[10px] uppercase tracking-[0.14em] text-mute transition-colors hover:text-[#ff7a3c]"
                >
                  ✕
                </button>
              </li>
            ))}
          </ul>
        )}
      </div>

      <footer className="flex flex-col gap-3 border-t border-line px-5 py-4 sm:flex-row sm:items-center sm:justify-between">
        <span className="font-mono text-[10px] uppercase tracking-[0.16em] text-mute">
          {staged.length} STAGED
        </span>
        <button
          type="button"
          disabled={busy || staged.length === 0}
          onClick={async () => {
            setError(null);
            setUploadComplete(false);
            setUploadProgress(0);
            try {
              const stagedCount = staged.length;
              await onUpload(staged, setUploadProgress);
              setLocalConfirmed((count) =>
                Math.min(expectedClips, Math.max(count, uploadedClips + stagedCount)),
              );
              setUploadProgress(100);
              setUploadComplete(true);
              setStaged([]);
            } catch (err) {
              setUploadProgress(null);
              setUploadComplete(false);
              setError(err instanceof Error ? err.message : "Upload failed.");
            }
          }}
          className="btn px-6 py-3"
        >
          {busy ? "Uploading…" : "Upload to Match Directory →"}
        </button>
      </footer>
    </section>
  );
}

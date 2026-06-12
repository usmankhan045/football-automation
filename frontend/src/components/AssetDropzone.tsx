"use client";

import { useCallback, useRef, useState } from "react";

interface Props {
  matchId: string;
  expectedClips: number;
  uploadedClips: number;
  busy?: boolean;
  /** Pushes the staged .mp4 files to the backend upload-assets endpoint. */
  onUpload: (files: File[]) => void;
}

// Drag-and-drop interface for the Veo .mp4 clips downloaded from Google Flow,
// routed to the match's storage directory on the backend.
export function AssetDropzone({
  matchId,
  expectedClips,
  uploadedClips,
  busy = false,
  onUpload,
}: Props) {
  const [dragging, setDragging] = useState(false);
  const [staged, setStaged] = useState<File[]>([]);
  const [error, setError] = useState<string | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  const accept = useCallback((fileList: FileList | null) => {
    if (!fileList) return;
    const incoming = Array.from(fileList);
    const valid = incoming.filter((f) => f.name.toLowerCase().endsWith(".mp4"));
    if (valid.length !== incoming.length) {
      setError("Only .mp4 clips are accepted.");
    } else {
      setError(null);
    }
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
              {uploadedClips} / {expectedClips} CONFIRMED
            </span>
          </div>
          <div className="h-1.5 w-full bg-[#0b0f14]">
            <div
              className="h-full transition-[width] duration-500"
              style={{
                width: `${progress * 100}%`,
                background: "linear-gradient(90deg,#b06cff,#00f0c8)",
                boxShadow: "0 0 12px rgba(176,108,255,0.55)",
              }}
            />
          </div>
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
          className="relative flex min-h-[150px] flex-1 cursor-pointer flex-col items-center justify-center gap-2 border border-dashed p-6 text-center transition-colors"
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
            from Google Flow · or click to browse · {remaining} remaining
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

        {error && (
          <p className="font-mono text-[11px] tracking-[0.08em]" style={{ color: "#ff7a3c" }}>
            ⚠ {error}
          </p>
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
          onClick={() => {
            onUpload(staged);
            setStaged([]);
          }}
          className="btn px-6 py-3"
        >
          {busy ? "Uploading…" : "Upload to Match Directory →"}
        </button>
      </footer>
    </section>
  );
}

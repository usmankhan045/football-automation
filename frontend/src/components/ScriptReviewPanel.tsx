"use client";

import { useEffect, useMemo, useState } from "react";
import type { MatchThread } from "@/lib/types";

const WORD_MIN = 65;
const WORD_MAX = 95;

interface Props {
  thread: MatchThread;
  busy?: boolean;
  /** Commits edited script + prompts to the backend approval endpoint. */
  onApprove: (payload: { script_raw: string; visual_prompts: string[] }) => void;
}

// Split workspace activated on PENDING_APPROVAL.
//   Left  — editable Llama 4 script draft (word-count guarded).
//   Right — Veo 3.1 visual prompts, each copy-to-clipboard for Google Flow.
//   Footer— Approve & Resume Pipeline -> backend.
export function ScriptReviewPanel({ thread, busy = false, onApprove }: Props) {
  const [script, setScript] = useState(thread.script_raw);
  const [prompts, setPrompts] = useState<string[]>(thread.video_prompts);
  const [copiedIndex, setCopiedIndex] = useState<number | null>(null);

  // Re-seed local draft only when a *different* thread becomes active — keyed on
  // match_id so background polling can't wipe the operator's in-progress edits.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(() => {
    setScript(thread.script_raw);
    setPrompts(thread.video_prompts);
    setCopiedIndex(null);
  }, [thread.match_id]);

  const wordCount = useMemo(
    () => (script.trim() ? script.trim().split(/\s+/).length : 0),
    [script],
  );
  const inRange = wordCount >= WORD_MIN && wordCount <= WORD_MAX;

  async function copyPrompt(text: string, index: number) {
    try {
      await navigator.clipboard.writeText(text);
      setCopiedIndex(index);
      window.setTimeout(
        () => setCopiedIndex((c) => (c === index ? null : c)),
        1400,
      );
    } catch {
      /* clipboard blocked — silently ignore in restricted contexts */
    }
  }

  return (
    <section className="panel corner flex h-full flex-col animate-rise">
      {/* Header */}
      <header className="flex items-center justify-between border-b border-line px-5 py-3.5">
        <div className="flex items-center gap-3">
          <span
            className="h-1.5 w-1.5 animate-pulseDot rounded-full"
            style={{ background: "#ffb000", boxShadow: "0 0 8px #ffb000" }}
          />
          <h2 className="font-mono text-[12px] uppercase tracking-[0.22em] text-ink">
            Script Review
          </h2>
        </div>
        <span className="label">CHK · HUMAN_VALIDATION_REQUIRED</span>
      </header>

      {/* Split body */}
      <div className="grid min-h-0 flex-1 grid-cols-1 lg:grid-cols-[1.15fr_1fr]">
        {/* LEFT — editable draft */}
        <div className="flex min-h-0 flex-col border-b border-line p-5 lg:border-b-0 lg:border-r">
          <div className="mb-2.5 flex items-center justify-between">
            <span className="label">Llama 4 · Voiceover Draft</span>
            <span
              className="font-mono text-[11px] tracking-[0.1em]"
              style={{ color: inRange ? "#2fe6a0" : "#ff7a3c" }}
            >
              {wordCount} / {WORD_MIN}-{WORD_MAX} W
            </span>
          </div>
          <textarea
            value={script}
            onChange={(e) => setScript(e.target.value)}
            spellCheck={false}
            className="scroll-thin min-h-0 flex-1 resize-none border border-line bg-[#070a0e] p-4 font-mono text-[13px] leading-relaxed text-ink outline-none transition-colors focus:border-[rgba(0,240,200,0.45)]"
            placeholder="Awaiting generated script…"
          />
          <p className="mt-2.5 font-mono text-[10px] leading-relaxed text-mute">
            Edit inline before confirmation. Word count is enforced to keep the
            Short between 25–40 seconds.
          </p>
        </div>

        {/* RIGHT — Veo prompt array */}
        <div className="flex min-h-0 flex-col p-5">
          <div className="mb-2.5 flex items-center justify-between">
            <span className="label">Veo 3.1 · Visual Prompts</span>
            <span className="font-mono text-[11px] tracking-[0.1em] text-dim">
              {prompts.length} CLIPS
            </span>
          </div>

          <ol className="scroll-thin min-h-0 flex-1 space-y-2.5 overflow-y-auto pr-1">
            {prompts.map((prompt, i) => {
              const copied = copiedIndex === i;
              return (
                <li
                  key={i}
                  className="group relative border border-line bg-[#080b10] p-3 transition-colors hover:border-[var(--line-2)]"
                >
                  <div className="mb-2 flex items-center justify-between">
                    <span className="font-mono text-[10px] tracking-[0.18em] text-mute">
                      PROMPT_{String(i + 1).padStart(2, "0")}
                    </span>
                    <button
                      type="button"
                      onClick={() => copyPrompt(prompt, i)}
                      className="font-mono text-[10px] uppercase tracking-[0.16em] transition-colors"
                      style={{ color: copied ? "#2fe6a0" : "var(--text-dim)" }}
                    >
                      {copied ? "✓ Copied" : "⧉ Copy"}
                    </button>
                  </div>
                  <textarea
                    value={prompt}
                    onChange={(e) => {
                      const next = [...prompts];
                      next[i] = e.target.value;
                      setPrompts(next);
                    }}
                    rows={2}
                    spellCheck={false}
                    className="scroll-thin w-full resize-none bg-transparent font-mono text-[12px] leading-relaxed text-dim outline-none focus:text-ink"
                  />
                </li>
              );
            })}
            {prompts.length === 0 && (
              <li className="border border-dashed border-line p-4 text-center font-mono text-[11px] text-mute">
                No visual prompts generated.
              </li>
            )}
          </ol>
          <p className="mt-2.5 font-mono text-[10px] leading-relaxed text-mute">
            Copy each prompt straight into Google Flow to render the clip.
          </p>
        </div>
      </div>

      {/* Footer action */}
      <footer className="flex flex-col gap-3 border-t border-line px-5 py-4 sm:flex-row sm:items-center sm:justify-between">
        <span className="font-mono text-[10px] uppercase tracking-[0.16em] text-mute">
          THREAD&nbsp;·&nbsp;<span className="text-dim">{thread.match_id}</span>
        </span>
        <button
          type="button"
          disabled={busy || !inRange}
          onClick={() => onApprove({ script_raw: script, visual_prompts: prompts })}
          className="btn relative overflow-hidden px-6 py-3"
          style={
            inRange && !busy
              ? { borderColor: "rgba(0,240,200,0.55)", color: "#eafffa" }
              : undefined
          }
        >
          {busy ? "Resuming…" : "Approve & Resume Pipeline →"}
        </button>
      </footer>
    </section>
  );
}

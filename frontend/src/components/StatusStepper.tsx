import { STATUS_META, STATUS_ORDER } from "@/lib/status";
import type { WorkflowStatus } from "@/lib/types";

// Horizontal milestone backbone: completed stages glow in their accent, the
// current stage pulses, future stages stay dim. Compact variant drops labels
// for the dense sidebar cards.
export function StatusStepper({
  status,
  compact = false,
}: {
  status: WorkflowStatus;
  compact?: boolean;
}) {
  const currentIndex = STATUS_META[status].index;

  return (
    <div className="flex w-full items-center">
      {STATUS_ORDER.map((stage, i) => {
        const meta = STATUS_META[stage];
        const done = i < currentIndex;
        const active = i === currentIndex;
        const color = done || active ? meta.hex : "var(--text-mute)";
        const isLast = i === STATUS_ORDER.length - 1;

        return (
          <div key={stage} className="flex flex-1 items-center last:flex-none">
            <div className="flex flex-col items-center gap-1.5">
              <span
                className={`block ${compact ? "h-1.5 w-1.5" : "h-2 w-2"} rounded-full ${
                  active ? "animate-pulseDot" : ""
                }`}
                style={{
                  background: done || active ? color : "transparent",
                  border: `1px solid ${color}`,
                  boxShadow: done || active ? `0 0 7px ${meta.hex}88` : "none",
                }}
              />
              {!compact && (
                <span
                  className="font-mono text-[9px] tracking-[0.14em]"
                  style={{ color: active ? meta.hex : "var(--text-mute)" }}
                >
                  {meta.code}
                </span>
              )}
            </div>
            {!isLast && (
              <span
                className={`mx-1 h-px flex-1 ${compact ? "" : "mb-4"}`}
                style={{
                  background: done
                    ? `${meta.hex}88`
                    : "var(--line)",
                }}
              />
            )}
          </div>
        );
      })}
    </div>
  );
}

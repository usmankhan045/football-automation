import { STATUS_META } from "@/lib/status";
import type { WorkflowStatus } from "@/lib/types";

export function StatusBadge({
  status,
  pulse = false,
}: {
  status: WorkflowStatus;
  pulse?: boolean;
}) {
  const meta = STATUS_META[status];
  return (
    <span
      className="inline-flex items-center gap-2 border px-2.5 py-1 font-mono text-[10px] uppercase tracking-[0.18em]"
      style={{
        color: meta.hex,
        borderColor: `${meta.hex}55`,
        background: `${meta.hex}0d`,
      }}
    >
      <span
        className={`h-1.5 w-1.5 rounded-full ${pulse ? "animate-pulseDot" : ""}`}
        style={{ background: meta.hex, boxShadow: `0 0 8px ${meta.hex}` }}
      />
      {meta.label}
    </span>
  );
}

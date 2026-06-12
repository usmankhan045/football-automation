import type { WorkflowStatus } from "./types";

export interface StatusMeta {
  /** Full label for badges. */
  label: string;
  /** Compact code shown in the milestone tracker. */
  code: string;
  /** Accent hex — drives borders, dots and glows for this state. */
  hex: string;
  /** Position in the lifecycle (0-indexed). */
  index: number;
}

// Ordered lifecycle — the milestone backbone of the entire board.
export const STATUS_ORDER: WorkflowStatus[] = [
  "SCRAPED",
  "PENDING_APPROVAL",
  "APPROVED",
  "PROCESSING_ASSETS",
  "RENDERING",
  "COMPLETED",
];

export const STATUS_META: Record<WorkflowStatus, StatusMeta> = {
  SCRAPED: { label: "Scraped", code: "SCR", hex: "#5b8db8", index: 0 },
  PENDING_APPROVAL: { label: "Pending Approval", code: "REV", hex: "#ffb000", index: 1 },
  APPROVED: { label: "Approved", code: "APR", hex: "#00d3ff", index: 2 },
  PROCESSING_ASSETS: { label: "Processing Assets", code: "AST", hex: "#b06cff", index: 3 },
  RENDERING: { label: "Rendering", code: "RND", hex: "#ff7a3c", index: 4 },
  COMPLETED: { label: "Completed", code: "DONE", hex: "#2fe6a0", index: 5 },
};

/** Does this status require a human at the wheel right now? */
export function isActionable(status: WorkflowStatus): boolean {
  return status === "PENDING_APPROVAL" || status === "PROCESSING_ASSETS";
}

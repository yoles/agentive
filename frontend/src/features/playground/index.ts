/**
 * Public barrel for the Playground feature (Stories 2.7 et 2.8).
 */

export { AgentTesterForm } from "./AgentTesterForm";
export { ControllerReviewView } from "./ControllerReviewView";
export { OutputInspector } from "./OutputInspector";
export { PlaygroundPage } from "./PlaygroundPage";
export { runPlayground } from "./api";
// P-12 : Story 2.8 (FR14). `shared/contracts/review.py` annonce la Story 5.4
// comme consommatrice de ces symboles, ils doivent être atteignables hors du
// dossier.
export { extraReviewKeys, isControllerReview } from "./reviewDetection";
// P-33 (Story 2.7, cf D-07) : usePlaygroundAssignedTools was missing from
// this barrel.
export { usePlaygroundRun, usePlaygroundAssignedTools } from "./hooks";
export type { PlaygroundAssignedTool } from "./hooks";
export type {
  ControllerReview,
  ControllerReviewVerdict,
  ReviewComment,
  ReviewSeverity,
  RunPlaygroundRequest,
  RunPlaygroundResponse,
  ToolInvocationLog,
  ToolInvocationStatus,
} from "./types";

/**
 * Best-effort shape detection for `ControllerReview` : Story 2.8 T4.1, AC4.
 *
 * `parsed_output` is never validated against a schema server-side (Story
 * 2.7 decision, "schema validation against output_contract deferred Story
 * 4.x"). This is a frontend-only, best-effort type guard (presence of the
 * expected keys), not a contract guarantee : an LLM answering with a
 * lookalike-but-malformed shape falls back to the raw-JSON view.
 */

import type {
  ControllerReview,
  ControllerReviewVerdict,
  ReviewComment,
  ReviewSeverity,
} from "./types";

const VALID_SEVERITIES: readonly ReviewSeverity[] = ["blocking", "suggestion", "question"];
const VALID_VERDICTS: readonly ControllerReviewVerdict[] = ["pass", "fail", "needs_fix"];

/** Keys the dedicated review view actually renders. Anything else on the object
 * is surfaced by `extraReviewKeys` rather than silently dropped (P-06). */
const KNOWN_REVIEW_KEYS: readonly string[] = ["comments", "verdict"];

function isNonEmptyString(value: unknown): value is string {
  return typeof value === "string" && value.length > 0;
}

function isReviewComment(value: unknown): value is ReviewComment {
  if (typeof value !== "object" || value === null) return false;
  const candidate = value as Record<string, unknown>;
  return (
    // P-06 : `location` and `message` are `min_length=1` in the Pydantic mirror
    // (`shared/contracts/review.py`). Accepting `""` here rendered an empty card
    // instead of falling back to the raw-JSON view.
    isNonEmptyString(candidate.location) &&
    typeof candidate.severity === "string" &&
    VALID_SEVERITIES.includes(candidate.severity as ReviewSeverity) &&
    isNonEmptyString(candidate.message) &&
    (candidate.suggested_fix === undefined ||
      candidate.suggested_fix === null ||
      typeof candidate.suggested_fix === "string")
  );
}

/** Keys present on a detected review that the dedicated view does not render.
 *
 * The backend `ControllerReview` is `extra="forbid"`, this guard is deliberately
 * not: an LLM enriching its answer (`summary`, `score`, ...) must still get the
 * review rendering. Returning the leftover keys lets the view show them instead
 * of hiding them behind the "Output brut" tab (P-06).
 */
export function extraReviewKeys(review: ControllerReview): string[] {
  return Object.keys(review).filter((key) => !KNOWN_REVIEW_KEYS.includes(key));
}

export function isControllerReview(parsed: unknown): parsed is ControllerReview {
  if (typeof parsed !== "object" || parsed === null) return false;
  const candidate = parsed as Record<string, unknown>;
  return (
    Array.isArray(candidate.comments) &&
    candidate.comments.every(isReviewComment) &&
    typeof candidate.verdict === "string" &&
    VALID_VERDICTS.includes(candidate.verdict as ControllerReviewVerdict)
  );
}

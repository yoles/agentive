/**
 * ControllerReviewView : Story 2.8 T4.3, AC4.
 *
 * Dedicated rendering for a `ControllerReview` shape detected in
 * `parsed_output` on the Playground's "Output parsé" tab : verdict badge +
 * comments grouped by severity (blocking first, then suggestion, then
 * question).
 */

import { extraReviewKeys } from "./reviewDetection";
import type { ControllerReview, ReviewComment, ReviewSeverity } from "./types";

// P-07 : uniquement des tokens du design system (globals.css `@theme`), pas de
// couleur Tailwind brute. Les valeurs brutes restaient en fond clair sur texte
// sombre en thème sombre, qui est le thème par défaut (UX-DR2).
const VERDICT_STYLES: Record<ControllerReview["verdict"], string> = {
  pass: "bg-status-success/15 text-status-success",
  fail: "bg-destructive/20 text-destructive",
  needs_fix: "bg-status-warning/15 text-status-warning",
};

const VERDICT_LABELS: Record<ControllerReview["verdict"], string> = {
  pass: "Pass",
  fail: "Fail",
  needs_fix: "Needs fix",
};

const SEVERITY_ORDER: readonly ReviewSeverity[] = ["blocking", "suggestion", "question"];

const SEVERITY_LABELS: Record<ReviewSeverity, string> = {
  blocking: "Bloquant",
  suggestion: "Suggestion",
  question: "Question",
};

const SEVERITY_STYLES: Record<ReviewSeverity, string> = {
  blocking: "bg-destructive/20 text-destructive",
  suggestion: "bg-primary/15 text-primary",
  question: "bg-muted text-muted-foreground",
};

function groupBySeverity(comments: ReviewComment[]): Map<ReviewSeverity, ReviewComment[]> {
  const groups = new Map<ReviewSeverity, ReviewComment[]>();
  for (const severity of SEVERITY_ORDER) {
    const inGroup = comments.filter((c) => c.severity === severity);
    if (inGroup.length > 0) groups.set(severity, inGroup);
  }
  return groups;
}

type Props = {
  review: ControllerReview;
};

export function ControllerReviewView({ review }: Props) {
  const groups = groupBySeverity(review.comments);
  const extraKeys = extraReviewKeys(review);

  return (
    <div className="flex flex-col gap-3" data-testid="controller-review-view">
      <div className="flex items-center gap-2">
        <span
          data-testid="controller-review-verdict"
          className={`rounded px-2 py-0.5 text-xs font-medium ${VERDICT_STYLES[review.verdict]}`}
        >
          {VERDICT_LABELS[review.verdict]}
        </span>
        <span className="text-xs text-muted-foreground">
          {review.comments.length} commentaire{review.comments.length > 1 ? "s" : ""}
        </span>
      </div>

      {[...groups.entries()].map(([severity, comments]) => (
        <div key={severity} data-testid={`controller-review-group-${severity}`}>
          <h4 className="mb-1 text-xs font-semibold text-muted-foreground">
            {SEVERITY_LABELS[severity]} ({comments.length})
          </h4>
          <ul className="flex flex-col gap-2">
            {comments.map((comment, idx) => (
              <li
                key={`${comment.location}-${idx}`}
                className="rounded border border-border bg-card p-3 text-xs"
              >
                <div className="flex items-center justify-between gap-2">
                  <span
                    className={`rounded px-2 py-0.5 text-xs ${SEVERITY_STYLES[comment.severity]}`}
                  >
                    {SEVERITY_LABELS[comment.severity]}
                  </span>
                  <span className="font-mono text-muted-foreground">{comment.location}</span>
                </div>
                <p className="mt-1">{comment.message}</p>
                {comment.suggested_fix && (
                  <p className="mt-1 text-muted-foreground">
                    <span className="font-medium">Correctif suggéré : </span>
                    {comment.suggested_fix}
                  </p>
                )}
              </li>
            ))}
          </ul>
        </div>
      ))}

      {extraKeys.length > 0 && (
        <details data-testid="controller-review-extra-keys" className="text-xs">
          <summary className="cursor-pointer text-muted-foreground">
            Champs supplémentaires ({extraKeys.length}) non rendus par cette vue
          </summary>
          <pre className="mt-1 max-h-64 overflow-auto rounded bg-muted p-3 whitespace-pre-wrap">
            {JSON.stringify(
              Object.fromEntries(
                extraKeys.map((key) => [key, (review as Record<string, unknown>)[key]]),
              ),
              null,
              2,
            )}
          </pre>
        </details>
      )}
    </div>
  );
}

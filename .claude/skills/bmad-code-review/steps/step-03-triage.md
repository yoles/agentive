---
---

# Step 3: Triage

## RULES

- YOU MUST ALWAYS SPEAK OUTPUT in your Agent communication style with the config `{communication_language}`
- Be precise. When uncertain between categories, prefer the more conservative classification.

## INSTRUCTIONS

1. **Normalize** findings into a common format. Expected input formats:
   - Adversarial (Blind Hunter): markdown list of descriptions
   - Edge Case Hunter: JSON array with `location`, `trigger_condition`, `guard_snippet`, `potential_consequence` fields
   - Acceptance Auditor: markdown list with title, AC/constraint reference, and evidence

   If a layer's output does not match its expected format, attempt best-effort parsing. Note any parsing issues for the user.

   Convert all to a unified list where each finding has:
   - `id` -- sequential integer
   - `source` -- `blind`, `edge`, `auditor`, or merged sources (e.g., `blind+edge`)
   - `title` -- one-line summary
   - `detail` -- full description
   - `location` -- file and line reference (if available)

2. **Deduplicate.** If two or more findings describe the same issue, merge them into one:
   - Use the most specific finding as the base (prefer edge-case JSON with location over adversarial prose).
   - Append any unique detail, reasoning, or location references from the other finding(s) into the surviving `detail` field.
   - Set `source` to the merged sources (e.g., `blind+edge`).

3. **Classify** each finding into exactly one bucket:
   - **intent_gap** -- The spec/intent is incomplete; cannot resolve from existing information. Only possible if `{review_mode}` = `"full"`.
   - **bad_spec** -- The spec should have prevented this; spec is wrong or ambiguous. Only possible if `{review_mode}` = `"full"`.
   - **patch** -- Code issue that is trivially fixable without human input. Just needs a code change.
   - **defer** -- Pre-existing issue not caused by the current change. Real but not actionable now.
     **A `defer` MUST name a carrier story.** See the two rules below; a finding you
     cannot give a carrier to is not a `defer`, it is a `patch` or a `reject`.
   - **reject** -- Noise, false positive, or handled elsewhere.

   If `{review_mode}` = `"no-spec"` and a finding would otherwise be `intent_gap` or `bad_spec`, reclassify it as `patch` (if code-fixable) or `defer` (if not).

3b. **Assign a carrier story to every `defer`** (mandatory -- a review cannot close without this).

   Debt consigned anywhere other than a story is debt that will be lost. This is not a
   hypothesis: this project lost the `wrap_external_input` gap for two full epics because it
   lived in one sentence of another story's Change Log, and lost four `defer` (D1-D4) for three
   days because they lived only in a review report. Both were found by accident, by a later
   review, not by the process.

   For each `defer`, record `carrier` = an existing key in `sprint-status.yaml`
   (e.g. `4-15-completude-purge-inventaire-tenant`), or the key of a story you create now.

   **Rule A -- a carrier is a story, never a document.** A Change Log entry, a runbook
   paragraph, an ADR or this review report are NOT carriers. Only a `sprint-status.yaml` key is.

   **Rule B -- depth 2: a hardening story's `defer` leaves its epic.**
   Determine whether the story under review is a *hardening story* -- one whose stated purpose is
   to close `defer` findings from earlier stories, rather than to deliver a product capability
   (its create-story note will say so explicitly).

   - Story under review is a **feature story** -> its `defer` MAY take a carrier inside the same
     epic. This is the legitimate first generation of debt, and it is part of finishing the epic.
   - Story under review is a **hardening story** -> its `defer` MUST take a carrier **outside**
     that epic (a transverse epic, or a later one). Never back into the same epic.

   Why: without Rule B there is no fixed point. Every review produces `defer`, every `defer`
   becomes a story in the same epic, every story calls a review. Epic 4 grew from 7 planned
   stories to 15 this way and could only be closed by moving its last second-generation debt out
   (Story 9.8 -> Epic 9). Rule B makes that move the default instead of a one-off judgement call,
   and gives the epic a stable closing condition: **an epic closes when the hardening story of
   its last feature story is done.**

   Precedents to follow rather than re-derive: Story 9.7 (an Epic 2 debt placed in Epic 9 rather
   than reopening a `done` epic) and Story 9.8 (the same, from Epic 4).

4. **Drop** all `reject` findings. Record the reject count for the summary.

5. If `{failed_layers}` is non-empty, report which layers failed before announcing results. If zero findings remain after dropping rejects AND `{failed_layers}` is non-empty, warn the user that the review may be incomplete rather than announcing a clean review.

6. If zero findings remain after dropping rejects and no layers failed, note clean review.


## NEXT

Read fully and follow `./step-04-present.md`

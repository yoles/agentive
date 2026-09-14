---
---

# Step 4: Present

## RULES

- YOU MUST ALWAYS SPEAK OUTPUT in your Agent communication style with the config `{communication_language}`
- Do NOT auto-fix anything. Present findings and let the user decide next steps.

## INSTRUCTIONS

1. Group remaining findings by category.

2. Present to the user in this order (include a section only if findings exist in that category):

   - **Intent Gaps**: "These findings suggest the captured intent is incomplete. Consider clarifying intent before proceeding."
     - List each with title + detail.

   - **Bad Spec**: "These findings suggest the spec should be amended. Consider regenerating or amending the spec with this context:"
     - List each with title + detail + suggested spec amendment.

   - **Patch**: "These are fixable code issues:"
     - List each with title + detail + location (if available).

   - **Defer**: "Pre-existing issues surfaced by this review (not caused by current changes):"
     - List each with title + detail + **its carrier story key** (assigned in step 3b).

     **CLOSING GATE -- do not skip.** A review MUST NOT be reported as complete while any
     `defer` has no carrier story. If one does, stop and resolve it first: either create the
     carrier story now (and register it in `sprint-status.yaml`), or reclassify the finding as
     `patch` / `reject` with a reason. Reporting "3 defer consigned" without a carrier per
     finding is precisely how this project lost debt twice -- once for two epics, once for three
     days -- and both times it was found by accident rather than by the process.

     Also verify Rule B held: if the reviewed story is a hardening story, every carrier must sit
     OUTSIDE its epic. A carrier inside the same epic means the epic has no stable closing
     condition.

3. Summary line: **X** intent_gap, **Y** bad_spec, **Z** patch, **W** defer findings. **R** findings rejected as noise.
   When `W > 0`, the line MUST also name the carrier story of each `defer`.

4. If clean review (zero findings across all layers after triage): state that N findings were raised but all were classified as noise, or that no findings were raised at all (as applicable).

5. Offer the user next steps (recommendations, not automated actions):
   - If `patch` findings exist: "These can be addressed in a follow-up implementation pass or manually."
   - If `intent_gap` or `bad_spec` findings exist: "Consider running the planning workflow to clarify intent or amend the spec before continuing."
   - If only `defer` findings remain: "No action needed for this change. Deferred items are noted for future attention."

Workflow complete.

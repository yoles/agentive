/**
 * Component tests : OutputInspector.
 *
 * Créé par la Story 2.7 (T8.3, P-04 fix-batch 2026-08-31), étendu par la
 * Story 2.8 (T4.4, AC4) avec 3 tests de détection/rendu de ControllerReview.
 */

import { afterEach, describe, expect, it } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { OutputInspector } from "./OutputInspector";
import type { RunPlaygroundResponse } from "./types";

function makeResult(overrides: Partial<RunPlaygroundResponse> = {}): RunPlaygroundResponse {
  return {
    prompt_resolved: "you are useful",
    raw_output: '{"summary": "hi"}',
    parsed_output: { summary: "hi" },
    tokens: { input_tokens: 42, output_tokens: 84 },
    cost_estimate_usd: "0.000123",
    model_used: "claude-sonnet-4-6",
    provider_used: "anthropic",
    tool_invocations: [],
    duration_ms_total: 123,
    ...overrides,
  };
}

describe("OutputInspector", () => {
  afterEach(() => cleanup());

  it("shows the empty state when no result and no error", () => {
    render(<OutputInspector result={undefined} isPending={false} error={undefined} />);
    expect(screen.getByTestId("playground-output-empty")).toBeInTheDocument();
  });

  it("shows the loading state while pending", () => {
    render(<OutputInspector result={undefined} isPending={true} error={undefined} />);
    expect(screen.getByTestId("playground-output-loading")).toBeInTheDocument();
  });

  it("switches tabs and renders tab-specific content", () => {
    render(
      <OutputInspector result={makeResult()} isPending={false} error={undefined} />,
    );
    // Default tab = raw.
    expect(screen.getByTestId("playground-tab-raw-content")).toHaveTextContent(
      '{"summary": "hi"}',
    );
    fireEvent.click(screen.getByTestId("playground-tab-prompt"));
    expect(screen.getByTestId("playground-tab-prompt-content")).toHaveTextContent(
      "you are useful",
    );
    fireEvent.click(screen.getByTestId("playground-tab-parsed"));
    expect(screen.getByTestId("playground-tab-parsed-content")).toHaveTextContent(
      '"summary": "hi"',
    );
  });

  it("displays input/output tokens and cost in the tokens tab", () => {
    render(
      <OutputInspector result={makeResult()} isPending={false} error={undefined} />,
    );
    fireEvent.click(screen.getByTestId("playground-tab-tokens"));
    const tokens = screen.getByTestId("playground-tab-tokens-content");
    expect(tokens).toHaveTextContent("42");
    expect(tokens).toHaveTextContent("84");
    expect(tokens).toHaveTextContent("0.000123");
  });

  it("renders tool invocations in chronological (array) order", () => {
    render(
      <OutputInspector
        result={makeResult({
          tool_invocations: [
            {
              tool_id: "t1",
              tool_name: "echo",
              server_id: "s1",
              arguments_redacted: {},
              result_summary: "first",
              duration_ms: 10,
              status: "success",
            },
            {
              tool_id: "t2",
              tool_name: "add",
              server_id: "s1",
              arguments_redacted: {},
              result_summary: "second",
              duration_ms: 20,
              status: "error",
            },
          ],
        })}
        isPending={false}
        error={undefined}
      />,
    );
    fireEvent.click(screen.getByTestId("playground-tab-tools"));
    const names = screen
      .getByTestId("playground-tab-tools-content")
      .querySelectorAll("li span.font-medium");
    expect(names).toHaveLength(2);
    expect(names[0]).toHaveTextContent("echo");
    expect(names[1]).toHaveTextContent("add");
  });

  it("shows an accurate message (not 'invalid JSON') when parsed_output is a non-object", () => {
    render(
      <OutputInspector
        result={makeResult({ parsed_output: null })}
        isPending={false}
        error={undefined}
      />,
    );
    fireEvent.click(screen.getByTestId("playground-tab-parsed"));
    // P-21 — the raw output MAY be valid JSON that just isn't an object
    // (array/scalar); the copy must not claim it is invalid JSON.
    expect(screen.getByTestId("playground-tab-parsed-content")).not.toHaveTextContent(
      "n'est pas un objet JSON valide",
    );
  });

  it("detects a ControllerReview shape in parsed_output and renders the verdict badge (Story 2.8 AC4)", () => {
    render(
      <OutputInspector
        result={makeResult({
          parsed_output: {
            verdict: "needs_fix",
            comments: [
              {
                location: "backend/src/foo.py:42",
                severity: "blocking",
                message: "Nom ambigu.",
                suggested_fix: "Renommer en `elapsed_ms`.",
              },
            ],
          },
        })}
        isPending={false}
        error={undefined}
      />,
    );
    fireEvent.click(screen.getByTestId("playground-tab-parsed"));
    expect(screen.getByTestId("controller-review-verdict")).toHaveTextContent("Needs fix");
    expect(screen.getByText("Renommer en `elapsed_ms`.", { exact: false })).toBeInTheDocument();
  });

  it("groups ControllerReview comments by severity, blocking first (Story 2.8 AC4)", () => {
    render(
      <OutputInspector
        result={makeResult({
          parsed_output: {
            verdict: "fail",
            comments: [
              {
                location: "field.name",
                severity: "question",
                message: "Pourquoi ce choix ?",
              },
              {
                location: "backend/src/foo.py:42",
                severity: "blocking",
                message: "Nom ambigu.",
              },
            ],
          },
        })}
        isPending={false}
        error={undefined}
      />,
    );
    fireEvent.click(screen.getByTestId("playground-tab-parsed"));
    const groups = screen
      .getByTestId("playground-tab-parsed-content")
      .querySelectorAll("[data-testid^='controller-review-group-']");
    expect(groups).toHaveLength(2);
    expect(groups[0]).toHaveAttribute("data-testid", "controller-review-group-blocking");
    expect(groups[1]).toHaveAttribute("data-testid", "controller-review-group-question");
  });

  it("falls back to raw JSON when a comment has an empty location or message (P-06)", () => {
    render(
      <OutputInspector
        result={makeResult({
          parsed_output: {
            verdict: "fail",
            comments: [{ location: "", severity: "blocking", message: "" }],
          },
        })}
        isPending={false}
        error={undefined}
      />,
    );
    fireEvent.click(screen.getByTestId("playground-tab-parsed"));
    // The Pydantic mirror is `min_length=1` on both fields, so an empty card is
    // not a review: the raw-JSON view must win rather than render a blank entry.
    expect(screen.queryByTestId("controller-review-view")).not.toBeInTheDocument();
    expect(screen.getByTestId("playground-tab-parsed-content")).toHaveTextContent('"verdict"');
  });

  it("surfaces review keys the dedicated view does not render instead of hiding them (P-06)", () => {
    render(
      <OutputInspector
        result={makeResult({
          parsed_output: {
            verdict: "pass",
            comments: [],
            summary: "Rien à signaler.",
            score: 0.92,
          },
        })}
        isPending={false}
        error={undefined}
      />,
    );
    fireEvent.click(screen.getByTestId("playground-tab-parsed"));
    expect(screen.getByTestId("controller-review-view")).toBeInTheDocument();
    const extras = screen.getByTestId("controller-review-extra-keys");
    expect(extras).toHaveTextContent("Rien à signaler.");
    expect(extras).toHaveTextContent("0.92");
  });

  it("does not render the extra-keys block for a review with no extra key (P-06)", () => {
    render(
      <OutputInspector
        result={makeResult({
          parsed_output: {
            verdict: "pass",
            comments: [{ location: "x", severity: "question", message: "ok ?" }],
          },
        })}
        isPending={false}
        error={undefined}
      />,
    );
    fireEvent.click(screen.getByTestId("playground-tab-parsed"));
    expect(screen.queryByTestId("controller-review-extra-keys")).not.toBeInTheDocument();
  });

  it("falls back to raw JSON when parsed_output does not match ControllerReview (non-regression Story 2.7)", () => {
    render(
      <OutputInspector
        result={makeResult({ parsed_output: { summary: "hi" } })}
        isPending={false}
        error={undefined}
      />,
    );
    fireEvent.click(screen.getByTestId("playground-tab-parsed"));
    expect(screen.queryByTestId("controller-review-view")).not.toBeInTheDocument();
    expect(screen.getByTestId("playground-tab-parsed-content")).toHaveTextContent(
      '"summary": "hi"',
    );
  });

  it("reads .detail from an RFC 7807 ApiError instead of showing 'undefined' (P-14)", () => {
    const apiError = {
      type: "/errors/validation",
      title: "Validation error",
      status: 422,
      detail: "system_prompt references variable 'topic' not present in arguments",
    };
    render(
      <OutputInspector
        result={undefined}
        isPending={false}
        error={apiError as unknown as Error}
      />,
    );
    expect(screen.getByTestId("playground-output-error")).toHaveTextContent(
      "system_prompt references variable 'topic' not present in arguments",
    );
  });
});

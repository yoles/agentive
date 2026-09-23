/**
 * OutputInspector — Story 2.7 T7.7.
 *
 * Tabs : Prompt | Raw Output | Parsed Output | Tool Invocations | Tokens & Cost.
 * Sprint 1 : no syntax highlighting (D81 défer Sprint 4+) ; plain
 * `<pre><code>` for code blocks.
 */

import { useState } from "react";
import type { ApiError } from "@/shared/api/client";
import { ControllerReviewView } from "./ControllerReviewView";
import { isControllerReview } from "./reviewDetection";
import type { RunPlaygroundResponse } from "./types";

/** P-14 — `apiFetch` always rejects with an `ApiError` shape (RFC 7807:
 * `.detail`/`.title`), never a real `Error` with `.message`. Reading
 * `.message` on that object is always `undefined`. */
function errorMessage(error: Error): string {
  const apiError = error as unknown as Partial<ApiError>;
  return apiError.detail ?? apiError.title ?? error.message ?? "Erreur inconnue.";
}

type TabId = "prompt" | "raw" | "parsed" | "tools" | "tokens";

const TABS: { id: TabId; label: string }[] = [
  { id: "prompt", label: "Prompt résolu" },
  { id: "raw", label: "Output brut" },
  { id: "parsed", label: "Output parsé" },
  { id: "tools", label: "Tool invocations" },
  { id: "tokens", label: "Tokens & coût" },
];

type Props = {
  result: RunPlaygroundResponse | undefined;
  isPending: boolean;
  error: Error | undefined;
};

export function OutputInspector({ result, isPending, error }: Props) {
  const [activeTab, setActiveTab] = useState<TabId>("raw");

  if (isPending) {
    return (
      <section
        className="rounded-md border border-border bg-muted/30 p-6 text-sm text-muted-foreground"
        data-testid="playground-output-loading"
      >
        Exécution en cours…
      </section>
    );
  }

  if (error) {
    return (
      <section
        className="rounded-md border border-destructive/40 bg-destructive/5 p-6 text-sm text-destructive"
        data-testid="playground-output-error"
      >
        <h3 className="font-semibold">Erreur</h3>
        <p className="mt-1 text-xs">{errorMessage(error)}</p>
      </section>
    );
  }

  if (!result) {
    return (
      <section
        className="rounded-md border border-dashed border-border bg-muted/10 p-6 text-sm text-muted-foreground"
        data-testid="playground-output-empty"
      >
        Lancez une exécution pour voir le résultat.
      </section>
    );
  }

  return (
    <section
      className="flex flex-col gap-3"
      data-testid="playground-output-result"
      aria-labelledby="playground-output-heading"
    >
      <header className="flex items-center justify-between">
        <h3 id="playground-output-heading" className="text-sm font-semibold">
          Résultat
        </h3>
        <span className="text-xs text-muted-foreground">
          {result.duration_ms_total} ms — {result.provider_used} / {result.model_used}
        </span>
      </header>

      <div className="flex gap-1 border-b border-border" role="tablist">
        {TABS.map((tab) => (
          <button
            key={tab.id}
            type="button"
            role="tab"
            aria-selected={activeTab === tab.id}
            data-testid={`playground-tab-${tab.id}`}
            onClick={() => setActiveTab(tab.id)}
            className={`px-3 py-1.5 text-xs font-medium transition-colors ${
              activeTab === tab.id
                ? "border-b-2 border-primary text-foreground"
                : "text-muted-foreground hover:text-foreground"
            }`}
          >
            {tab.label}
          </button>
        ))}
      </div>

      {activeTab === "prompt" && (
        <pre
          className="max-h-96 overflow-auto rounded bg-muted p-3 text-xs whitespace-pre-wrap"
          data-testid="playground-tab-prompt-content"
        >
          {result.prompt_resolved}
        </pre>
      )}

      {activeTab === "raw" && (
        <pre
          className="max-h-96 overflow-auto rounded bg-muted p-3 text-xs whitespace-pre-wrap"
          data-testid="playground-tab-raw-content"
        >
          {result.raw_output}
        </pre>
      )}

      {activeTab === "parsed" && (
        <div data-testid="playground-tab-parsed-content">
          {result.parsed_output === null ? (
            <p className="rounded border border-amber-500/40 bg-amber-500/10 p-3 text-xs text-amber-700">
              Parsing échoué : l'output brut n'est pas un objet JSON (JSON invalide, ou un
              tableau / une valeur scalaire au lieu d'un objet).
            </p>
          ) : isControllerReview(result.parsed_output) ? (
            <ControllerReviewView review={result.parsed_output} />
          ) : (
            <pre className="max-h-96 overflow-auto rounded bg-muted p-3 text-xs whitespace-pre-wrap">
              {JSON.stringify(result.parsed_output, null, 2)}
            </pre>
          )}
        </div>
      )}

      {activeTab === "tools" && (
        <div data-testid="playground-tab-tools-content">
          {result.tool_invocations.length === 0 ? (
            <p className="text-xs text-muted-foreground">
              Aucune invocation d'outil pendant ce run.
            </p>
          ) : (
            <ul className="flex flex-col gap-2">
              {result.tool_invocations.map((inv, idx) => (
                <li
                  key={`${inv.tool_id}-${idx}`}
                  className="rounded border border-border bg-card p-3 text-xs"
                >
                  <div className="flex items-center justify-between">
                    <span className="font-medium">{inv.tool_name}</span>
                    <span
                      className={`rounded px-2 py-0.5 text-xs ${
                        inv.status === "success"
                          ? "bg-green-100 text-green-800"
                          : "bg-destructive/20 text-destructive"
                      }`}
                    >
                      {inv.status}
                    </span>
                  </div>
                  <p className="mt-1 text-muted-foreground">
                    {inv.duration_ms} ms — {inv.result_summary}
                  </p>
                </li>
              ))}
            </ul>
          )}
        </div>
      )}

      {activeTab === "tokens" && (
        <dl
          className="grid grid-cols-2 gap-2 rounded bg-muted p-3 text-xs"
          data-testid="playground-tab-tokens-content"
        >
          <dt className="font-medium">Tokens d'entrée</dt>
          <dd>{result.tokens.input_tokens}</dd>
          <dt className="font-medium">Tokens de sortie</dt>
          <dd>{result.tokens.output_tokens}</dd>
          <dt className="font-medium">Coût estimé (USD)</dt>
          <dd>{result.cost_estimate_usd ?? "—"}</dd>
          <dt className="font-medium">Modèle</dt>
          <dd>{result.model_used}</dd>
          <dt className="font-medium">Provider</dt>
          <dd>{result.provider_used}</dd>
        </dl>
      )}
    </section>
  );
}

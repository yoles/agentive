/**
 * AgentTesterForm — Story 2.7 T7.6.
 *
 * Sprint 1 — input UX = JSON textarea (D76 form builder dynamique
 * depuis input_contract défer Story 4.x). Tool subset toggle via
 * Story 2.5 ``useAgentTools(templateId)`` + checkboxes.
 */

import { useMemo, useState } from "react";
import { Button } from "@/shared/components/ui/button";
import { Checkbox } from "@/shared/components/ui/checkbox";
import { usePlaygroundAssignedTools } from "./hooks";
import type { RunPlaygroundRequest } from "./types";

type Props = {
  templateId: string;
  isRunning: boolean;
  onSubmit: (req: RunPlaygroundRequest) => void;
};

export function AgentTesterForm({ templateId, isRunning, onSubmit }: Props) {
  const assignedQuery = usePlaygroundAssignedTools(templateId);
  const [argumentsJson, setArgumentsJson] = useState<string>("{}");
  const [jsonError, setJsonError] = useState<string | null>(null);
  const [timeoutSeconds, setTimeoutSeconds] = useState<number>(30);

  const assignedTools = useMemo(
    () => assignedQuery.data?.assigned_tools ?? [],
    [assignedQuery.data],
  );
  const [enabledToolIds, setEnabledToolIds] = useState<Set<string> | null>(null);
  // null = all enabled (matches backend semantic) ; non-null = explicit subset.

  function toggleTool(toolId: string) {
    setEnabledToolIds((prev) => {
      const current = prev ?? new Set(assignedTools.map((t) => t.tool_id));
      const next = new Set(current);
      if (next.has(toolId)) {
        next.delete(toolId);
      } else {
        next.add(toolId);
      }
      return next;
    });
  }

  function handleSubmit(event: React.FormEvent) {
    event.preventDefault();
    let parsed: Record<string, unknown>;
    try {
      parsed = JSON.parse(argumentsJson) as Record<string, unknown>;
      if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
        throw new Error("Doit être un objet JSON");
      }
      setJsonError(null);
    } catch (err) {
      setJsonError(err instanceof Error ? err.message : "JSON invalide");
      return;
    }
    onSubmit({
      arguments: parsed,
      enabled_tool_ids:
        enabledToolIds === null ? null : Array.from(enabledToolIds),
      timeout_seconds: timeoutSeconds,
    });
  }

  return (
    <form
      onSubmit={handleSubmit}
      className="flex flex-col gap-4"
      data-testid="playground-tester-form"
    >
      <div>
        <label
          htmlFor="playground-arguments-json"
          className="text-sm font-medium"
        >
          Arguments (JSON)
        </label>
        <p className="mt-1 text-xs text-muted-foreground">
          Variables référencées dans le system_prompt (notation <code>{"{key}"}</code>).
        </p>
        <textarea
          id="playground-arguments-json"
          data-testid="playground-arguments-textarea"
          value={argumentsJson}
          onChange={(e) => setArgumentsJson(e.target.value)}
          rows={8}
          className="mt-1 w-full rounded border border-border bg-card p-2 font-mono text-xs"
          spellCheck={false}
        />
        {jsonError && (
          <p
            className="mt-1 text-xs text-destructive"
            data-testid="playground-json-error"
          >
            {jsonError}
          </p>
        )}
      </div>

      <div>
        <label
          htmlFor="playground-timeout"
          className="text-sm font-medium"
        >
          Timeout (secondes)
        </label>
        <input
          id="playground-timeout"
          data-testid="playground-timeout-input"
          type="number"
          min={1}
          max={120}
          step={1}
          value={timeoutSeconds}
          onChange={(e) => setTimeoutSeconds(Number(e.target.value))}
          className="mt-1 w-24 rounded border border-border bg-card p-2 text-xs"
        />
      </div>

      <div>
        <p className="text-sm font-medium">Outils activés</p>
        {assignedTools.length === 0 ? (
          <p className="mt-1 text-xs text-muted-foreground">
            Aucun outil assigné à cet agent.
          </p>
        ) : (
          <ul className="mt-2 flex flex-col gap-1">
            {assignedTools.map((tool) => {
              const isChecked =
                enabledToolIds === null
                  ? true
                  : enabledToolIds.has(tool.tool_id);
              return (
                <li key={tool.tool_id} className="flex items-center gap-2">
                  <Checkbox
                    id={`playground-tool-${tool.tool_id}`}
                    checked={isChecked}
                    onCheckedChange={() => toggleTool(tool.tool_id)}
                    data-testid={`playground-tool-${tool.tool_id}`}
                  />
                  <label
                    htmlFor={`playground-tool-${tool.tool_id}`}
                    className="cursor-pointer text-xs"
                  >
                    {tool.name}
                  </label>
                </li>
              );
            })}
          </ul>
        )}
      </div>

      <Button
        type="submit"
        disabled={isRunning}
        data-testid="playground-run-button"
      >
        {isRunning ? "Exécution…" : "Lancer le run"}
      </Button>
    </form>
  );
}

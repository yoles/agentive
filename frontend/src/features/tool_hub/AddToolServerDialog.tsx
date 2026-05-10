/**
 * `AddToolServerDialog` — Story 2.5 AC7.
 *
 * Modal form to register a new MCP server. Validates the JSON
 * `connection_config` client-side via `JSON.parse` before sending.
 * On success closes the dialog + invalidates the tool-servers list.
 */

import { useState } from "react";
import { toast } from "sonner";
import { Button } from "@/shared/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/shared/components/ui/dialog";
import { Input } from "@/shared/components/ui/input";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/shared/components/ui/select";
import { Textarea } from "@/shared/components/ui/textarea";
import { useCreateToolServer } from "./hooks";
import type { Transport } from "./types";

type Props = {
  open: boolean;
  onOpenChange: (open: boolean) => void;
};

const STDIO_TEMPLATE = JSON.stringify(
  { command: "python", args: ["-m", "my.mcp.server"] },
  null,
  2,
);
const SSE_TEMPLATE = JSON.stringify(
  { url: "https://example.com/mcp" },
  null,
  2,
);

export function AddToolServerDialog({ open, onOpenChange }: Props) {
  const [name, setName] = useState("");
  const [transport, setTransport] = useState<Transport>("stdio");
  const [configRaw, setConfigRaw] = useState(STDIO_TEMPLATE);
  const [error, setError] = useState<string | null>(null);
  const createMutation = useCreateToolServer();

  function reset() {
    setName("");
    setTransport("stdio");
    setConfigRaw(STDIO_TEMPLATE);
    setError(null);
  }

  function handleTransportChange(value: string) {
    const t = value as Transport;
    setTransport(t);
    // Re-fill the config textarea with a template for the new transport
    // ONLY if the user hasn't deviated from the previous template.
    if (configRaw === STDIO_TEMPLATE && t === "sse") setConfigRaw(SSE_TEMPLATE);
    else if (configRaw === SSE_TEMPLATE && t === "stdio") setConfigRaw(STDIO_TEMPLATE);
  }

  async function handleSubmit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError(null);

    let parsed: Record<string, unknown>;
    try {
      parsed = JSON.parse(configRaw);
    } catch (e) {
      setError(`JSON invalide : ${(e as Error).message}`);
      return;
    }
    if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
      setError("connection_config doit être un objet JSON.");
      return;
    }

    try {
      const detail = await createMutation.mutateAsync({
        name: name.trim(),
        transport,
        connection_config: parsed,
      });
      toast.success(`Serveur connecté (${detail.tools.length} outils découverts)`);
      reset();
      onOpenChange(false);
    } catch (err) {
      const apiError = err as { status?: number; detail?: string; title?: string };
      if (apiError.status === 409) {
        toast.error("Un serveur avec ce nom existe déjà.");
      } else if (apiError.status === 503) {
        toast.error("Le serveur MCP n'a pas répondu dans les 10 secondes.");
      } else {
        toast.error(apiError.detail ?? apiError.title ?? "Échec de la connexion.");
      }
    }
  }

  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        if (!next) reset();
        onOpenChange(next);
      }}
    >
      <DialogContent className="sm:max-w-lg" data-testid="add-tool-server-dialog">
        <DialogHeader>
          <DialogTitle>Ajouter un serveur MCP</DialogTitle>
          <DialogDescription>
            Connectez un serveur MCP (stdio ou SSE). Ses outils seront découverts
            automatiquement et stockés dans le registry.
          </DialogDescription>
        </DialogHeader>

        <form onSubmit={handleSubmit} className="flex flex-col gap-4" noValidate>
          <div className="flex flex-col gap-2">
            <label htmlFor="server-name" className="text-sm font-medium">
              Nom
            </label>
            <Input
              id="server-name"
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="my-mcp-server"
              required
              maxLength={255}
              data-testid="add-server-name"
            />
          </div>

          <div className="flex flex-col gap-2">
            <label htmlFor="server-transport" className="text-sm font-medium">
              Transport
            </label>
            <Select value={transport} onValueChange={handleTransportChange}>
              <SelectTrigger id="server-transport" data-testid="add-server-transport">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="stdio">stdio (subprocess)</SelectItem>
                <SelectItem value="sse">SSE (HTTP)</SelectItem>
              </SelectContent>
            </Select>
          </div>

          <div className="flex flex-col gap-2">
            <label htmlFor="server-config" className="text-sm font-medium">
              connection_config (JSON)
            </label>
            <Textarea
              id="server-config"
              rows={6}
              value={configRaw}
              onChange={(e) => setConfigRaw(e.target.value)}
              className="font-mono text-xs"
              data-testid="add-server-config"
            />
            <p className="text-xs text-muted-foreground">
              {transport === "stdio"
                ? "Format : { \"command\": str, \"args\": list[str], \"env\"?: dict }"
                : "Format : { \"url\": str, \"headers\"?: dict }"}
            </p>
            {error && (
              <p className="text-xs text-destructive" role="alert">
                {error}
              </p>
            )}
          </div>

          <DialogFooter>
            <Button
              type="button"
              variant="outline"
              onClick={() => onOpenChange(false)}
              disabled={createMutation.isPending}
            >
              Annuler
            </Button>
            <Button
              type="submit"
              disabled={createMutation.isPending || name.trim().length === 0}
              data-testid="add-server-submit"
            >
              {createMutation.isPending ? "Connexion…" : "Connecter"}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

/**
 * `CreateNamespaceDialog` — Story 3.2 AC1.
 *
 * Modal form to create a namespace (name/type/department/project). No
 * `retention_policy` override field — the per-type default (Story 3.2 AC1)
 * covers the MVP need; an explicit override is reachable via the API but
 * not exposed here to keep the form minimal.
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
import { useCreateNamespace } from "./hooks";
import type { EmbeddingBackend, NamespaceType } from "./types";

type Props = {
  open: boolean;
  onOpenChange: (open: boolean) => void;
};

const NAMESPACE_TYPE_LABELS: Record<NamespaceType, string> = {
  client: "Client",
  metier: "Métier",
  operationnelle: "Opérationnelle",
  contextuelle: "Contextuelle",
};

// Story 3.6 AC2/AC3 — `cloud` first/default: unchanged pre-3.6 behaviour.
const EMBEDDING_BACKEND_LABELS: Record<EmbeddingBackend, string> = {
  cloud: "Cloud (OpenAI, par défaut)",
  local: "Local (FastEmbed)",
  voyage: "Voyage",
};

export function CreateNamespaceDialog({ open, onOpenChange }: Props) {
  const [name, setName] = useState("");
  const [type, setType] = useState<NamespaceType>("metier");
  const [department, setDepartment] = useState("");
  const [project, setProject] = useState("");
  const [embeddingBackend, setEmbeddingBackend] = useState<EmbeddingBackend>("cloud");
  const createMutation = useCreateNamespace();

  function reset() {
    setName("");
    setType("metier");
    setDepartment("");
    setProject("");
    setEmbeddingBackend("cloud");
  }

  /** Escape/overlay-dismiss go through `Dialog`'s `onOpenChange`, which

   * resets the draft. The "Annuler" button used to call `onOpenChange`
   * directly and skip that reset, leaving the draft behind — the two
   * dismiss paths must share this one function (code review Story 3.2,
   * P8). */
  function handleCancel() {
    reset();
    onOpenChange(false);
  }

  async function handleSubmit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();

    try {
      await createMutation.mutateAsync({
        name: name.trim(),
        type,
        department: department.trim() || null,
        project: project.trim() || null,
        embedding_backend: embeddingBackend,
      });
      toast.success(`Namespace "${name.trim()}" créé.`);
      reset();
      onOpenChange(false);
    } catch (err) {
      const apiError = err as { status?: number; detail?: string; title?: string };
      if (apiError.status === 409) {
        toast.error("Un namespace avec ce nom existe déjà.");
      } else {
        toast.error(apiError.detail ?? apiError.title ?? "Échec de la création.");
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
      <DialogContent className="sm:max-w-lg" data-testid="create-namespace-dialog">
        <DialogHeader>
          <DialogTitle>Créer un namespace</DialogTitle>
          <DialogDescription>
            Un namespace isole la mémoire par département/projet. La rétention par
            défaut dépend du type choisi (7j contextuelle, 90j opérationnelle, 365j
            métier, illimité client).
          </DialogDescription>
        </DialogHeader>

        <form onSubmit={handleSubmit} className="flex flex-col gap-4" noValidate>
          <div className="flex flex-col gap-2">
            <label htmlFor="namespace-name" className="text-sm font-medium">
              Nom
            </label>
            <Input
              id="namespace-name"
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="dev-notes"
              required
              maxLength={255}
              data-testid="create-namespace-name"
            />
          </div>

          <div className="flex flex-col gap-2">
            <label htmlFor="namespace-type" className="text-sm font-medium">
              Type
            </label>
            <Select value={type} onValueChange={(value) => setType(value as NamespaceType)}>
              <SelectTrigger id="namespace-type" data-testid="create-namespace-type">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {(Object.keys(NAMESPACE_TYPE_LABELS) as NamespaceType[]).map((t) => (
                  <SelectItem key={t} value={t}>
                    {NAMESPACE_TYPE_LABELS[t]}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>

          <div className="flex flex-col gap-2">
            <label htmlFor="namespace-embedding-backend" className="text-sm font-medium">
              Backend d'embedding
            </label>
            <Select
              value={embeddingBackend}
              onValueChange={(value) => setEmbeddingBackend(value as EmbeddingBackend)}
            >
              <SelectTrigger
                id="namespace-embedding-backend"
                data-testid="create-namespace-embedding-backend"
              >
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {(Object.keys(EMBEDDING_BACKEND_LABELS) as EmbeddingBackend[]).map((backend) => (
                  <SelectItem key={backend} value={backend}>
                    {EMBEDDING_BACKEND_LABELS[backend]}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>

          <div className="flex flex-col gap-2">
            <label htmlFor="namespace-department" className="text-sm font-medium">
              Département (optionnel)
            </label>
            <Input
              id="namespace-department"
              value={department}
              onChange={(e) => setDepartment(e.target.value)}
              placeholder="Dev"
              maxLength={100}
              data-testid="create-namespace-department"
            />
          </div>

          <div className="flex flex-col gap-2">
            <label htmlFor="namespace-project" className="text-sm font-medium">
              Projet (optionnel)
            </label>
            <Input
              id="namespace-project"
              value={project}
              onChange={(e) => setProject(e.target.value)}
              placeholder="agentive"
              maxLength={100}
              data-testid="create-namespace-project"
            />
          </div>

          <DialogFooter>
            <Button
              type="button"
              variant="outline"
              onClick={handleCancel}
              disabled={createMutation.isPending}
            >
              Annuler
            </Button>
            <Button
              type="submit"
              disabled={createMutation.isPending || name.trim().length === 0}
              data-testid="create-namespace-submit"
            >
              {createMutation.isPending ? "Création…" : "Créer"}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

/**
 * `PurgeChunksDialog` — Story 3.6 AC5.
 *
 * First destructive-confirmation pattern in this frontend (no
 * `AlertDialog`/confirmation dialog exists anywhere else yet) — built on
 * the existing `Dialog` primitive with a `variant="destructive"` confirm
 * button, per this story's Dev Notes.
 *
 * Confirming loops `DELETE /memory/chunks/{id}` once per selected chunk via
 * `Promise.allSettled` (never `Promise.all`): a chunk already purged by
 * another tab/session between selection and confirmation must not fail the
 * whole batch — each failure is reported individually via `toast`.
 */

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
import { usePurgeChunk } from "./hooks";

type Props = {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  namespace: string;
  chunkIds: string[];
  /** Called after the batch settles (whatever the outcome) so the caller
   * can clear its selection. */
  onSettled: () => void;
};

export function PurgeChunksDialog({ open, onOpenChange, namespace, chunkIds, onSettled }: Props) {
  const purgeMutation = usePurgeChunk(namespace);

  async function handleConfirm() {
    const results = await Promise.allSettled(
      chunkIds.map((chunkId) => purgeMutation.mutateAsync(chunkId)),
    );
    const failures = results.filter((r) => r.status === "rejected").length;
    const succeeded = results.length - failures;

    if (succeeded > 0) {
      toast.success(`${succeeded} chunk${succeeded > 1 ? "s" : ""} purgé${succeeded > 1 ? "s" : ""}.`);
    }
    if (failures > 0) {
      toast.error(
        `${failures} chunk${failures > 1 ? "s" : ""} n'${failures > 1 ? "ont" : "a"} pas pu être purgé${failures > 1 ? "s" : ""} (déjà purgé ou introuvable).`,
      );
    }

    onSettled();
    onOpenChange(false);
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent data-testid="purge-chunks-dialog">
        <DialogHeader>
          <DialogTitle>Purger {chunkIds.length} chunk(s) ?</DialogTitle>
          <DialogDescription>
            Cette action est irréversible : {chunkIds.length} chunk
            {chunkIds.length > 1 ? "s" : ""} seront archivés et retirés de la recherche
            mémoire du namespace « {namespace} ».
          </DialogDescription>
        </DialogHeader>
        <DialogFooter>
          <Button
            type="button"
            variant="outline"
            onClick={() => onOpenChange(false)}
            disabled={purgeMutation.isPending}
          >
            Annuler
          </Button>
          <Button
            type="button"
            variant="destructive"
            onClick={handleConfirm}
            disabled={purgeMutation.isPending || chunkIds.length === 0}
            data-testid="purge-chunks-confirm"
          >
            {purgeMutation.isPending ? "Purge…" : `Purger ${chunkIds.length} chunk(s)`}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

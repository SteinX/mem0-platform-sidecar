"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Copy, RefreshCw, Save, Trash2 } from "lucide-react";

import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import { Button } from "@/components/ui/button";
import DeleteConfirmationModal from "@/components/ui/delete-confirmation-modal";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { ScrollArea } from "@/components/ui/scroll-area";
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Textarea } from "@/components/ui/textarea";
import { toast } from "@/components/ui/use-toast";
import type {
  SidecarMemory,
  SidecarMemoryHistoryResponse,
  SidecarMemoryUpdateResponse,
} from "@/types/sidecar";
import {
  beginMemoryOperation,
  buildMemoryPatch,
  canApplyMemoryOperation,
  initializeMemoryDraft,
  isMemoryDraftDirty,
  isMemoryDraftReady,
  memoryApiPath,
  nextMemoryRequestGeneration,
  normalizeMemoryId,
  parseMemoryHistory,
  type MemoryDraft,
} from "@/utils/memory-explorer-state";
import { sidecarDelete, sidecarGet, sidecarPatch } from "@/utils/sidecar-api";

type MemoryDetailDrawerProps = {
  memoryId: string | null;
  onMemoryIdChange: (memoryId: string | null) => void;
  onRefreshList: () => void;
  onDeleted: (memoryId: string) => void;
};

export function MemoryDetailDrawer({
  memoryId,
  onMemoryIdChange,
  onRefreshList,
  onDeleted,
}: MemoryDetailDrawerProps) {
  const normalizedMemoryId = normalizeMemoryId(memoryId);
  const [activeMemoryId, setActiveMemoryId] = useState<string | null>(
    normalizedMemoryId,
  );
  const [detail, setDetail] = useState<SidecarMemory | null>(null);
  const [history, setHistory] = useState<unknown[]>([]);
  const [draft, setDraft] = useState<MemoryDraft | null>(null);
  const [initialDraft, setInitialDraft] = useState<MemoryDraft | null>(null);
  const [draftMemoryId, setDraftMemoryId] = useState<string | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [historyLoading, setHistoryLoading] = useState(false);
  const [detailError, setDetailError] = useState<string | null>(null);
  const [historyError, setHistoryError] = useState<string | null>(null);
  const [formError, setFormError] = useState<string | null>(null);
  const [isSaving, setIsSaving] = useState(false);
  const [isDeleting, setIsDeleting] = useState(false);
  const [showDiscardDialog, setShowDiscardDialog] = useState(false);
  const [showDeleteDialog, setShowDeleteDialog] = useState(false);
  const [pendingMemoryId, setPendingMemoryId] = useState<string | null>(null);
  const detailGeneration = useRef(0);
  const historyGeneration = useRef(0);
  const mutationGeneration = useRef(0);
  const mountedRef = useRef(true);
  const activeMemoryIdRef = useRef<string | null>(normalizedMemoryId);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      activeMemoryIdRef.current = null;
      detailGeneration.current = nextMemoryRequestGeneration(
        detailGeneration.current,
      );
      historyGeneration.current = nextMemoryRequestGeneration(
        historyGeneration.current,
      );
      mutationGeneration.current = nextMemoryRequestGeneration(
        mutationGeneration.current,
      );
    };
  }, []);

  const isDirty = draft !== null
    && initialDraft !== null
    && draftMemoryId === activeMemoryId
    && isMemoryDraftDirty(draft, initialDraft);
  const isBusy = isSaving || isDeleting;
  const draftReady = isMemoryDraftReady(
    activeMemoryId,
    draftMemoryId,
    detail?.id ?? null,
  );

  const loadDetail = useCallback(async (id: string, resetDraft: boolean) => {
    const operation = beginMemoryOperation(detailGeneration.current, id);
    detailGeneration.current = operation.generation;
    if (!canApplyMemoryOperation(
      operation,
      detailGeneration.current,
      activeMemoryIdRef.current,
      mountedRef.current,
    )) {
      return;
    }
    setDetailLoading(true);
    setDetailError(null);
    try {
      const response = await sidecarGet<SidecarMemory>(memoryApiPath(id));
      if (!canApplyMemoryOperation(
        operation,
        detailGeneration.current,
        activeMemoryIdRef.current,
        mountedRef.current,
      )) {
        return;
      }
      setDetail(response);
      if (resetDraft || draftMemoryId !== id) {
        const nextDraft = initializeMemoryDraft(response);
        setDraft(nextDraft);
        setInitialDraft(nextDraft);
        setDraftMemoryId(id);
        setFormError(null);
      }
    } catch (error) {
      if (canApplyMemoryOperation(
        operation,
        detailGeneration.current,
        activeMemoryIdRef.current,
        mountedRef.current,
      )) {
        setDetailError(error instanceof Error ? error.message : String(error));
      }
    } finally {
      if (canApplyMemoryOperation(
        operation,
        detailGeneration.current,
        activeMemoryIdRef.current,
        mountedRef.current,
      )) {
        setDetailLoading(false);
      }
    }
  }, [draftMemoryId]);

  const loadHistory = useCallback(async (id: string) => {
    const operation = beginMemoryOperation(historyGeneration.current, id);
    historyGeneration.current = operation.generation;
    if (!canApplyMemoryOperation(
      operation,
      historyGeneration.current,
      activeMemoryIdRef.current,
      mountedRef.current,
    )) {
      return;
    }
    setHistoryLoading(true);
    setHistoryError(null);
    try {
      const response = await sidecarGet<SidecarMemoryHistoryResponse>(`${memoryApiPath(id)}/history`);
      if (canApplyMemoryOperation(
        operation,
        historyGeneration.current,
        activeMemoryIdRef.current,
        mountedRef.current,
      )) {
        setHistory(Array.isArray(response.results) ? response.results : []);
      }
    } catch (error) {
      if (canApplyMemoryOperation(
        operation,
        historyGeneration.current,
        activeMemoryIdRef.current,
        mountedRef.current,
      )) {
        setHistoryError(error instanceof Error ? error.message : String(error));
      }
    } finally {
      if (canApplyMemoryOperation(
        operation,
        historyGeneration.current,
        activeMemoryIdRef.current,
        mountedRef.current,
      )) {
        setHistoryLoading(false);
      }
    }
  }, []);

  const activateMemory = useCallback((id: string | null) => {
    const normalizedId = normalizeMemoryId(id);
    detailGeneration.current = nextMemoryRequestGeneration(detailGeneration.current);
    historyGeneration.current = nextMemoryRequestGeneration(historyGeneration.current);
    mutationGeneration.current = nextMemoryRequestGeneration(
      mutationGeneration.current,
    );
    activeMemoryIdRef.current = normalizedId;
    setActiveMemoryId(normalizedId);
    setDraft(null);
    setInitialDraft(null);
    setDraftMemoryId(null);
    setShowDiscardDialog(false);
    setPendingMemoryId(null);
    if (normalizedId === null) {
      return;
    }
    setDetail(null);
    setHistory([]);
    setDetailError(null);
    setHistoryError(null);
    void loadDetail(normalizedId, true);
    void loadHistory(normalizedId);
  }, [loadDetail, loadHistory]);

  useEffect(() => {
    if (normalizedMemoryId === activeMemoryId) {
      return;
    }
    if (isBusy) {
      if (activeMemoryId !== null) {
        onMemoryIdChange(activeMemoryId);
      }
      return;
    }
    if (activeMemoryId !== null && isDirty) {
      setPendingMemoryId(normalizedMemoryId);
      setShowDiscardDialog(true);
      onMemoryIdChange(activeMemoryId);
      return;
    }
    activateMemory(normalizedMemoryId);
  }, [
    activateMemory,
    activeMemoryId,
    isBusy,
    isDirty,
    normalizedMemoryId,
    onMemoryIdChange,
  ]);

  useEffect(() => {
    if (activeMemoryId !== null && detail === null && !detailLoading && detailError === null) {
      void loadDetail(activeMemoryId, true);
      void loadHistory(activeMemoryId);
    }
  }, [activeMemoryId, detail, detailError, detailLoading, loadDetail, loadHistory]);

  const requestClose = () => {
    if (isBusy) {
      return;
    }
    if (isDirty) {
      setPendingMemoryId(null);
      setShowDiscardDialog(true);
      return;
    }
    onMemoryIdChange(null);
    activateMemory(null);
  };

  const discardAndContinue = () => {
    if (isBusy) {
      return;
    }
    const target = pendingMemoryId;
    setShowDiscardDialog(false);
    onMemoryIdChange(target);
    activateMemory(target);
  };

  const saveMemory = async () => {
    const targetMemoryId = activeMemoryIdRef.current;
    if (
      targetMemoryId === null
      || targetMemoryId !== activeMemoryId
      || draft === null
      || initialDraft === null
      || !draftReady
    ) {
      return;
    }
    let patch: Record<string, unknown>;
    try {
      patch = buildMemoryPatch(draft, initialDraft);
    } catch (error) {
      setFormError(error instanceof Error ? error.message : String(error));
      return;
    }
    if (Object.keys(patch).length === 0) {
      setFormError("No changes to save.");
      return;
    }
    const operation = beginMemoryOperation(
      mutationGeneration.current,
      targetMemoryId,
    );
    mutationGeneration.current = operation.generation;
    setIsSaving(true);
    setFormError(null);
    try {
      const response = await sidecarPatch<SidecarMemoryUpdateResponse>(
        memoryApiPath(targetMemoryId),
        patch,
      );
      if (!canApplyMemoryOperation(
        operation,
        mutationGeneration.current,
        activeMemoryIdRef.current,
        mountedRef.current,
      )) {
        return;
      }
      const nextDraft = initializeMemoryDraft(response.memory);
      setDetail(response.memory);
      setDraft(nextDraft);
      setInitialDraft(nextDraft);
      setDraftMemoryId(targetMemoryId);
      setFormError(null);
      if (!canApplyMemoryOperation(
        operation,
        mutationGeneration.current,
        activeMemoryIdRef.current,
        mountedRef.current,
      )) {
        return;
      }
      toast({ title: "Memory updated", variant: "success" });
      if (!canApplyMemoryOperation(
        operation,
        mutationGeneration.current,
        activeMemoryIdRef.current,
        mountedRef.current,
      )) {
        return;
      }
      onRefreshList();
      if (canApplyMemoryOperation(
        operation,
        mutationGeneration.current,
        activeMemoryIdRef.current,
        mountedRef.current,
      )) {
        void loadDetail(targetMemoryId, false);
        void loadHistory(targetMemoryId);
      }
    } catch (error) {
      if (!canApplyMemoryOperation(
        operation,
        mutationGeneration.current,
        activeMemoryIdRef.current,
        mountedRef.current,
      )) {
        return;
      }
      const message = error instanceof Error ? error.message : String(error);
      setFormError(message);
      toast({ title: "Failed to update memory", description: message, variant: "destructive" });
    } finally {
      if (canApplyMemoryOperation(
        operation,
        mutationGeneration.current,
        activeMemoryIdRef.current,
        mountedRef.current,
      )) {
        setIsSaving(false);
      }
    }
  };

  const deleteMemory = async () => {
    const targetMemoryId = activeMemoryIdRef.current;
    if (targetMemoryId === null || targetMemoryId !== activeMemoryId) {
      return;
    }
    const operation = beginMemoryOperation(
      mutationGeneration.current,
      targetMemoryId,
    );
    mutationGeneration.current = operation.generation;
    setIsDeleting(true);
    try {
      await sidecarDelete(memoryApiPath(targetMemoryId));
      if (!canApplyMemoryOperation(
        operation,
        mutationGeneration.current,
        activeMemoryIdRef.current,
        mountedRef.current,
      )) {
        return;
      }
      toast({ title: "Memory deleted", variant: "success" });
      if (!canApplyMemoryOperation(
        operation,
        mutationGeneration.current,
        activeMemoryIdRef.current,
        mountedRef.current,
      )) {
        return;
      }
      setShowDeleteDialog(false);
      setIsDeleting(false);
      if (!canApplyMemoryOperation(
        operation,
        mutationGeneration.current,
        activeMemoryIdRef.current,
        mountedRef.current,
      )) {
        return;
      }
      onDeleted(targetMemoryId);
      if (!canApplyMemoryOperation(
        operation,
        mutationGeneration.current,
        activeMemoryIdRef.current,
        mountedRef.current,
      )) {
        return;
      }
      activateMemory(null);
    } catch (error) {
      if (!canApplyMemoryOperation(
        operation,
        mutationGeneration.current,
        activeMemoryIdRef.current,
        mountedRef.current,
      )) {
        return;
      }
      toast({
        title: "Failed to delete memory",
        description: error instanceof Error ? error.message : String(error),
        variant: "destructive",
      });
    } finally {
      if (canApplyMemoryOperation(
        operation,
        mutationGeneration.current,
        activeMemoryIdRef.current,
        mountedRef.current,
      )) {
        setIsDeleting(false);
      }
    }
  };

  const parsedHistory = useMemo(() => parseMemoryHistory(history), [history]);

  return (
    <>
      <Sheet open={activeMemoryId !== null} onOpenChange={(open) => open || requestClose()}>
        <SheetContent side="right" className="flex w-full max-w-full flex-col gap-0 overflow-x-hidden p-0 sm:max-w-2xl">
          <SheetHeader className="border-b border-memBorder-primary px-5 py-4 pr-12 text-left">
            <SheetTitle>Memory details</SheetTitle>
            <SheetDescription className="break-all">
              Inspect the selected memory, its source, and update history.
            </SheetDescription>
          </SheetHeader>

          <Tabs defaultValue="details" className="flex min-h-0 flex-1 flex-col">
            <TabsList aria-label="Memory detail sections" className="mx-5 mt-4 grid grid-cols-2">
              <TabsTrigger value="details">Details</TabsTrigger>
              <TabsTrigger value="history">Source & Updates</TabsTrigger>
            </TabsList>

            <ScrollArea className="min-h-0 flex-1">
              <TabsContent value="details" className="m-0 space-y-5 px-5 py-5">
                {detailLoading && detail === null ? (
                  <p role="status" className="text-sm text-onSurface-default-secondary">Loading memory details...</p>
                ) : null}
                {detailError !== null ? (
                  <div role="alert" className="space-y-2">
                    <p className="break-all text-sm text-onSurface-danger-primary">{detailError}</p>
                    <Button type="button" size="sm" variant="outline" disabled={activeMemoryId === null} onClick={() => activeMemoryId && void loadDetail(activeMemoryId, detail === null)}>
                      <RefreshCw className="mr-2 size-4" />Retry details
                    </Button>
                  </div>
                ) : null}

                {draft !== null && draftReady ? (
                  <>
                    <div className="space-y-1.5">
                      <Label htmlFor="memory-content">Memory content</Label>
                      <Textarea
                        id="memory-content"
                        className="min-h-32 whitespace-pre-wrap break-words"
                        value={draft.text}
                        disabled={isBusy}
                        onChange={(event) => setDraft((current) => current && ({ ...current, text: event.target.value }))}
                      />
                    </div>
                    <div className="space-y-1.5">
                      <Label htmlFor="memory-metadata">Metadata JSON</Label>
                      <Textarea
                        id="memory-metadata"
                        spellCheck={false}
                        className="min-h-40 overflow-x-auto font-mono text-xs"
                        value={draft.metadataText}
                        disabled={isBusy}
                        aria-describedby={formError ? "memory-form-error" : undefined}
                        onChange={(event) => setDraft((current) => current && ({ ...current, metadataText: event.target.value }))}
                      />
                    </div>
                    <div className="space-y-1.5">
                      <Label htmlFor="memory-expiration">Expiration</Label>
                      <Input
                        id="memory-expiration"
                        placeholder="ISO timestamp or blank"
                        value={draft.expiration}
                        disabled={isBusy}
                        onChange={(event) => setDraft((current) => current && ({ ...current, expiration: event.target.value }))}
                      />
                    </div>
                    {formError ? <p id="memory-form-error" role="alert" className="break-all text-sm text-onSurface-danger-primary">{formError}</p> : null}
                    <div className="flex min-w-0 flex-wrap items-center justify-between gap-2 border-t border-memBorder-primary pt-4">
                      <div className="flex min-w-0 flex-wrap gap-2">
                        <Button type="button" variant="outline" disabled={!activeMemoryId} onClick={async () => {
                          const targetMemoryId = activeMemoryIdRef.current;
                          if (targetMemoryId) {
                            try {
                              await navigator.clipboard.writeText(targetMemoryId);
                              if (
                                !mountedRef.current
                                || activeMemoryIdRef.current !== targetMemoryId
                              ) {
                                return;
                              }
                              toast({ title: "Memory ID copied", variant: "success" });
                            } catch (error) {
                              if (
                                !mountedRef.current
                                || activeMemoryIdRef.current !== targetMemoryId
                              ) {
                                return;
                              }
                              toast({
                                title: "Failed to copy memory ID",
                                description: error instanceof Error ? error.message : String(error),
                                variant: "destructive",
                              });
                            }
                          }
                        }}>
                          <Copy className="mr-2 size-4" />Copy ID
                        </Button>
                        <Button type="button" variant="destructive" disabled={isBusy} onClick={() => setShowDeleteDialog(true)}>
                          <Trash2 className="mr-2 size-4" />Delete
                        </Button>
                      </div>
                      <Button type="button" disabled={!isDirty || isBusy} onClick={() => void saveMemory()}>
                        <Save className="mr-2 size-4" />{isSaving ? "Saving..." : "Save changes"}
                      </Button>
                    </div>
                  </>
                ) : null}
              </TabsContent>

              <TabsContent value="history" className="m-0 space-y-6 px-5 py-5">
                {historyLoading ? <p role="status" className="text-sm text-onSurface-default-secondary">Loading source and updates...</p> : null}
                {historyError !== null ? (
                  <div role="alert" className="space-y-2">
                    <p className="break-all text-sm text-onSurface-danger-primary">{historyError}</p>
                    <Button type="button" size="sm" variant="outline" disabled={activeMemoryId === null} onClick={() => activeMemoryId && void loadHistory(activeMemoryId)}>
                      <RefreshCw className="mr-2 size-4" />Retry history
                    </Button>
                  </div>
                ) : null}
                <section aria-labelledby="memory-source-heading" className="space-y-3">
                  <h3 id="memory-source-heading" className="font-semibold">Source</h3>
                  {!historyLoading && historyError === null && parsedHistory.sourceMessages.length === 0 ? (
                    <p className="text-sm text-onSurface-default-secondary">Source unavailable</p>
                  ) : (
                    parsedHistory.sourceMessages.map((message, index) => (
                      <div key={`${message.role}-${index}`} className="min-w-0 rounded-md border border-memBorder-primary p-3">
                        <p className="mb-1 text-xs font-semibold uppercase text-onSurface-default-secondary">{message.role}</p>
                        <p className="whitespace-pre-wrap break-words text-sm">{message.content}</p>
                      </div>
                    ))
                  )}
                </section>
                <section aria-labelledby="memory-updates-heading" className="space-y-3">
                  <h3 id="memory-updates-heading" className="font-semibold">Updates</h3>
                  {!historyLoading && historyError === null && parsedHistory.updates.length === 0 ? (
                    <p className="text-sm text-onSurface-default-secondary">No updates recorded.</p>
                  ) : (
                    parsedHistory.updates.map((update, index) => (
                      <article key={`${update.timestamp ?? "unknown"}-${index}`} className="min-w-0 space-y-2 border-l-2 border-memBorder-primary pl-3">
                        <div className="flex min-w-0 flex-wrap items-center justify-between gap-2">
                          <h4 className="font-medium">{update.event}</h4>
                          <time className="break-words text-xs text-onSurface-default-secondary" dateTime={update.timestamp ?? undefined}>
                            {update.timestamp ? new Date(update.timestamp).toLocaleString() : "Timestamp unavailable"}
                          </time>
                        </div>
                        {update.oldMemory !== null ? <div><span className="text-xs font-semibold">Old</span><p className="whitespace-pre-wrap break-words text-sm">{update.oldMemory}</p></div> : null}
                        {update.newMemory !== null ? <div><span className="text-xs font-semibold">New</span><p className="whitespace-pre-wrap break-words text-sm">{update.newMemory}</p></div> : null}
                      </article>
                    ))
                  )}
                </section>
              </TabsContent>
            </ScrollArea>
          </Tabs>
        </SheetContent>
      </Sheet>

      <AlertDialog open={showDiscardDialog} onOpenChange={setShowDiscardDialog}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Discard unsaved changes?</AlertDialogTitle>
            <AlertDialogDescription>
              Your edits will be lost if you close this memory or open another one.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Keep editing</AlertDialogCancel>
            <AlertDialogAction disabled={isBusy} onClick={discardAndContinue}>
              Discard changes
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      <DeleteConfirmationModal
        isOpen={showDeleteDialog}
        onClose={() => !isDeleting && setShowDeleteDialog(false)}
        onConfirm={() => void deleteMemory()}
        title="Delete memory"
        description="This memory will be permanently removed. This cannot be undone."
        itemName={activeMemoryId ?? ""}
        confirmButtonText={isDeleting ? "Deleting..." : "Delete"}
      />
    </>
  );
}

"use client";

import { useEffect, useMemo, useState } from "react";
import { Ban, Save, Trash2 } from "lucide-react";

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
import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from "@/components/ui/collapsible";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { ScrollArea } from "@/components/ui/scroll-area";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";
import { Switch } from "@/components/ui/switch";
import { Textarea } from "@/components/ui/textarea";
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { toast } from "@/components/ui/use-toast";
import { SidecarCategory, SidecarCategoryInput } from "@/types/sidecar";
import { editorToSchema, validateCategoryFields } from "@/utils/category-schema";
import {
  type CategoryDraft,
  activateAdvancedMode,
  categoryDraftFingerprint,
  createCategoryDraft,
  planCategoryDisable,
  planBuilderTransition,
  resolveCategorySchemaForSave,
  resetToEmptyBuilder,
} from "@/utils/category-editor-state";
import { sidecarDelete, sidecarPatch, sidecarPost } from "@/utils/sidecar-api";

import { CategoryFieldEditor } from "./category-field-editor";

type CategoryEditorDrawerProps = {
  projectId: string;
  category: SidecarCategory | null;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onSaved: (category: SidecarCategory) => void;
  onDeleted: (categoryId: string) => void;
};

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

export function CategoryEditorDrawer({
  projectId,
  category,
  open,
  onOpenChange,
  onSaved,
  onDeleted,
}: CategoryEditorDrawerProps) {
  const [draft, setDraft] = useState<CategoryDraft>(() => createCategoryDraft(category));
  const [initialDraft, setInitialDraft] = useState<CategoryDraft>(() =>
    createCategoryDraft(category),
  );
  const [initialFingerprint, setInitialFingerprint] = useState(() =>
    categoryDraftFingerprint(createCategoryDraft(category)),
  );
  const [fieldErrors, setFieldErrors] = useState<Record<string, string>>({});
  const [formError, setFormError] = useState<string | null>(null);
  const [isSaving, setIsSaving] = useState(false);
  const [isDeleting, setIsDeleting] = useState(false);
  const [showDiscardDialog, setShowDiscardDialog] = useState(false);
  const [showDeleteDialog, setShowDeleteDialog] = useState(false);
  const [showDisableDialog, setShowDisableDialog] = useState(false);
  const [showBuilderResetDialog, setShowBuilderResetDialog] = useState(false);

  useEffect(() => {
    if (!open) {
      return;
    }
    const nextDraft = createCategoryDraft(category);
    setDraft(nextDraft);
    setInitialDraft(nextDraft);
    setInitialFingerprint(categoryDraftFingerprint(nextDraft));
    setFieldErrors({});
    setFormError(null);
    setShowDiscardDialog(false);
    setShowDeleteDialog(false);
    setShowDisableDialog(false);
    setShowBuilderResetDialog(false);
  }, [category, open]);

  const isDirty = categoryDraftFingerprint(draft) !== initialFingerprint;
  const generatedSchema = useMemo(
    () => JSON.stringify(editorToSchema(draft.fields), null, 2),
    [draft.fields],
  );
  const isBusy = isSaving || isDeleting;

  const requestClose = () => {
    if (isBusy) {
      return;
    }
    if (isDirty) {
      setShowDiscardDialog(true);
      return;
    }
    onOpenChange(false);
  };

  const switchToAdvanced = () => {
    const nextDraft = activateAdvancedMode(draft);
    if (nextDraft === draft) {
      return;
    }
    setDraft(nextDraft);
    setFieldErrors({});
    setFormError(null);
  };

  const switchToBuilder = () => {
    const transition = planBuilderTransition(draft);
    if (transition.status === "ready") {
      setDraft(transition.draft);
      setFormError(null);
      return;
    }
    if (transition.status === "confirm") {
      setDraft((current) => ({
        ...current,
        unsupportedPaths: transition.unsupportedPaths,
      }));
      setShowBuilderResetDialog(true);
      return;
    }
    setFormError(transition.message);
  };

  const saveCategory = async () => {
    const name = draft.name.trim();
    if (!name) {
      setFormError("Category name is required.");
      return;
    }

    let schema: Record<string, unknown>;
    if (draft.mode === "builder") {
      const validation = validateCategoryFields(draft.fields);
      setFieldErrors(validation.fieldErrors);
      if (!validation.valid) {
        setFormError(validation.formError);
        return;
      }
    }
    try {
      schema = resolveCategorySchemaForSave(
        draft,
        initialDraft,
        category?.schema,
      );
    } catch (error) {
      setFormError(errorMessage(error));
      return;
    }

    const payload: SidecarCategoryInput = {
      name,
      description: draft.description,
      enabled: draft.enabled,
      strategy: draft.strategy,
      schema,
    };

    setIsSaving(true);
    setFormError(null);
    try {
      const saved = category
        ? await sidecarPatch<SidecarCategory>(
            `/v1/projects/${projectId}/categories/${category.id}`,
            payload,
          )
        : await sidecarPost<SidecarCategory>(
            `/v1/projects/${projectId}/categories`,
            payload,
          );
      onSaved(saved);
      onOpenChange(false);
      toast({ title: category ? "Category updated" : "Category created", variant: "success" });
    } catch (error) {
      toast({
        title: "Failed to save category",
        description: errorMessage(error),
        variant: "destructive",
      });
    } finally {
      setIsSaving(false);
    }
  };

  const disableCategory = async () => {
    if (!category) {
      return;
    }
    setIsSaving(true);
    try {
      const saved = await sidecarPatch<SidecarCategory>(
        `/v1/projects/${projectId}/categories/${category.id}`,
        { enabled: false },
      );
      onSaved(saved);
      onOpenChange(false);
      toast({ title: "Category disabled", variant: "success" });
    } catch (error) {
      toast({
        title: "Failed to disable category",
        description: errorMessage(error),
        variant: "destructive",
      });
    } finally {
      setIsSaving(false);
      setShowDisableDialog(false);
    }
  };

  const requestDisable = () => {
    if (planCategoryDisable(isDirty) === "confirm") {
      setShowDisableDialog(true);
      return;
    }
    void disableCategory();
  };

  const deleteCategory = async () => {
    if (!category) {
      return;
    }
    setIsDeleting(true);
    try {
      await sidecarDelete(`/v1/projects/${projectId}/categories/${category.id}`);
      onDeleted(category.id);
      onOpenChange(false);
      toast({ title: "Category deleted", variant: "success" });
    } catch (error) {
      toast({
        title: "Failed to delete category",
        description: errorMessage(error),
        variant: "destructive",
      });
    } finally {
      setIsDeleting(false);
      setShowDeleteDialog(false);
    }
  };

  return (
    <>
      <Sheet open={open} onOpenChange={(nextOpen) => nextOpen || requestClose()}>
        <SheetContent side="right" className="flex w-full flex-col gap-0 p-0 sm:max-w-2xl">
          <SheetHeader className="border-b border-memBorder-primary px-5 py-4 text-left">
            <SheetTitle>{category ? "Edit category" : "Create category"}</SheetTitle>
            <SheetDescription>
              Define the metadata schema used to classify memories.
            </SheetDescription>
          </SheetHeader>

          <ScrollArea className="min-h-0 flex-1">
            <div className="space-y-5 px-5 py-5">
              <div className="grid gap-4 sm:grid-cols-2">
                <div className="space-y-1.5">
                  <Label htmlFor="category-name">Name</Label>
                  <Input
                    id="category-name"
                    value={draft.name}
                    disabled={isBusy}
                    onChange={(event) => setDraft((current) => ({
                      ...current,
                      name: event.target.value,
                    }))}
                  />
                </div>
                <div className="space-y-1.5">
                  <Label htmlFor="category-strategy">Strategy</Label>
                  <Select
                    value={draft.strategy}
                    disabled={isBusy}
                    onValueChange={(strategy) => setDraft((current) => ({
                      ...current,
                      strategy,
                    }))}
                  >
                    <SelectTrigger id="category-strategy"><SelectValue /></SelectTrigger>
                    <SelectContent>
                      <SelectItem value="metadata">Metadata</SelectItem>
                      {draft.strategy !== "metadata" ? (
                        <SelectItem value={draft.strategy}>{draft.strategy}</SelectItem>
                      ) : null}
                    </SelectContent>
                  </Select>
                </div>
                <div className="space-y-1.5 sm:col-span-2">
                  <Label htmlFor="category-description">Description</Label>
                  <Textarea
                    id="category-description"
                    value={draft.description}
                    disabled={isBusy}
                    onChange={(event) => setDraft((current) => ({
                      ...current,
                      description: event.target.value,
                    }))}
                  />
                </div>
                <label className="flex items-center gap-2 text-sm">
                  <Switch
                    checked={draft.enabled}
                    disabled={isBusy}
                    onCheckedChange={(enabled) => setDraft((current) => ({
                      ...current,
                      enabled,
                    }))}
                  />
                  Enabled
                </label>
              </div>

              <div className="flex w-full rounded-md border border-memBorder-primary p-1">
                <Button
                  type="button"
                  variant={draft.mode === "builder" ? "secondary" : "ghost"}
                  className="min-w-0 flex-1"
                  disabled={isBusy}
                  onClick={switchToBuilder}
                >
                  Field builder
                </Button>
                <Button
                  type="button"
                  variant={draft.mode === "advanced" ? "secondary" : "ghost"}
                  className="min-w-0 flex-1"
                  disabled={isBusy}
                  onClick={switchToAdvanced}
                >
                  Advanced schema
                </Button>
              </div>

              {draft.mode === "builder" ? (
                <>
                  <CategoryFieldEditor
                    fields={draft.fields}
                    errors={fieldErrors}
                    disabled={isBusy}
                    onChange={(fields) => setDraft((current) => ({ ...current, fields }))}
                  />
                  <Collapsible>
                    <CollapsibleTrigger asChild>
                      <Button type="button" variant="ghost" size="sm">
                        Generated schema
                      </Button>
                    </CollapsibleTrigger>
                    <CollapsibleContent className="pt-2">
                      <Textarea
                        readOnly
                        value={generatedSchema}
                        aria-label="Generated schema preview"
                        className="min-h-44 font-mono text-xs"
                      />
                    </CollapsibleContent>
                  </Collapsible>
                </>
              ) : (
                <div className="space-y-1.5">
                  <Label htmlFor="advanced-schema">Advanced schema</Label>
                  <Textarea
                    id="advanced-schema"
                    value={draft.rawSchemaText}
                    disabled={isBusy}
                    className="min-h-80 font-mono text-xs"
                    onChange={(event) => setDraft((current) => ({
                      ...current,
                      rawSchemaText: event.target.value,
                    }))}
                  />
                </div>
              )}

              {formError ? (
                <p role="alert" className="text-sm text-onSurface-danger-primary">{formError}</p>
              ) : null}
            </div>
          </ScrollArea>

          <div className="flex flex-wrap items-center justify-between gap-2 border-t border-memBorder-primary px-5 py-4">
            <div className="flex gap-2">
              {category ? (
                <>
                  <Button
                    type="button"
                    size="sm"
                    variant="outline"
                    disabled={isBusy || !category.enabled}
                    onClick={requestDisable}
                  >
                    <Ban className="mr-2 size-4" />
                    Disable
                  </Button>
                  <Tooltip>
                    <TooltipTrigger asChild>
                      <Button
                        type="button"
                        size="icon"
                        variant="ghost"
                        aria-label="Delete category"
                        disabled={isBusy}
                        onClick={() => setShowDeleteDialog(true)}
                      >
                        <Trash2 className="size-4" />
                      </Button>
                    </TooltipTrigger>
                    <TooltipContent>Delete category</TooltipContent>
                  </Tooltip>
                </>
              ) : null}
            </div>
            <div className="flex gap-2">
              <Button type="button" variant="outline" disabled={isBusy} onClick={requestClose}>
                Cancel
              </Button>
              <Button type="button" disabled={isBusy} onClick={() => void saveCategory()}>
                <Save className="mr-2 size-4" />
                {isSaving ? "Saving..." : "Save category"}
              </Button>
            </div>
          </div>
        </SheetContent>
      </Sheet>

      <AlertDialog open={showDiscardDialog} onOpenChange={setShowDiscardDialog}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Discard changes?</AlertDialogTitle>
            <AlertDialogDescription>
              Your unsaved category changes will be lost.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Keep editing</AlertDialogCancel>
            <AlertDialogAction onClick={() => onOpenChange(false)}>Discard</AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      <AlertDialog open={showDeleteDialog} onOpenChange={setShowDeleteDialog}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Delete category?</AlertDialogTitle>
            <AlertDialogDescription>
              This permanently deletes {category?.name || "this category"}.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Cancel</AlertDialogCancel>
            <AlertDialogAction
              className="bg-onSurface-danger-primary text-white"
              onClick={() => void deleteCategory()}
            >
              Delete category
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      <AlertDialog open={showDisableDialog} onOpenChange={setShowDisableDialog}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Discard changes and disable?</AlertDialogTitle>
            <AlertDialogDescription>
              Unsaved category changes will be lost before this category is disabled.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Keep editing</AlertDialogCancel>
            <AlertDialogAction onClick={() => void disableCategory()}>
              Discard and disable
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      <AlertDialog open={showBuilderResetDialog} onOpenChange={setShowBuilderResetDialog}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Replace advanced schema?</AlertDialogTitle>
            <AlertDialogDescription asChild>
              <div className="space-y-2">
                <p>The field builder cannot represent these schema paths:</p>
                <ul className="max-h-36 list-disc overflow-auto pl-5 font-mono text-xs">
                  {draft.unsupportedPaths.map((path) => <li key={path}>{path}</li>)}
                </ul>
                <p>Continuing starts with an empty field builder.</p>
              </div>
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Keep advanced schema</AlertDialogCancel>
            <AlertDialogAction onClick={() => {
              setDraft((current) => resetToEmptyBuilder(current));
              setFieldErrors({});
              setFormError(null);
            }}>
              Start empty builder
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </>
  );
}

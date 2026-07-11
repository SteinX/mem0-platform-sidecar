"use client";

import { useCallback, useEffect, useState } from "react";
import { Plus, RefreshCw } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { toast } from "@/components/ui/use-toast";
import { SidecarCategory, SidecarCategoryResponse } from "@/types/sidecar";
import { countSchemaFields, schemaToEditor } from "@/utils/category-schema";
import { sidecarGet } from "@/utils/sidecar-api";
import { getSidecarProjectId } from "@/utils/sidecar-project";

import { CategoryEditorDrawer } from "./category-editor-drawer";

function formatUpdatedAt(value: string): string {
  const date = new Date(value);
  return Number.isNaN(date.getTime())
    ? value
    : new Intl.DateTimeFormat(undefined, {
        dateStyle: "medium",
        timeStyle: "short",
      }).format(date);
}

export default function CategoriesPage() {
  const [categories, setCategories] = useState<SidecarCategory[]>([]);
  const [projectId, setProjectId] = useState<string | null>(null);
  const [selectedCategory, setSelectedCategory] = useState<SidecarCategory | null>(null);
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [isLoading, setIsLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);

  const loadCategories = useCallback(async () => {
    setIsLoading(true);
    setLoadError(null);
    try {
      const resolvedProjectId = await getSidecarProjectId();
      const response = await sidecarGet<SidecarCategoryResponse>(
        `/v1/projects/${resolvedProjectId}/categories`,
      );
      setProjectId(resolvedProjectId);
      setCategories(response.categories);
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      setProjectId(null);
      setLoadError(message);
      toast({
        title: "Failed to load categories",
        description: message,
        variant: "destructive",
      });
    } finally {
      setIsLoading(false);
    }
  }, []);

  useEffect(() => {
    void loadCategories();
  }, [loadCategories]);

  const handleSaved = (saved: SidecarCategory) => {
    setCategories((current) => {
      const exists = current.some((category) => category.id === saved.id);
      return exists
        ? current.map((category) => (category.id === saved.id ? saved : category))
        : [saved, ...current];
    });
  };

  const handleDeleted = (categoryId: string) => {
    setCategories((current) => current.filter((category) => category.id !== categoryId));
  };

  const openCreateDrawer = () => {
    setSelectedCategory(null);
    setDrawerOpen(true);
  };

  const openEditDrawer = (category: SidecarCategory) => {
    setSelectedCategory(category);
    setDrawerOpen(true);
  };

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="space-y-1">
          <h1 className="text-xl font-semibold font-fustat">Custom Categories</h1>
          <p className="text-sm text-onSurface-default-secondary">
            Define structured metadata for organizing and retrieving memories.
          </p>
        </div>
        <Button onClick={openCreateDrawer} disabled={!projectId || isLoading}>
          <Plus className="mr-2 size-4" />
          Create category
        </Button>
      </div>

      {loadError ? (
        <div className="flex flex-wrap items-center justify-between gap-3 border-y border-memBorder-primary py-4">
          <p className="text-sm text-onSurface-danger-primary">{loadError}</p>
          <Button variant="outline" onClick={() => void loadCategories()}>
            <RefreshCw className="mr-2 size-4" />
            Retry
          </Button>
        </div>
      ) : null}

      <div className="border-y border-memBorder-primary">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead className="min-w-52">Category</TableHead>
              <TableHead>Status</TableHead>
              <TableHead className="hidden md:table-cell">Strategy</TableHead>
              <TableHead className="hidden sm:table-cell">Fields</TableHead>
              <TableHead className="hidden lg:table-cell">Version</TableHead>
              <TableHead className="hidden xl:table-cell">Updated</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {isLoading ? (
              <TableRow>
                <TableCell colSpan={6} className="h-24 text-center text-onSurface-default-secondary">
                  Loading categories...
                </TableCell>
              </TableRow>
            ) : categories.length === 0 && !loadError ? (
              <TableRow>
                <TableCell colSpan={6} className="h-24 text-center text-onSurface-default-secondary">
                  No categories yet.
                </TableCell>
              </TableRow>
            ) : (
              categories.map((category) => {
                const editor = schemaToEditor(category.schema);
                const isAdvanced = editor.mode === "advanced";
                return (
                  <TableRow
                    key={category.id}
                    role="button"
                    tabIndex={0}
                    className="cursor-pointer focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
                    onClick={() => openEditDrawer(category)}
                    onKeyDown={(event) => {
                      if (event.key === "Enter" || event.key === " ") {
                        event.preventDefault();
                        openEditDrawer(category);
                      }
                    }}
                  >
                    <TableCell>
                      <div className="min-w-0 space-y-1">
                        <div className="flex flex-wrap items-center gap-2">
                          <span className="font-medium">{category.name}</span>
                          {isAdvanced ? <Badge variant="outline">Advanced</Badge> : null}
                        </div>
                        <p className="max-w-xl truncate text-xs text-onSurface-default-secondary">
                          {category.description || "No description"}
                        </p>
                      </div>
                    </TableCell>
                    <TableCell>
                      <span className={category.enabled
                        ? "text-onSurface-success-primary"
                        : "text-onSurface-default-secondary"}
                      >
                        {category.enabled ? "Enabled" : "Disabled"}
                      </span>
                    </TableCell>
                    <TableCell className="hidden capitalize md:table-cell">{category.strategy}</TableCell>
                    <TableCell className="hidden sm:table-cell">{countSchemaFields(category.schema)}</TableCell>
                    <TableCell className="hidden lg:table-cell">v{category.version}</TableCell>
                    <TableCell className="hidden whitespace-nowrap xl:table-cell">
                      {formatUpdatedAt(category.updated_at)}
                    </TableCell>
                  </TableRow>
                );
              })
            )}
          </TableBody>
        </Table>
      </div>

      {projectId ? (
        <CategoryEditorDrawer
          projectId={projectId}
          category={selectedCategory}
          open={drawerOpen}
          onOpenChange={setDrawerOpen}
          onSaved={handleSaved}
          onDeleted={handleDeleted}
        />
      ) : null}
    </div>
  );
}

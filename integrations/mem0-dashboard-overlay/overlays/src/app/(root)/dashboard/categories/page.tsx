"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { Plus, Save, Trash2 } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import { Textarea } from "@/components/ui/textarea";
import { toast } from "@/components/ui/use-toast";
import { SidecarCategory, SidecarCategoryResponse } from "@/types/sidecar";
import { sidecarGet, sidecarPut } from "@/utils/sidecar-api";

type EditableCategory = {
  id: string;
  name: string;
  description: string;
  schemaText: string;
  enabled: boolean;
  strategy: string;
};

const PROJECT_ID = "default";

function createCategoryId(): string {
  return crypto.randomUUID();
}

function toEditable(category: SidecarCategory): EditableCategory {
  return {
    id: createCategoryId(),
    name: category.name,
    description: category.description,
    schemaText: JSON.stringify(category.schema ?? {}, null, 2),
    enabled: category.enabled,
    strategy: category.strategy,
  };
}

function emptyCategory(): EditableCategory {
  return {
    id: createCategoryId(),
    name: "",
    description: "",
    schemaText: "{}",
    enabled: true,
    strategy: "metadata",
  };
}

export default function CategoriesPage() {
  const [categories, setCategories] = useState<EditableCategory[]>([]);
  const [isLoading, setIsLoading] = useState(true);
  const [isSaving, setIsSaving] = useState(false);
  const [hasLoaded, setHasLoaded] = useState(false);

  const enabledCount = useMemo(
    () => categories.filter((category) => category.enabled).length,
    [categories],
  );

  const loadCategories = useCallback(async () => {
    setIsLoading(true);
    try {
      const response = await sidecarGet<SidecarCategoryResponse>(
        `/v1/projects/${PROJECT_ID}/categories`,
      );
      setCategories(response.categories.map(toEditable));
      setHasLoaded(true);
    } catch (error) {
      setHasLoaded(false);
      toast({
        title: "Failed to load categories",
        description: error instanceof Error ? error.message : String(error),
        variant: "destructive",
      });
    } finally {
      setIsLoading(false);
    }
  }, []);

  useEffect(() => {
    void loadCategories();
  }, [loadCategories]);

  const updateCategory = (
    index: number,
    updates: Partial<EditableCategory>,
  ) => {
    setCategories((current) =>
      current.map((category, itemIndex) =>
        itemIndex === index ? { ...category, ...updates } : category,
      ),
    );
  };

  const isEditorDisabled = isLoading || isSaving || !hasLoaded;

  const saveCategories = async () => {
    setIsSaving(true);
    try {
      const payload = {
        categories: categories.map((category) => ({
          name: category.name.trim(),
          description: category.description,
          enabled: category.enabled,
          strategy: category.strategy,
          schema: JSON.parse(category.schemaText),
        })),
      };
      const response = await sidecarPut<SidecarCategoryResponse>(
        `/v1/projects/${PROJECT_ID}/categories`,
        payload,
      );
      setCategories(response.categories.map(toEditable));
      toast({ title: "Categories saved", variant: "success" });
    } catch (error) {
      toast({
        title: "Failed to save categories",
        description: error instanceof Error ? error.message : String(error),
        variant: "destructive",
      });
    } finally {
      setIsSaving(false);
    }
  };

  return (
    <div className="space-y-6">
      <div className="flex items-start justify-between gap-4">
        <div className="space-y-1">
          <h1 className="text-xl font-semibold font-fustat">Custom Categories</h1>
          <p className="text-sm text-onSurface-default-secondary">
            Project {PROJECT_ID} has {categories.length} categories, {enabledCount} enabled.
          </p>
        </div>
        <div className="flex gap-2">
          <Button
            variant="outline"
            onClick={() => setCategories((current) => [...current, emptyCategory()])}
            disabled={isEditorDisabled}
          >
            <Plus className="mr-2 size-4" />
            Add
          </Button>
          <Button onClick={saveCategories} disabled={isEditorDisabled}>
            <Save className="mr-2 size-4" />
            Save
          </Button>
        </div>
      </div>

      {!hasLoaded ? (
        <Card className="border-memBorder-primary">
          <CardContent className="flex flex-col items-start gap-3 p-5">
            <p className="text-sm text-onSurface-default-secondary">
              Load categories before editing or saving changes.
            </p>
            <Button
              variant="outline"
              onClick={() => void loadCategories()}
              disabled={isLoading}
            >
              Retry load
            </Button>
          </CardContent>
        </Card>
      ) : null}

      <div className="grid gap-4">
        {categories.map((category, index) => (
          <Card key={category.id} className="border-memBorder-primary">
            <CardContent className="grid gap-4 p-5 md:grid-cols-[1fr_1fr_auto]">
              <div className="space-y-2">
                <Label>Name</Label>
                <Input
                  value={category.name}
                  onChange={(event) => updateCategory(index, { name: event.target.value })}
                  disabled={isEditorDisabled}
                />
                <Label>Description</Label>
                <Input
                  value={category.description}
                  onChange={(event) =>
                    updateCategory(index, { description: event.target.value })
                  }
                  disabled={isEditorDisabled}
                />
              </div>
              <div className="space-y-2">
                <Label>Schema JSON</Label>
                <Textarea
                  className="min-h-28 font-mono text-xs"
                  value={category.schemaText}
                  onChange={(event) =>
                    updateCategory(index, { schemaText: event.target.value })
                  }
                  disabled={isEditorDisabled}
                />
              </div>
              <div className="flex items-start gap-3">
                <Switch
                  checked={category.enabled}
                  onCheckedChange={(enabled) => updateCategory(index, { enabled })}
                  disabled={isEditorDisabled}
                />
                <Button
                  variant="ghost"
                  size="icon"
                  onClick={() =>
                    setCategories((current) =>
                      current.filter((_, itemIndex) => itemIndex !== index),
                    )
                  }
                  disabled={isEditorDisabled}
                >
                  <Trash2 className="size-4 text-onSurface-danger-primary" />
                </Button>
              </div>
            </CardContent>
          </Card>
        ))}
      </div>
    </div>
  );
}

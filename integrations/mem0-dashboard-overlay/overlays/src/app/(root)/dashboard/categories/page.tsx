"use client";

import { useEffect, useMemo, useState } from "react";
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
  name: string;
  description: string;
  schemaText: string;
  enabled: boolean;
  strategy: string;
};

const PROJECT_ID = "default";

function toEditable(category: SidecarCategory): EditableCategory {
  return {
    name: category.name,
    description: category.description,
    schemaText: JSON.stringify(category.schema ?? {}, null, 2),
    enabled: category.enabled,
    strategy: category.strategy,
  };
}

function emptyCategory(): EditableCategory {
  return {
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

  const enabledCount = useMemo(
    () => categories.filter((category) => category.enabled).length,
    [categories],
  );

  useEffect(() => {
    async function loadCategories() {
      try {
        const response = await sidecarGet<SidecarCategoryResponse>(
          `/v1/projects/${PROJECT_ID}/categories`,
        );
        setCategories(response.categories.map(toEditable));
      } catch (error) {
        toast({
          title: "Failed to load categories",
          description: error instanceof Error ? error.message : String(error),
          variant: "destructive",
        });
      } finally {
        setIsLoading(false);
      }
    }

    void loadCategories();
  }, []);

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
          >
            <Plus className="mr-2 size-4" />
            Add
          </Button>
          <Button onClick={saveCategories} disabled={isSaving || isLoading}>
            <Save className="mr-2 size-4" />
            Save
          </Button>
        </div>
      </div>

      <div className="grid gap-4">
        {categories.map((category, index) => (
          <Card key={`${category.name}-${index}`} className="border-memBorder-primary">
            <CardContent className="grid gap-4 p-5 md:grid-cols-[1fr_1fr_auto]">
              <div className="space-y-2">
                <Label>Name</Label>
                <Input
                  value={category.name}
                  onChange={(event) => updateCategory(index, { name: event.target.value })}
                />
                <Label>Description</Label>
                <Input
                  value={category.description}
                  onChange={(event) =>
                    updateCategory(index, { description: event.target.value })
                  }
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
                />
              </div>
              <div className="flex items-start gap-3">
                <Switch
                  checked={category.enabled}
                  onCheckedChange={(enabled) => updateCategory(index, { enabled })}
                />
                <Button
                  variant="ghost"
                  size="icon"
                  onClick={() =>
                    setCategories((current) =>
                      current.filter((_, itemIndex) => itemIndex !== index),
                    )
                  }
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

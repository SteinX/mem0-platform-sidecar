"use client";

import { useState } from "react";

import { Button } from "@/components/ui/button";
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover";

type MemoryCategoriesProps = {
  categories: string[];
  mobile?: boolean;
};

export function MemoryCategories({
  categories,
  mobile = false,
}: MemoryCategoriesProps) {
  const [open, setOpen] = useState(false);
  const normalized = Array.from(new Set(
    categories.map((category) => category.trim()).filter(Boolean),
  ));

  if (normalized.length === 0) {
    return <span className="text-onSurface-default-tertiary">None</span>;
  }

  if (mobile) {
    return (
      <div className="flex min-w-0 flex-wrap gap-1.5" aria-label="Memory categories">
        {normalized.map((category) => (
          <CategoryChip key={category} category={category} />
        ))}
      </div>
    );
  }

  const remainingCount = normalized.length - 1;
  return (
    <div
      className="flex min-w-0 items-center gap-1.5"
      onClick={(event) => event.stopPropagation()}
      onKeyDown={(event) => event.stopPropagation()}
    >
      <CategoryChip category={normalized[0]} />
      {remainingCount > 0 ? (
        <Popover open={open} onOpenChange={setOpen}>
          <PopoverTrigger asChild>
            <Button
              type="button"
              size="sm"
              variant="outline"
              className="h-auto px-2 py-1 text-xs"
              aria-expanded={open}
              aria-label={`Show ${remainingCount} more categories`}
            >
              +{remainingCount}
            </Button>
          </PopoverTrigger>
          <PopoverContent align="start" className="w-64 max-w-[calc(100vw-2rem)] p-3">
            <p className="mb-2 text-xs font-semibold text-onSurface-default-secondary">
              All categories
            </p>
            <div className="flex flex-wrap gap-1.5">
              {normalized.map((category) => (
                <CategoryChip key={category} category={category} />
              ))}
            </div>
          </PopoverContent>
        </Popover>
      ) : null}
    </div>
  );
}

function CategoryChip({ category }: { category: string }) {
  return (
    <span
      className="inline-flex max-w-full items-center rounded-md border border-memBorder-primary bg-surface-default-fg-secondary px-2 py-1 text-xs text-onSurface-default-secondary"
      title={category}
    >
      <span className="truncate">{category}</span>
    </span>
  );
}

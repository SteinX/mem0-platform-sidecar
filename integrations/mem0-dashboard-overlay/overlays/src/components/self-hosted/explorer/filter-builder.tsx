"use client";

import { useState } from "react";

import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import {
  applyFilterBuilderDraft,
  cancelFilterBuilderDraft,
  changeExplorerFilterField,
  changeExplorerFilterOperator,
  createFilterBuilderDraft,
  filterOperatorsForField,
  openFilterBuilderDraft,
  parseCommaSeparatedFilterValues,
  removeAllFilterBuilderDraft,
  toggleFilterValue,
} from "@/components/self-hosted/explorer/explorer-component-state";
import type {
  ExplorerField,
  ExplorerFilter,
  ExplorerMatch,
  ExplorerOperator,
} from "@/types/dashboard-explorer";
import { createExplorerFilter } from "@/utils/explorer-query-state";

export type ExplorerFilterFieldOption = {
  value: ExplorerField;
  label: string;
  options?: Array<{ value: string; label: string }>;
  operators?: ExplorerOperator[];
};

type FilterBuilderProps = {
  match: ExplorerMatch;
  filters: ExplorerFilter[];
  fields: ExplorerFilterFieldOption[];
  allowAnyMatch?: boolean;
  onApply: (match: ExplorerMatch, filters: ExplorerFilter[]) => void;
  onRemoveAll: (filters: ExplorerFilter[]) => void;
};

const OPERATOR_LABELS: Record<ExplorerOperator, string> = {
  equals: "Equals",
  not_equals: "Does not equal",
  in: "Is any of",
  contains: "Contains",
};

export function FilterBuilder({
  match,
  filters,
  fields,
  allowAnyMatch = true,
  onApply,
  onRemoveAll,
}: FilterBuilderProps) {
  const [draft, setDraft] = useState(() =>
    createFilterBuilderDraft(match, filters),
  );
  const draftMatch = draft.match;
  const draftFilters = draft.filters;

  function handleOpenChange(nextOpen: boolean) {
    setDraft((current) =>
      nextOpen
        ? openFilterBuilderDraft(match, filters)
        : cancelFilterBuilderDraft(current),
    );
  }

  function updateFilter(
    id: string,
    update: (filter: ExplorerFilter) => ExplorerFilter,
  ) {
    setDraft((current) => ({
      ...current,
      filters: current.filters.map((filter) =>
        filter.id === id ? update(filter) : filter,
      ),
    }));
  }

  function addFilter() {
    const firstField = fields[0]?.value ?? "user_id";
    setDraft((current) => ({
      ...current,
      filters: [
        ...current.filters,
        changeExplorerFilterField(createExplorerFilter(), firstField),
      ],
    }));
  }

  function removeFilter(id: string) {
    setDraft((current) => ({
      ...current,
      filters: current.filters.filter((filter) => filter.id !== id),
    }));
  }

  function applyFilters() {
    const applied = applyFilterBuilderDraft(draft);
    setDraft(applied.draft);
    onApply(applied.match, applied.filters);
  }

  function removeAllFilters() {
    const removed = removeAllFilterBuilderDraft(draft);
    setDraft(removed.draft);
    onRemoveAll(removed.filters);
  }

  return (
    <Popover open={draft.open} onOpenChange={handleOpenChange}>
      <PopoverTrigger asChild>
        <Button type="button" variant="outline" aria-label="Edit filters">
          Filters{filters.length > 0 ? ` (${filters.length})` : ""}
        </Button>
      </PopoverTrigger>
      <PopoverContent
        align="start"
        className="w-[min(42rem,calc(100vw-2rem))] max-h-[80vh] overflow-y-auto p-4"
      >
        <div className="space-y-4">
          <div className="space-y-2">
            <Label htmlFor="explorer-filter-match">Match</Label>
            <Select
              disabled={!allowAnyMatch}
              value={draftMatch}
              onValueChange={(value) =>
                setDraft((current) => ({
                  ...current,
                  match: value as ExplorerMatch,
                }))
              }
            >
              <SelectTrigger
                id="explorer-filter-match"
                aria-label="Filter match mode"
              >
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="all">Match all</SelectItem>
                {allowAnyMatch ? (
                  <SelectItem value="any">Match any</SelectItem>
                ) : null}
              </SelectContent>
            </Select>
          </div>

          <div className="space-y-3">
            {draftFilters.map((filter, index) => {
              const field = fields.find(
                (candidate) => candidate.value === filter.field,
              );
              const operators =
                field?.operators ?? filterOperatorsForField(filter.field);
              return (
                <div
                  key={filter.id}
                  role="group"
                  aria-label={`Filter ${index + 1}`}
                  className="grid gap-2 rounded-lg border p-3 sm:grid-cols-[1fr_1fr_2fr_auto]"
                >
                  <div className="space-y-1">
                    <Label htmlFor={`${filter.id}-field`}>
                      Field {index + 1}
                    </Label>
                    <Select
                      value={filter.field}
                      onValueChange={(value) =>
                        updateFilter(filter.id, (current) =>
                          changeExplorerFilterField(
                            current,
                            value as ExplorerField,
                          ),
                        )
                      }
                    >
                      <SelectTrigger
                        id={`${filter.id}-field`}
                        aria-label={`Field for filter ${index + 1}`}
                      >
                        <SelectValue />
                      </SelectTrigger>
                      <SelectContent>
                        {fields.map((option) => (
                          <SelectItem key={option.value} value={option.value}>
                            {option.label}
                          </SelectItem>
                        ))}
                      </SelectContent>
                    </Select>
                  </div>

                  <div className="space-y-1">
                    <Label htmlFor={`${filter.id}-operator`}>
                      Operator {index + 1}
                    </Label>
                    <Select
                      value={filter.operator}
                      onValueChange={(value) =>
                        updateFilter(filter.id, (current) =>
                          changeExplorerFilterOperator(
                            current,
                            value as ExplorerOperator,
                          ),
                        )
                      }
                    >
                      <SelectTrigger
                        id={`${filter.id}-operator`}
                        aria-label={`Operator for filter ${index + 1}`}
                      >
                        <SelectValue />
                      </SelectTrigger>
                      <SelectContent>
                        {operators.map((operator) => (
                          <SelectItem key={operator} value={operator}>
                            {OPERATOR_LABELS[operator]}
                          </SelectItem>
                        ))}
                      </SelectContent>
                    </Select>
                  </div>

                  <div className="space-y-1 sm:self-end">
                    {renderValueEditor(filter, index, field, updateFilter)}
                  </div>

                  <Button
                    type="button"
                    size="icon"
                    variant="ghost"
                    className="sm:self-end"
                    aria-label={`Remove filter ${index + 1}`}
                    onClick={() => removeFilter(filter.id)}
                  >
                    ×
                  </Button>
                </div>
              );
            })}
          </div>

          <Button type="button" size="sm" variant="outline" onClick={addFilter}>
            Add filter
          </Button>

          <div className="flex flex-wrap justify-between gap-2 border-t pt-3">
            <Button
              type="button"
              variant="ghost"
              disabled={filters.length === 0 && draftFilters.length === 0}
              onClick={removeAllFilters}
            >
              Remove filters
            </Button>
            <div className="flex gap-2">
              <Button
                type="button"
                variant="ghost"
                onClick={() => setDraft(cancelFilterBuilderDraft(draft))}
              >
                Cancel
              </Button>
              <Button type="button" onClick={applyFilters}>
                Apply
              </Button>
            </div>
          </div>
        </div>
      </PopoverContent>
    </Popover>
  );
}

function renderValueEditor(
  filter: ExplorerFilter,
  index: number,
  field: ExplorerFilterFieldOption | undefined,
  updateFilter: (
    id: string,
    update: (filter: ExplorerFilter) => ExplorerFilter,
  ) => void,
) {
  if (filter.field === "metadata") {
    const value = metadataValue(filter.value);
    return (
      <div className="grid grid-cols-2 gap-2">
        <Input
          aria-label="Metadata key"
          placeholder="Metadata key"
          value={value.key}
          onChange={(event) =>
            updateFilter(filter.id, (current) => ({
              ...current,
              value: {
                ...metadataValue(current.value),
                key: event.target.value,
              },
            }))
          }
        />
        <Input
          aria-label="Metadata value"
          placeholder="Metadata value"
          value={value.value}
          onChange={(event) =>
            updateFilter(filter.id, (current) => ({
              ...current,
              value: {
                ...metadataValue(current.value),
                value: event.target.value,
              },
            }))
          }
        />
      </div>
    );
  }

  if (filter.operator === "in" && field?.options !== undefined) {
    const selected = arrayValue(filter.value);
    return (
      <fieldset
        className="flex flex-wrap gap-3"
        aria-label={`Values for filter ${index + 1}`}
      >
        <legend className="sr-only">Choose values</legend>
        {field.options.map((option) => {
          const id = `${filter.id}-${option.value}`;
          return (
            <div key={option.value} className="flex items-center gap-2">
              <Checkbox
                id={id}
                checked={selected.includes(option.value)}
                onCheckedChange={(checked) =>
                  updateFilter(filter.id, (current) => ({
                    ...current,
                    value: toggleFilterValue(
                      arrayValue(current.value),
                      option.value,
                      checked === true,
                    ),
                  }))
                }
              />
              <Label htmlFor={id}>{option.label}</Label>
            </div>
          );
        })}
      </fieldset>
    );
  }

  if (filter.operator === "in") {
    return (
      <Input
        aria-label="Comma-separated IDs"
        placeholder="Comma-separated IDs"
        value={arrayValue(filter.value).join(", ")}
        onChange={(event) =>
          updateFilter(filter.id, (current) => ({
            ...current,
            value: parseCommaSeparatedFilterValues(event.target.value),
          }))
        }
      />
    );
  }

  if (field?.options !== undefined) {
    return (
      <Select
        value={scalarValue(filter.value)}
        onValueChange={(value) =>
          updateFilter(filter.id, (current) => ({
            ...current,
            value,
          }))
        }
      >
        <SelectTrigger
          aria-label={`${field.label} value for filter ${index + 1}`}
        >
          <SelectValue placeholder="Choose a value" />
        </SelectTrigger>
        <SelectContent>
          {field.options.map((option) => (
            <SelectItem key={option.value} value={option.value}>
              {option.label}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
    );
  }

  return (
    <Input
      aria-label={`${field?.label ?? filter.field} value for filter ${index + 1}`}
      placeholder="Value"
      value={scalarValue(filter.value)}
      onChange={(event) =>
        updateFilter(filter.id, (current) => ({
          ...current,
          value: event.target.value,
        }))
      }
    />
  );
}

function metadataValue(value: ExplorerFilter["value"]): {
  key: string;
  value: string;
} {
  return !Array.isArray(value) && typeof value === "object"
    ? value
    : { key: "", value: "" };
}

function arrayValue(value: ExplorerFilter["value"]): string[] {
  return Array.isArray(value) ? value : [];
}

function scalarValue(value: ExplorerFilter["value"]): string {
  return typeof value === "string" ? value : "";
}

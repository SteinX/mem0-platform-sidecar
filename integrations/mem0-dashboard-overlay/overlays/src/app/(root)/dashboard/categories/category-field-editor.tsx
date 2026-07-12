"use client";

import { ChevronDown, ChevronUp, Plus, Trash2 } from "lucide-react";

import { Button } from "@/components/ui/button";
import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from "@/components/ui/collapsible";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Switch } from "@/components/ui/switch";
import { Textarea } from "@/components/ui/textarea";
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import {
  CategoryField,
  CategoryFieldType,
  CategoryScalarType,
  createEmptyField,
} from "@/utils/category-schema";

type CategoryFieldEditorProps = {
  fields: CategoryField[];
  errors: Record<string, string>;
  disabled: boolean;
  depth?: 0 | 1;
  onChange: (fields: CategoryField[]) => void;
};

const ROOT_TYPES: { value: CategoryFieldType; label: string }[] = [
  { value: "string", label: "Text" },
  { value: "number", label: "Number" },
  { value: "boolean", label: "Boolean" },
  { value: "date", label: "Date" },
  { value: "enum", label: "Options" },
  { value: "array", label: "List" },
  { value: "object", label: "Object" },
];

const SCALAR_TYPES: { value: CategoryScalarType; label: string }[] = [
  { value: "string", label: "Text" },
  { value: "number", label: "Number" },
  { value: "boolean", label: "Boolean" },
  { value: "date", label: "Date" },
  { value: "enum", label: "Options" },
];

function IconButton({
  label,
  disabled,
  onClick,
  children,
}: {
  label: string;
  disabled: boolean;
  onClick: () => void;
  children: React.ReactNode;
}) {
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <Button
          type="button"
          size="icon"
          variant="ghost"
          className="size-8 shrink-0"
          aria-label={label}
          disabled={disabled}
          onClick={onClick}
        >
          {children}
        </Button>
      </TooltipTrigger>
      <TooltipContent>{label}</TooltipContent>
    </Tooltip>
  );
}

function moveField(fields: CategoryField[], from: number, to: number): CategoryField[] {
  const next = [...fields];
  const [field] = next.splice(from, 1);
  next.splice(to, 0, field);
  return next;
}

function EnumOptions({
  values,
  disabled,
  onChange,
}: {
  values: string[];
  disabled: boolean;
  onChange: (values: string[]) => void;
}) {
  return (
    <div className="space-y-2">
      <Label>Options</Label>
      {values.map((value, index) => (
        <div key={index} className="flex min-w-0 items-center gap-2">
          <Input
            value={value}
            aria-label={`Option ${index + 1}`}
            disabled={disabled}
            onChange={(event) =>
              onChange(values.map((item, itemIndex) =>
                itemIndex === index ? event.target.value : item,
              ))
            }
          />
          <IconButton
            label={`Remove option ${index + 1}`}
            disabled={disabled}
            onClick={() => onChange(values.filter((_, itemIndex) => itemIndex !== index))}
          >
            <Trash2 className="size-4" />
          </IconButton>
        </div>
      ))}
      <Button
        type="button"
        size="sm"
        variant="outline"
        disabled={disabled}
        onClick={() => onChange([...values, ""])}
      >
        <Plus className="mr-2 size-4" />
        Add option
      </Button>
    </div>
  );
}

export function CategoryFieldEditor({
  fields,
  errors,
  disabled,
  depth = 0,
  onChange,
}: CategoryFieldEditorProps) {
  const updateField = (index: number, updates: Partial<CategoryField>) => {
    onChange(fields.map((field, itemIndex) =>
      itemIndex === index ? { ...field, ...updates } : field,
    ));
  };

  return (
    <div className="space-y-3">
      {fields.map((field, index) => {
        const fieldTypes = depth === 1
          ? ROOT_TYPES.filter((type) => type.value !== "object")
          : ROOT_TYPES;
        const errorId = `${field.id}-error`;

        return (
          <div
            key={field.id}
            role="group"
            aria-describedby={errors[field.id] ? errorId : undefined}
            className="border-b border-memBorder-primary pb-4 last:border-0"
          >
            <div className="grid min-w-0 gap-3 sm:grid-cols-[minmax(0,1fr)_160px_auto]">
              <div className="min-w-0 space-y-1.5">
                <Label htmlFor={`${field.id}-key`}>Field key</Label>
                <Input
                  id={`${field.id}-key`}
                  value={field.key}
                  placeholder="field_name"
                  disabled={disabled}
                  aria-invalid={Boolean(errors[field.id])}
                  aria-describedby={errors[field.id] ? errorId : undefined}
                  onChange={(event) => updateField(index, { key: event.target.value })}
                />
              </div>
              <div className="space-y-1.5">
                <Label htmlFor={`${field.id}-type`}>Type</Label>
                <Select
                  value={field.type}
                  disabled={disabled}
                  onValueChange={(value: CategoryFieldType) => updateField(index, { type: value })}
                >
                  <SelectTrigger
                    id={`${field.id}-type`}
                    aria-label={`Type for ${field.key || `field ${index + 1}`}`}
                  >
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    {fieldTypes.map((type) => (
                      <SelectItem key={type.value} value={type.value}>{type.label}</SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </div>
              <div className="flex items-end justify-end gap-0.5">
                <IconButton
                  label="Move field up"
                  disabled={disabled || index === 0}
                  onClick={() => onChange(moveField(fields, index, index - 1))}
                >
                  <ChevronUp className="size-4" />
                </IconButton>
                <IconButton
                  label="Move field down"
                  disabled={disabled || index === fields.length - 1}
                  onClick={() => onChange(moveField(fields, index, index + 1))}
                >
                  <ChevronDown className="size-4" />
                </IconButton>
                <IconButton
                  label="Remove field"
                  disabled={disabled}
                  onClick={() => onChange(fields.filter((_, itemIndex) => itemIndex !== index))}
                >
                  <Trash2 className="size-4" />
                </IconButton>
              </div>
            </div>

            {errors[field.id] ? (
              <p id={errorId} className="mt-1 text-xs text-onSurface-danger-primary">
                {errors[field.id]}
              </p>
            ) : null}

            <div className="mt-3 flex flex-wrap items-center gap-x-6 gap-y-3">
              <label className="flex items-center gap-2 text-sm">
                <Switch
                  checked={field.required}
                  disabled={disabled}
                  onCheckedChange={(required) => updateField(index, { required })}
                />
                Required
              </label>
              <label className="flex items-center gap-2 text-sm">
                <Switch
                  checked={field.hasDefault}
                  disabled={disabled}
                  onCheckedChange={(hasDefault) => updateField(index, { hasDefault })}
                />
                Default value
              </label>
            </div>

            {field.hasDefault ? (
              <div className="mt-3 max-w-md space-y-1.5">
                <Label htmlFor={`${field.id}-default`}>Default</Label>
                {field.type === "enum" ? (
                  <Select
                    value={field.defaultValue || undefined}
                    disabled={disabled}
                    onValueChange={(defaultValue) => updateField(index, { defaultValue })}
                  >
                    <SelectTrigger id={`${field.id}-default`}>
                      <SelectValue placeholder="Select an option" />
                    </SelectTrigger>
                    <SelectContent>
                      {field.enumValues
                        .filter((value) => value.trim())
                        .map((value, optionIndex) => (
                          <SelectItem key={`${value}-${optionIndex}`} value={value}>
                            {value}
                          </SelectItem>
                        ))}
                    </SelectContent>
                  </Select>
                ) : field.type === "boolean" ? (
                  <Select
                    value={field.defaultValue || "false"}
                    disabled={disabled}
                    onValueChange={(defaultValue) => updateField(index, { defaultValue })}
                  >
                    <SelectTrigger id={`${field.id}-default`}><SelectValue /></SelectTrigger>
                    <SelectContent>
                      <SelectItem value="false">False</SelectItem>
                      <SelectItem value="true">True</SelectItem>
                    </SelectContent>
                  </Select>
                ) : field.type === "object" || field.type === "array" ? (
                  <Textarea
                    id={`${field.id}-default`}
                    value={field.defaultValue}
                    placeholder={field.type === "array" ? "[]" : "{}"}
                    disabled={disabled}
                    onChange={(event) => updateField(index, { defaultValue: event.target.value })}
                  />
                ) : (
                  <Input
                    id={`${field.id}-default`}
                    type={field.type === "number" ? "number" : field.type === "date" ? "date" : "text"}
                    value={field.defaultValue}
                    disabled={disabled}
                    onChange={(event) => updateField(index, { defaultValue: event.target.value })}
                  />
                )}
              </div>
            ) : null}

            {field.type === "enum" ? (
              <div className="mt-3 max-w-md">
                <EnumOptions
                  values={field.enumValues}
                  disabled={disabled}
                  onChange={(enumValues) => updateField(index, { enumValues })}
                />
              </div>
            ) : null}

            {field.type === "array" ? (
              <div className="mt-3 max-w-md space-y-3">
                <div className="space-y-1.5">
                  <Label htmlFor={`${field.id}-array-item-type`}>List item type</Label>
                  <Select
                    value={field.arrayItemType}
                    disabled={disabled}
                    onValueChange={(arrayItemType: CategoryScalarType) =>
                      updateField(index, { arrayItemType })
                    }
                  >
                    <SelectTrigger id={`${field.id}-array-item-type`}><SelectValue /></SelectTrigger>
                    <SelectContent>
                      {SCALAR_TYPES.map((type) => (
                        <SelectItem key={type.value} value={type.value}>{type.label}</SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                </div>
                {field.arrayItemType === "enum" ? (
                  <EnumOptions
                    values={field.arrayEnumValues}
                    disabled={disabled}
                    onChange={(arrayEnumValues) => updateField(index, { arrayEnumValues })}
                  />
                ) : null}
              </div>
            ) : null}

            {field.type === "object" && depth === 0 ? (
              <div className="mt-4 border-l-2 border-memBorder-primary pl-4">
                <p className="mb-3 text-sm font-medium">Object fields</p>
                <CategoryFieldEditor
                  fields={field.children}
                  errors={errors}
                  disabled={disabled}
                  depth={1}
                  onChange={(children) => updateField(index, { children })}
                />
              </div>
            ) : null}

            <Collapsible className="mt-3">
              <CollapsibleTrigger asChild>
                <Button type="button" size="sm" variant="ghost" disabled={disabled}>
                  More settings
                </Button>
              </CollapsibleTrigger>
              <CollapsibleContent className="grid gap-3 pt-2 sm:grid-cols-2">
                <div className="space-y-1.5">
                  <Label htmlFor={`${field.id}-title`}>Display name</Label>
                  <Input
                    id={`${field.id}-title`}
                    value={field.title}
                    disabled={disabled}
                    onChange={(event) => updateField(index, { title: event.target.value })}
                  />
                </div>
                <div className="space-y-1.5 sm:col-span-2">
                  <Label htmlFor={`${field.id}-description`}>Description</Label>
                  <Textarea
                    id={`${field.id}-description`}
                    value={field.description}
                    disabled={disabled}
                    onChange={(event) => updateField(index, { description: event.target.value })}
                  />
                </div>
              </CollapsibleContent>
            </Collapsible>
          </div>
        );
      })}

      <Button
        type="button"
        variant="outline"
        disabled={disabled}
        onClick={() => onChange([...fields, createEmptyField()])}
      >
        <Plus className="mr-2 size-4" />
        Add field
      </Button>
    </div>
  );
}

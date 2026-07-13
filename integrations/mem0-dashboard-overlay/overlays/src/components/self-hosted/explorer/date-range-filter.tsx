"use client";

import { useEffect, useState } from "react";
import type { DateRange as CalendarDateRange } from "react-day-picker";

import { Button } from "@/components/ui/button";
import { Calendar } from "@/components/ui/calendar";
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover";
import {
  calendarRangeToUtcRange,
  formatDateRangeLabel,
  isoRangeToCalendarRange,
} from "@/components/self-hosted/explorer/explorer-component-state";
import type { ExplorerDateRange } from "@/types/dashboard-explorer";
import { datePresetRange } from "@/utils/explorer-query-state";

type DatePreset = "all" | "1d" | "7d" | "30d";

type DateRangeFilterProps = {
  value: ExplorerDateRange;
  onChange: (value: ExplorerDateRange) => void;
};

const DATE_PRESETS: Array<{ label: string; preset: DatePreset }> = [
  { label: "All time", preset: "all" },
  { label: "Last 24 hours", preset: "1d" },
  { label: "Last 7 days", preset: "7d" },
  { label: "Last 30 days", preset: "30d" },
];

export function DateRangeFilter({ value, onChange }: DateRangeFilterProps) {
  const rangeLabel = formatDateRangeLabel(value);
  const [open, setOpen] = useState(false);
  const [draftRange, setDraftRange] = useState<CalendarDateRange | undefined>(
    () => isoRangeToCalendarRange(value),
  );
  // Starting with the mobile layout keeps the server and first client render equal.
  const [isDesktop, setIsDesktop] = useState(false);

  useEffect(() => {
    const media = window.matchMedia("(min-width: 768px)");
    const updateLayout = () => setIsDesktop(media.matches);
    updateLayout();
    media.addEventListener("change", updateLayout);
    return () => media.removeEventListener("change", updateLayout);
  }, []);

  function handleOpenChange(nextOpen: boolean) {
    if (nextOpen) {
      setDraftRange(isoRangeToCalendarRange(value));
    }
    setOpen(nextOpen);
  }

  function applyPreset(preset: DatePreset) {
    onChange(datePresetRange(preset));
    setOpen(false);
  }

  function applyCustomRange() {
    const range = draftRange === undefined
      ? null
      : calendarRangeToUtcRange(draftRange);
    if (range === null) {
      return;
    }
    onChange(range);
    setOpen(false);
  }

  return (
    <Popover open={open} onOpenChange={handleOpenChange}>
      <PopoverTrigger asChild>
        <Button
          type="button"
          variant="outline"
          aria-label={`Choose date range: ${rangeLabel}`}
        >
          {rangeLabel}
        </Button>
      </PopoverTrigger>
      <PopoverContent align="start" className="w-auto max-w-[calc(100vw-2rem)] p-3">
        <div className="flex flex-wrap gap-2" aria-label="Date range presets">
          {DATE_PRESETS.map(({ label, preset }) => (
            <Button
              key={preset}
              type="button"
              size="sm"
              variant="ghost"
              onClick={() => applyPreset(preset)}
            >
              {label}
            </Button>
          ))}
        </div>
        <Calendar
          mode="range"
          numberOfMonths={isDesktop ? 2 : 1}
          selected={draftRange}
          defaultMonth={draftRange?.from}
          onSelect={setDraftRange}
          initialFocus
        />
        <div className="flex justify-end gap-2 border-t pt-3">
          <Button type="button" variant="ghost" onClick={() => setOpen(false)}>
            Cancel
          </Button>
          <Button
            type="button"
            onClick={applyCustomRange}
            disabled={draftRange?.from === undefined || draftRange.to === undefined}
          >
            Apply
          </Button>
        </div>
      </PopoverContent>
    </Popover>
  );
}

"use client";

import { TooltipProvider } from "@/components/ui/tooltip";

export const SIDEBAR_WIDTH = 180;
export const COLLAPSED_SIDEBAR_WIDTH = 64;
export const COLLAPSED_SIDEBAR_PADDING = 16;
export const COLLAPSED_SIDEBAR_WIDTH_WITHOUT_PADDING =
  COLLAPSED_SIDEBAR_WIDTH - COLLAPSED_SIDEBAR_PADDING;

export const ClientLayout: React.FC<{ children: React.ReactNode }> = ({
  children,
}) => <TooltipProvider delayDuration={0}>{children}</TooltipProvider>;

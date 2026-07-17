"use client";

import React from "react";
import dynamic from "next/dynamic";
import { Provider } from "react-redux";
import { ThemeProvider } from "@/components/theme-provider";
import { cn } from "@/lib/utils";
import store from "@/store/store";
import "@/styles/globals.css";
import { ClientLayout } from "./clientLayout";
import { DMMono, Fustat, Inter, InterDisplay, Roboto } from "./fonts";

const Toaster = dynamic(
  () =>
    import("@/components/ui/sonner").then((mod) => ({ default: mod.Toaster })),
  {
    ssr: false,
  },
);

export function DashboardClientLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="en" suppressHydrationWarning>
      <body
        className={cn(
          Inter.className,
          InterDisplay.variable,
          Roboto.variable,
          Fustat.variable,
          DMMono.variable,
        )}
        suppressHydrationWarning
      >
        <Provider store={store}>
          <ThemeProvider
            attribute="class"
            defaultTheme="light"
            enableSystem
            disableTransitionOnChange
          >
            <ClientLayout>{children}</ClientLayout>
            <Toaster />
          </ThemeProvider>
        </Provider>
      </body>
    </html>
  );
}

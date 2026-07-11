"use client";

import * as React from "react";
import Link from "next/link";
import {
  Activity,
  ChartLine,
  ChevronDown,
  FolderInput,
  GalleryVerticalEnd,
  KeyRound,
  Settings,
  Tags,
  Users,
  WebhookIcon,
  Wrench,
} from "lucide-react";
import { useDispatch, useSelector } from "react-redux";
import { RootState } from "@/store/store";
import { toggleSidebar } from "@/store/reducers/layoutReducer";
import { Badge } from "@/components/ui/badge";
import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from "@/components/ui/collapsible";
import {
  Sidebar,
  SidebarContent,
  SidebarGroup,
  SidebarGroupLabel,
  SidebarMenu,
  SidebarMenuButton,
  SidebarMenuItem,
  SidebarRail,
} from "@/components/ui/sidebar";
import { usePathname } from "next/navigation";
import { cn } from "@/lib/utils";

type NavigationItem = {
  title: string;
  url: string;
  icon: React.ElementType;
  badge?: string;
};

type NavigationItemsProps = {
  group: "memory-tools" | "cloud-features";
  items: readonly NavigationItem[];
  pathname: string;
  isSidebarCollapsed: boolean;
};

const MEMORY_TOOL_ITEMS: NavigationItem[] = [
  {
    title: "Categories",
    url: "/dashboard/categories",
    icon: Tags,
  },
  {
    title: "Export",
    url: "/dashboard/export",
    icon: FolderInput,
  },
];

const CLOUD_FEATURE_ITEMS: NavigationItem[] = [
  {
    title: "Webhooks",
    url: "/dashboard/webhooks",
    icon: WebhookIcon,
    badge: "PRO",
  },
  {
    title: "Analytics",
    url: "/dashboard/analytics",
    icon: ChartLine,
    badge: "PRO",
  },
];

function NavigationItems({
  items,
  pathname,
  isSidebarCollapsed,
}: NavigationItemsProps) {
  return (
    <>
      {items.map((item) => (
        <SidebarMenuItem key={item.title}>
          <SidebarMenuButton
            asChild
            collapsed={isSidebarCollapsed}
            active={pathname === item.url}
            tooltip={isSidebarCollapsed ? item.title : undefined}
          >
            <Link
              href={item.url}
              className={cn(
                "flex items-center w-full",
                isSidebarCollapsed ? "justify-center mx-auto" : "gap-1.5",
              )}
            >
              <item.icon className="size-4 shrink-0" />
              {!isSidebarCollapsed && (
                <>
                  <span>{item.title}</span>
                  {item.badge && (
                    <Badge
                      variant="outline"
                      className="ml-auto text-memGold-600 border-memGold-300 typo-caption-sm px-1.5 py-0"
                    >
                      {item.badge}
                    </Badge>
                  )}
                </>
              )}
            </Link>
          </SidebarMenuButton>
        </SidebarMenuItem>
      ))}
    </>
  );
}

export function MainNav({
  className,
  ...props
}: React.HTMLAttributes<HTMLElement>) {
  const pathname = usePathname();
  const dispatch = useDispatch();
  const isSidebarCollapsed = useSelector(
    (state: RootState) => state.layout.isSidebarCollapsed,
  );
  const [isCloudOpen, setIsCloudOpen] = React.useState(true);

  React.useEffect(() => {
    const sidebarMediaQuery = window.matchMedia("(max-width: 767px)");
    const collapseSidebarOnNarrowViewport = () => {
      if (sidebarMediaQuery.matches && !isSidebarCollapsed) {
        dispatch(toggleSidebar());
      }
    };

    collapseSidebarOnNarrowViewport();
    sidebarMediaQuery.addEventListener("change", collapseSidebarOnNarrowViewport);
    return () => {
      sidebarMediaQuery.removeEventListener(
        "change",
        collapseSidebarOnNarrowViewport,
      );
    };
  }, [dispatch, isSidebarCollapsed]);

  return (
    <Sidebar
      collapsible={isSidebarCollapsed ? "icon" : undefined}
      className={cn(className, "border-r-0 w-full mb-0 bg-transparent")}
      {...props}
    >
      <SidebarContent>
        <SidebarGroup>
          <SidebarMenu className="gap-0">
            <div className="flex flex-col gap-3">
              <div className="flex flex-col gap-0">
                {!isSidebarCollapsed && (
                  <SidebarGroupLabel className="mb-0">
                    ACTIVITY
                  </SidebarGroupLabel>
                )}
                {[
                  {
                    title: "Requests",
                    url: "/dashboard/requests",
                    icon: Activity,
                    active: pathname === "/dashboard/requests",
                  },
                  {
                    title: "Memories",
                    url: "/dashboard/memories",
                    icon: GalleryVerticalEnd,
                    active: pathname === "/dashboard/memories",
                  },
                  {
                    title: "Entities",
                    url: "/dashboard/entities",
                    icon: Users,
                    active: pathname === "/dashboard/entities",
                  },
                ].map((item) => (
                  <SidebarMenuItem key={item.title}>
                    <SidebarMenuButton
                      asChild
                      collapsed={isSidebarCollapsed}
                      active={item.active}
                      tooltip={isSidebarCollapsed ? item.title : undefined}
                    >
                      <Link
                        href={item.url}
                        className={cn(
                          "flex items-center w-full",
                          isSidebarCollapsed
                            ? "justify-center mx-auto"
                            : "gap-1.5",
                        )}
                      >
                        <item.icon className="size-4 shrink-0" />
                        {!isSidebarCollapsed && <span>{item.title}</span>}
                      </Link>
                    </SidebarMenuButton>
                  </SidebarMenuItem>
                ))}
              </div>

              {isSidebarCollapsed && (
                <div className="h-[1px] w-full bg-memBorder-primary my-2" />
              )}

              <div className="flex flex-col gap-0">
                {!isSidebarCollapsed && (
                  <SidebarGroupLabel className="mb-0">
                    MEMORY TOOLS
                  </SidebarGroupLabel>
                )}
                <NavigationItems
                  group="memory-tools"
                  items={MEMORY_TOOL_ITEMS}
                  pathname={pathname}
                  isSidebarCollapsed={isSidebarCollapsed}
                />
              </div>

              {isSidebarCollapsed && (
                <div className="h-[1px] w-full bg-memBorder-primary my-2" />
              )}

              <Collapsible
                open={isCloudOpen}
                onOpenChange={setIsCloudOpen}
                className="flex flex-col gap-0"
              >
                {!isSidebarCollapsed && (
                  <CollapsibleTrigger asChild>
                    <SidebarGroupLabel className="cursor-pointer mb-0">
                      CLOUD FEATURES
                      <ChevronDown
                        className={cn(
                          "size-3 transition-transform duration-200",
                          isCloudOpen ? "" : "-rotate-90",
                        )}
                      />
                    </SidebarGroupLabel>
                  </CollapsibleTrigger>
                )}
                <CollapsibleContent className="flex flex-col gap-0">
                  <NavigationItems
                    group="cloud-features"
                    items={CLOUD_FEATURE_ITEMS}
                    pathname={pathname}
                    isSidebarCollapsed={isSidebarCollapsed}
                  />
                </CollapsibleContent>
              </Collapsible>

              {isSidebarCollapsed && (
                <div className="h-[1px] w-full bg-memBorder-primary my-2" />
              )}

              <div className="flex flex-col gap-0">
                {!isSidebarCollapsed && (
                  <SidebarGroupLabel className="mb-0">
                    ACCOUNT
                  </SidebarGroupLabel>
                )}
                {[
                  {
                    title: "API Keys",
                    url: "/dashboard/api-keys",
                    icon: KeyRound,
                    active: pathname === "/dashboard/api-keys",
                  },
                  {
                    title: "Configuration",
                    url: "/dashboard/configuration",
                    icon: Wrench,
                    active: pathname === "/dashboard/configuration",
                  },
                  {
                    title: "Settings",
                    url: "/dashboard/settings",
                    icon: Settings,
                    active: pathname === "/dashboard/settings",
                  },
                ].map((item) => (
                  <SidebarMenuItem key={item.title}>
                    <SidebarMenuButton
                      asChild
                      collapsed={isSidebarCollapsed}
                      active={item.active}
                      tooltip={isSidebarCollapsed ? item.title : undefined}
                    >
                      <Link
                        href={item.url}
                        className={cn(
                          "flex items-center w-full",
                          isSidebarCollapsed
                            ? "justify-center mx-auto"
                            : "gap-1.5",
                        )}
                      >
                        <item.icon className="size-4 shrink-0" />
                        {!isSidebarCollapsed && <span>{item.title}</span>}
                      </Link>
                    </SidebarMenuButton>
                  </SidebarMenuItem>
                ))}
              </div>
            </div>
          </SidebarMenu>
        </SidebarGroup>
      </SidebarContent>
      <SidebarRail />
    </Sidebar>
  );
}

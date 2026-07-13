"use client";

import { Button } from "@/components/ui/button";
import {
  createEntityBadgeItems,
  entityBadgeClickPayload,
  truncateIdentity,
} from "@/components/self-hosted/explorer/explorer-component-state";
import type { EntityBadgeItem } from "@/components/self-hosted/explorer/explorer-component-state";

type EntityBadgesProps = {
  userId?: string | null;
  agentId?: string | null;
  appId?: string | null;
  runId?: string | null;
  entity?: {
    type: "user" | "agent" | "app" | "run";
    id: string;
    displayName?: string | null;
  };
  onBadgeClick?: (identity: Pick<EntityBadgeItem, "field" | "value">) => void;
};

type RenderedEntityBadge = EntityBadgeItem & {
  displayValue?: string;
};

const SINGLE_ENTITY_FIELDS: Record<
  NonNullable<EntityBadgesProps["entity"]>["type"],
  Pick<EntityBadgeItem, "field" | "label">
> = {
  user: { field: "user_id", label: "User" },
  agent: { field: "agent_id", label: "Agent" },
  app: { field: "app_id", label: "App" },
  run: { field: "run_id", label: "Run" },
};

export function EntityBadges({
  userId,
  agentId,
  appId,
  runId,
  entity,
  onBadgeClick,
}: EntityBadgesProps) {
  const identities: RenderedEntityBadge[] =
    entity === undefined
      ? createEntityBadgeItems({ userId, agentId, appId, runId })
      : entity.id.trim() === ""
        ? []
        : [
            {
              ...SINGLE_ENTITY_FIELDS[entity.type],
              value: entity.id,
              displayValue: entity.displayName?.trim() || undefined,
            },
          ];

  if (identities.length === 0) {
    return null;
  }

  return (
    <div
      className="flex flex-wrap gap-1.5"
      aria-label={
        entity === undefined ? "Memory identities" : "Entity identity"
      }
    >
      {identities.map((identity) =>
        onBadgeClick ? (
          <Button
            key={identity.field}
            type="button"
            size="sm"
            variant="outline"
            className="h-auto max-w-48 gap-1 px-2 py-1 font-mono text-xs"
            title={identity.value}
            aria-label={`Filter by ${identity.label} ${identity.value}`}
            onClick={() => onBadgeClick(entityBadgeClickPayload(identity))}
          >
            <span className="font-sans font-semibold">{identity.label}</span>
            <span className="truncate">
              {identity.displayValue ?? truncateIdentity(identity.value)}
            </span>
          </Button>
        ) : (
          <span
            key={identity.field}
            className="inline-flex max-w-48 items-center gap-1 rounded-md border px-2 py-1 font-mono text-xs"
            title={identity.value}
            aria-label={`${identity.label} entity ${identity.value}`}
            tabIndex={0}
          >
            <span className="font-sans font-semibold">{identity.label}</span>
            <span className="truncate">
              {identity.displayValue ?? truncateIdentity(identity.value)}
            </span>
          </span>
        ),
      )}
    </div>
  );
}

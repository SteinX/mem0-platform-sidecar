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
  onBadgeClick?: (identity: Pick<EntityBadgeItem, "field" | "value">) => void;
};

export function EntityBadges({
  userId,
  agentId,
  appId,
  runId,
  onBadgeClick,
}: EntityBadgesProps) {
  const identities = createEntityBadgeItems({ userId, agentId, appId, runId });

  if (identities.length === 0) {
    return null;
  }

  return (
    <div className="flex flex-wrap gap-1.5" aria-label="Memory identities">
      {identities.map((identity) => onBadgeClick ? (
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
          <span className="truncate">{truncateIdentity(identity.value)}</span>
        </Button>
      ) : (
        <span
          key={identity.field}
          className="inline-flex max-w-48 items-center gap-1 rounded-md border px-2 py-1 font-mono text-xs"
          title={identity.value}
        >
          <span className="font-sans font-semibold">{identity.label}</span>
          <span className="truncate">{truncateIdentity(identity.value)}</span>
        </span>
      ))}
    </div>
  );
}

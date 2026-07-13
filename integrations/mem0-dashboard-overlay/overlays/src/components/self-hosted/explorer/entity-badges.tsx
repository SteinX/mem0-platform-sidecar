"use client";

import { Button } from "@/components/ui/button";

type EntityField = "user_id" | "agent_id" | "app_id" | "run_id";

type EntityBadgeClick = {
  field: EntityField;
  value: string;
};

type EntityBadgesProps = {
  userId?: string | null;
  agentId?: string | null;
  appId?: string | null;
  runId?: string | null;
  onBadgeClick?: (identity: EntityBadgeClick) => void;
};

type IdentityCandidate = {
  field: EntityField;
  label: string;
  value: string | null | undefined;
};

type Identity = Omit<IdentityCandidate, "value"> & { value: string };

export function EntityBadges({
  userId,
  agentId,
  appId,
  runId,
  onBadgeClick,
}: EntityBadgesProps) {
  const identities: Identity[] = [
    { field: "user_id", label: "User", value: userId },
    { field: "agent_id", label: "Agent", value: agentId },
    { field: "app_id", label: "App", value: appId },
    { field: "run_id", label: "Run", value: runId },
  ].filter((identity): identity is Identity => Boolean(identity.value));

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
          onClick={() => onBadgeClick({ field: identity.field, value: identity.value })}
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

function truncateIdentity(value: string): string {
  return value.length <= 18 ? value : `${value.slice(0, 9)}…${value.slice(-6)}`;
}

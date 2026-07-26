"use client";

import type { ReactNode } from "react";
import { useState } from "react";
import { format } from "date-fns";
import { Check, Copy, KeyRound, Plus, Trash2 } from "lucide-react";
import { CopyToClipboard } from "react-copy-to-clipboard";

import { DataTable } from "@/components/shared/data-table";
import { TableSkeleton } from "@/components/shared/table-skeleton";
import { EmptyState } from "@/components/self-hosted/empty-state";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import DeleteConfirmationModal from "@/components/ui/delete-confirmation-modal";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { toast } from "@/components/ui/use-toast";
import { useApiQuery } from "@/hooks/use-api-query";
import { getErrorMessage } from "@/lib/error-message";
import type { ApiKey, ApiKeyCreateResponse } from "@/types/api";
import { api } from "@/utils/api";
import { API_KEY_ENDPOINTS } from "@/utils/api-endpoints";

type ApiKeyColumn = {
  readonly key: keyof ApiKey;
  readonly label: string;
  readonly width: number;
  readonly render?: (value: ApiKey[keyof ApiKey], row: ApiKey) => ReactNode;
};

export default function ApiKeysPage() {
  const [createOpen, setCreateOpen] = useState(false);
  const [newLabel, setNewLabel] = useState("");
  const [newKey, setNewKey] = useState("");
  const [copied, setCopied] = useState(false);
  const [keyToRevoke, setKeyToRevoke] = useState<ApiKey | null>(null);

  const {
    data: keys = [],
    isLoading,
    refetch,
  } = useApiQuery<ApiKey[]>(
    async () => {
      const response = await api.get<ApiKey[]>(API_KEY_ENDPOINTS.BASE);
      return response.data ?? [];
    },
    { errorToast: "Failed to load client keys", initialData: [] },
  );

  const handleCreate = async () => {
    const label = newLabel.trim();
    if (label === "") {
      return;
    }
    try {
      const response = await api.post<ApiKeyCreateResponse>(
        API_KEY_ENDPOINTS.BASE,
        { label },
      );
      setNewKey(response.data.key);
      setNewLabel(label);
      void refetch();
    } catch (error) {
      if (!(error instanceof Error)) {
        throw error;
      }
      toast({
        title: "Failed to create key",
        description: getErrorMessage(error),
        variant: "destructive",
      });
    }
  };

  const handleRevoke = async () => {
    if (keyToRevoke === null) {
      return;
    }
    try {
      await api.delete(API_KEY_ENDPOINTS.BY_ID(keyToRevoke.id));
      toast({ title: "Client key revoked", variant: "success" });
      setKeyToRevoke(null);
      void refetch();
    } catch (error) {
      if (!(error instanceof Error)) {
        throw error;
      }
      toast({
        title: "Failed to revoke key",
        description: getErrorMessage(error),
        variant: "destructive",
      });
    }
  };

  const handleDialogClose = (open: boolean) => {
    if (!open) {
      setNewKey("");
      setNewLabel("");
      setCopied(false);
    }
    setCreateOpen(open);
  };

  const columns: ApiKeyColumn[] = [
    { key: "label", label: "Client", width: 30 },
    {
      key: "key_prefix",
      label: "Key prefix",
      width: 22,
      render: (value) => (
        <code className="font-mono text-xs">
          {typeof value === "string" ? value : ""}...
        </code>
      ),
    },
    {
      key: "created_at",
      label: "Created",
      width: 20,
      render: (value) =>
        typeof value === "string" ? formatDate(value) : "Unknown",
    },
    {
      key: "last_used_at",
      label: "Last used",
      width: 20,
      render: (value) =>
        typeof value === "string" ? formatDate(value) : "Never",
    },
    {
      key: "id",
      label: "",
      width: 8,
      render: (_value, row) => (
        <Button
          type="button"
          variant="ghost"
          size="icon"
          aria-label={`Revoke ${row.label}`}
          onClick={() => setKeyToRevoke(row)}
          className="size-7"
        >
          <Trash2 className="size-3.5 text-onSurface-danger-primary" />
        </Button>
      ),
    },
  ];

  return (
    <div className="min-w-0 space-y-5">
      <div className="flex min-w-0 flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
        <div className="min-w-0 space-y-1">
          <h1 className="font-fustat text-xl font-semibold">
            API &amp; MCP Client Keys
          </h1>
          <p className="max-w-3xl break-words text-sm text-onSurface-default-secondary">
            Create one named key per client. The same key authenticates Mem0
            REST calls and the remote MCP endpoint.
          </p>
        </div>
        <Dialog open={createOpen} onOpenChange={handleDialogClose}>
          <DialogTrigger asChild>
            <Button size="sm">
              <Plus className="mr-1 size-4" />
              Create client key
            </Button>
          </DialogTrigger>
          <DialogContent>
            <DialogHeader>
              <DialogTitle>Create client key</DialogTitle>
            </DialogHeader>
            {newKey === "" ? (
              <div className="mt-2 space-y-4">
                <div className="space-y-2">
                  <Label htmlFor="api-key-label">Client label</Label>
                  <Input
                    id="api-key-label"
                    value={newLabel}
                    onChange={(event) => setNewLabel(event.target.value)}
                    placeholder="OpenCode laptop"
                    autoComplete="off"
                  />
                  <p className="text-xs text-onSurface-default-secondary">
                    Use a distinct label for each machine, agent, or integration
                    so Requests can attribute its activity.
                  </p>
                </div>
                <Button
                  type="button"
                  onClick={() => void handleCreate()}
                  disabled={newLabel.trim() === ""}
                  className="w-full"
                >
                  Create
                </Button>
              </div>
            ) : (
              <div className="mt-2 space-y-4">
                <div className="space-y-2">
                  <Label htmlFor="api-key-new">Client key</Label>
                  <div className="flex min-w-0 gap-2">
                    <Input
                      id="api-key-new"
                      value={newKey}
                      readOnly
                      className="min-w-0 font-mono text-sm"
                    />
                    <CopyToClipboard
                      text={newKey}
                      onCopy={() => setCopied(true)}
                    >
                      <Button
                        type="button"
                        variant="outline"
                        size="icon"
                        aria-label="Copy client key"
                      >
                        {copied ? (
                          <Check className="size-4" />
                        ) : (
                          <Copy className="size-4" />
                        )}
                      </Button>
                    </CopyToClipboard>
                  </div>
                  <p className="text-xs text-onSurface-danger-primary">
                    Save this key now. It is shown only once.
                  </p>
                </div>
                <div className="space-y-2 rounded-md border border-memBorder-primary p-3 text-xs text-onSurface-default-secondary">
                  <p>
                    REST: send this value in the{" "}
                    <code className="font-mono">X-API-Key</code> header.
                  </p>
                  <p>
                    Remote MCP: send{" "}
                    <code className="break-all font-mono">
                      Authorization: Bearer &lt;key&gt;
                    </code>
                    .
                  </p>
                </div>
                <Button
                  type="button"
                  onClick={() => handleDialogClose(false)}
                  className="w-full"
                >
                  Done
                </Button>
              </div>
            )}
          </DialogContent>
        </Dialog>
      </div>

      <section
        aria-labelledby="client-key-usage"
        className="flex min-w-0 items-start gap-3 rounded-md border border-memBorder-primary p-4"
      >
        <KeyRound className="mt-0.5 size-4 shrink-0" />
        <div className="min-w-0 space-y-1">
          <h2 id="client-key-usage" className="font-semibold">
            Migration-safe authentication
          </h2>
          <p className="break-words text-sm text-onSurface-default-secondary">
            New clients should use a named key from this page. The legacy shared
            MCP token remains available only during hybrid-mode migration and
            does not appear in this list.
          </p>
        </div>
      </section>

      {isLoading ? (
        <TableSkeleton rows={3} columns={5} />
      ) : keys.length === 0 ? (
        <EmptyState
          title="No client keys yet"
          description="Create a named key for your first REST or remote MCP client."
        />
      ) : (
        <>
          <Card className="hidden overflow-hidden border-memBorder-primary md:block">
            <DataTable
              data={keys}
              columns={columns}
              getRowKey={(row) => row.id}
            />
          </Card>
          <div className="space-y-2 md:hidden">
            {keys.map((key) => (
              <Card
                key={key.id}
                className="min-w-0 space-y-3 border-memBorder-primary p-4"
              >
                <div className="flex min-w-0 items-start justify-between gap-3">
                  <div className="min-w-0">
                    <p className="truncate font-medium" title={key.label}>
                      {key.label}
                    </p>
                    <code className="break-all font-mono text-xs text-onSurface-default-secondary">
                      {key.key_prefix}...
                    </code>
                  </div>
                  <Button
                    type="button"
                    variant="ghost"
                    size="icon"
                    aria-label={`Revoke ${key.label}`}
                    onClick={() => setKeyToRevoke(key)}
                    className="size-8 shrink-0"
                  >
                    <Trash2 className="size-4 text-onSurface-danger-primary" />
                  </Button>
                </div>
                <dl className="grid grid-cols-2 gap-3 text-xs">
                  <KeyDate label="Created" value={key.created_at} />
                  <KeyDate label="Last used" value={key.last_used_at} />
                </dl>
              </Card>
            ))}
          </div>
        </>
      )}

      <DeleteConfirmationModal
        isOpen={keyToRevoke !== null}
        onClose={() => setKeyToRevoke(null)}
        onConfirm={handleRevoke}
        title="Revoke client key"
        description="REST and MCP clients using this key will immediately stop working. This cannot be undone."
        itemName={keyToRevoke?.label ?? ""}
        confirmButtonText="Revoke"
      />
    </div>
  );
}

function KeyDate({
  label,
  value,
}: {
  readonly label: string;
  readonly value: string | null;
}) {
  return (
    <div className="min-w-0 space-y-1">
      <dt className="font-semibold text-onSurface-default-secondary">
        {label}
      </dt>
      <dd className="break-words">
        {value === null ? "Never" : formatDate(value)}
      </dd>
    </div>
  );
}

function formatDate(value: string): string {
  const date = new Date(value);
  return Number.isFinite(date.getTime())
    ? format(date, "MMM d, yyyy")
    : "Unknown";
}

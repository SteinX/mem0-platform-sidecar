"use client";

import { useEffect, useId, useState } from "react";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";

interface DeleteConfirmationModalProps {
  readonly isOpen: boolean;
  readonly onClose: () => void;
  readonly onConfirm: () => void;
  readonly title: string;
  readonly description: string;
  readonly itemName: string;
  readonly confirmButtonText?: string;
  readonly isPending?: boolean;
  readonly pendingButtonText?: string;
}

export default function DeleteConfirmationModal({
  isOpen,
  onClose,
  onConfirm,
  title,
  description,
  itemName,
  confirmButtonText = "Delete",
  isPending = false,
  pendingButtonText = "Deleting...",
}: DeleteConfirmationModalProps) {
  const [confirmationText, setConfirmationText] = useState("");
  const confirmationDescriptionId = useId();
  const confirmationInputId = useId();

  useEffect(() => {
    if (!isOpen) {
      setConfirmationText("");
    }
  }, [isOpen]);

  const handleClose = () => {
    if (isPending) {
      return;
    }
    setConfirmationText("");
    onClose();
  };

  const handleConfirm = () => {
    if (!isPending && confirmationText === itemName) {
      onConfirm();
    }
  };

  const isDeleteEnabled = confirmationText === itemName && !isPending;

  return (
    <Dialog
      open={isOpen}
      onOpenChange={(open) => {
        if (!open) {
          handleClose();
        }
      }}
    >
      <DialogContent
        showCloseIcon={!isPending}
        onEscapeKeyDown={(event) => {
          if (isPending) {
            event.preventDefault();
          }
        }}
        onPointerDownOutside={(event) => {
          if (isPending) {
            event.preventDefault();
          }
        }}
      >
        <DialogTitle>{title}</DialogTitle>
        <DialogDescription className="mb-4">{description}</DialogDescription>

        <div className="space-y-4">
          <p
            id={confirmationDescriptionId}
            className="text-sm text-onSurface-default-secondary"
          >
            Please type <span className="break-all font-bold">{itemName}</span>{" "}
            to confirm.
          </p>
          <Label htmlFor={confirmationInputId} className="sr-only">
            Confirmation text
          </Label>
          <Input
            id={confirmationInputId}
            type="text"
            placeholder="Enter name to confirm"
            value={confirmationText}
            onChange={(event) => setConfirmationText(event.target.value)}
            disabled={isPending}
            aria-describedby={confirmationDescriptionId}
            className="w-full"
          />
        </div>

        <div role="status" aria-live="polite" className="sr-only">
          {isPending ? pendingButtonText : ""}
        </div>
        <div className="mt-6 flex justify-end gap-2">
          <Button
            type="button"
            onClick={handleClose}
            variant="outline"
            disabled={isPending}
          >
            Cancel
          </Button>
          <Button
            type="button"
            onClick={handleConfirm}
            variant="destructive"
            disabled={!isDeleteEnabled}
            aria-busy={isPending}
          >
            {isPending ? pendingButtonText : confirmButtonText}
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  );
}

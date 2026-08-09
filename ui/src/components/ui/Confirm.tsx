/** Asking before something cannot be undone.
 *
 * window.confirm cannot say which file, cannot warn that a step is running, and
 * cannot make the dangerous button look dangerous — it is one line of text and
 * two buttons the browser chose. Everything here that destroys something asks
 * with this instead.
 */

import type { ReactNode } from "react";
import { Modal } from "./Modal";
import { Button } from "./primitives";

export function Confirm(
  { open, title, children, confirmLabel = "Confirm", danger, busy, onConfirm, onClose }:
  {
    open: boolean;
    title: ReactNode;
    children?: ReactNode;
    confirmLabel?: string;
    danger?: boolean;
    busy?: boolean;
    onConfirm: () => void;
    onClose: () => void;
  },
) {
  return (
    <Modal
      open={open}
      onClose={onClose}
      title={title}
      footer={
        <>
          <Button onClick={onClose}>Cancel</Button>
          <Button
            variant={danger ? "danger" : "primary"}
            busy={busy}
            onClick={onConfirm}
          >{confirmLabel}</Button>
        </>
      }
    >
      {children && (
        <div className="space-y-2 p-5 text-sm leading-relaxed">{children}</div>
      )}
    </Modal>
  );
}

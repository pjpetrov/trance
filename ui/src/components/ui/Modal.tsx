/** A dialog, using the platform's own.
 *
 * `<dialog>` gives focus trapping, Escape, inert background and the top layer
 * for free — all of which the old hand-rolled modals either lacked or
 * reimplemented badly. The only thing to get right is keeping React's idea of
 * open in step with the element's, which is what the effect below does.
 */

import { useEffect, useRef, type ReactNode } from "react";
import { cn } from "@/lib/cn";
import { Button } from "./primitives";

export function Modal(
  { open, onClose, title, subtitle, children, footer, wide }:
  {
    open: boolean;
    onClose: () => void;
    title: ReactNode;
    subtitle?: ReactNode;
    children: ReactNode;
    footer?: ReactNode;
    /** For the editors — agents, models, loops — which are a list beside a form
     *  and are unusable in a narrow column. */
    wide?: boolean;
  },
) {
  const ref = useRef<HTMLDialogElement>(null);

  useEffect(() => {
    const dialog = ref.current;
    if (!dialog) return;
    if (open && !dialog.open) dialog.showModal();
    if (!open && dialog.open) dialog.close();
  }, [open]);

  return (
    <dialog
      ref={ref}
      // `cancel` is Escape. Without preventing the default the element closes
      // itself and React still thinks it is open, so it cannot be reopened.
      onCancel={(event) => { event.preventDefault(); onClose(); }}
      onClick={(event) => { if (event.target === ref.current) onClose(); }}
      className={cn(
        "m-auto w-[min(94vw,var(--modal-w))] rounded-[--radius-lg] border border-line",
        "bg-panel p-0 text-fg shadow-2xl backdrop:bg-black/60 backdrop:backdrop-blur-sm",
      )}
      style={{ ["--modal-w" as string]: wide ? "1100px" : "640px" }}
    >
      {open && (
        <div className="flex max-h-[86vh] flex-col">
          <header className="flex items-start gap-3 border-b border-line px-5 py-3.5">
            <div className="min-w-0 flex-1">
              <h2 className="text-base font-medium">{title}</h2>
              {subtitle && (
                <p className="mt-0.5 text-xs leading-snug text-muted">{subtitle}</p>
              )}
            </div>
            <Button variant="ghost" size="sm" onClick={onClose} aria-label="Close">✕</Button>
          </header>

          <div className="min-h-0 flex-1 overflow-y-auto">{children}</div>

          {footer && (
            <footer className="flex items-center justify-end gap-2 border-t border-line px-5 py-3">
              {footer}
            </footer>
          )}
        </div>
      )}
    </dialog>
  );
}

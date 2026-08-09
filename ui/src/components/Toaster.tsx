/** Transient messages. Errors stay until dismissed; confirmations fade.
 *
 * An API error used to be an alert() in some places and a silent console.log in
 * others, so half the failures in this UI were invisible. Everything that can
 * fail reports here.
 */

import { useEffect } from "react";
import { create } from "zustand";
import { cn } from "@/lib/cn";

export type ToastTone = "info" | "ok" | "err";

interface Toast {
  id: number;
  tone: ToastTone;
  message: string;
}

interface ToastStore {
  toasts: Toast[];
  push: (message: string, tone?: ToastTone) => void;
  dismiss: (id: number) => void;
}

let nextId = 1;

export const useToasts = create<ToastStore>((set) => ({
  toasts: [],
  push: (message, tone = "info") =>
    set((state) => ({ toasts: [...state.toasts, { id: nextId++, tone, message }] })),
  dismiss: (id) => set((state) => ({ toasts: state.toasts.filter((t) => t.id !== id) })),
}));

/** The one line every call site uses: `toast.err(String(error))`. */
export const toast = {
  info: (message: string) => useToasts.getState().push(message, "info"),
  ok: (message: string) => useToasts.getState().push(message, "ok"),
  err: (message: string) => useToasts.getState().push(message, "err"),
};

export function Toaster() {
  const toasts = useToasts((state) => state.toasts);
  return (
    <div className="pointer-events-none fixed bottom-4 right-4 z-50 flex w-80 flex-col gap-2">
      {toasts.map((item) => <ToastCard key={item.id} toast={item} />)}
    </div>
  );
}

function ToastCard({ toast: item }: { toast: Toast }) {
  const dismiss = useToasts((state) => state.dismiss);

  useEffect(() => {
    // An error is usually the thing you were about to read; it waits for you.
    if (item.tone === "err") return;
    const timer = setTimeout(() => dismiss(item.id), 4000);
    return () => clearTimeout(timer);
  }, [item, dismiss]);

  return (
    <div
      role={item.tone === "err" ? "alert" : "status"}
      onClick={() => dismiss(item.id)}
      className={cn(
        "pointer-events-auto cursor-pointer rounded-[--radius] border px-3 py-2",
        "text-xs leading-snug shadow-lg backdrop-blur",
        item.tone === "err" && "border-err/40 bg-err-soft text-err",
        item.tone === "ok" && "border-ok/40 bg-ok-soft text-ok",
        item.tone === "info" && "border-line bg-panel-2 text-fg",
      )}
    >
      {item.message}
    </div>
  );
}

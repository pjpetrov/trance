/** The small set of things every screen is built from.
 *
 * The old CSS had 921 lines and roughly four button styles that had drifted
 * apart, three panel borders and two definitions of "muted". These are the
 * decisions, made once: one type scale, one radius, one focus ring, and states
 * that exist deliberately rather than per-component.
 */

import { forwardRef, type ButtonHTMLAttributes, type HTMLAttributes,
         type InputHTMLAttributes, type ReactNode, type SelectHTMLAttributes,
         type TextareaHTMLAttributes } from "react";
import { cn } from "@/lib/cn";

// ------------------------------------------------------------------ button

type ButtonVariant = "primary" | "default" | "ghost" | "danger";
type ButtonSize = "sm" | "md";

const BUTTON_VARIANTS: Record<ButtonVariant, string> = {
  primary: "bg-accent text-bg font-medium hover:brightness-110 active:brightness-95",
  default: "bg-panel-2 text-fg border border-line hover:border-muted hover:bg-line/40",
  ghost: "text-muted hover:text-fg hover:bg-panel-2",
  danger: "bg-transparent text-err border border-err/40 hover:bg-err-soft hover:border-err",
};

const BUTTON_SIZES: Record<ButtonSize, string> = {
  sm: "h-7 px-2.5 text-xs gap-1.5",
  md: "h-9 px-3.5 text-sm gap-2",
};

export interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: ButtonVariant;
  size?: ButtonSize;
  /** Shows a spinner and blocks the click, without changing the width — a
   *  button that resizes mid-click moves the one next to it under the cursor. */
  busy?: boolean;
}

export const Button = forwardRef<HTMLButtonElement, ButtonProps>(function Button(
  { className, variant = "default", size = "md", busy, disabled, children, ...rest }, ref,
) {
  return (
    <button
      ref={ref}
      disabled={disabled || busy}
      aria-busy={busy || undefined}
      className={cn(
        "inline-flex items-center justify-center rounded-[--radius] whitespace-nowrap",
        "transition-colors select-none",
        "disabled:opacity-45 disabled:pointer-events-none",
        BUTTON_SIZES[size], BUTTON_VARIANTS[variant], className,
      )}
      {...rest}
    >
      {busy && <Spinner />}
      {children}
    </button>
  );
});

export function Spinner({ className }: { className?: string }) {
  return (
    <span
      role="status"
      aria-label="working"
      className={cn(
        "inline-block size-3 shrink-0 rounded-full border-2 border-current",
        "border-r-transparent animate-spin", className,
      )}
    />
  );
}

// ------------------------------------------------------------------- badge

export type Tone = "neutral" | "accent" | "ok" | "warn" | "err" | "purple" | "cyan";

const TONES: Record<Tone, string> = {
  neutral: "bg-panel-2 text-muted border-line",
  accent: "bg-accent-soft text-accent border-accent/30",
  ok: "bg-ok-soft text-ok border-ok/30",
  warn: "bg-warn-soft text-warn border-warn/30",
  err: "bg-err-soft text-err border-err/30",
  purple: "bg-purple-soft text-purple border-purple/30",
  cyan: "bg-cyan/10 text-cyan border-cyan/30",
};

export function Badge(
  { tone = "neutral", className, children, ...rest }:
  HTMLAttributes<HTMLSpanElement> & { tone?: Tone },
) {
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1 rounded-full border px-2 py-0.5",
        "text-[11px] font-medium leading-none whitespace-nowrap",
        TONES[tone], className,
      )}
      {...rest}
    >
      {children}
    </span>
  );
}

/** A coloured dot. Reads faster than a word in a dense list, and it is what the
 *  session picker and the step rail are scanned for. */
export function Dot({ tone = "neutral", pulse }: { tone?: Tone; pulse?: boolean }) {
  const colour: Record<Tone, string> = {
    neutral: "bg-muted", accent: "bg-accent", ok: "bg-ok", warn: "bg-warn",
    err: "bg-err", purple: "bg-purple", cyan: "bg-cyan",
  };
  return (
    <span className="relative inline-flex size-2 shrink-0">
      {pulse && (
        <span className={cn("absolute inline-flex size-full animate-ping rounded-full opacity-60",
                            colour[tone])} />
      )}
      <span className={cn("relative inline-flex size-2 rounded-full", colour[tone])} />
    </span>
  );
}

// ------------------------------------------------------------------ panel

export function Panel(
  { className, children, ...rest }: HTMLAttributes<HTMLDivElement>,
) {
  return (
    <div
      className={cn("rounded-[--radius-lg] border border-line bg-panel", className)}
      {...rest}
    >
      {children}
    </div>
  );
}

export function PanelHeader(
  { title, subtitle, actions, className }:
  { title: ReactNode; subtitle?: ReactNode; actions?: ReactNode; className?: string },
) {
  return (
    <div className={cn("flex items-center gap-3 border-b border-line px-4 py-2.5", className)}>
      <div className="min-w-0 flex-1">
        <div className="truncate text-sm font-medium">{title}</div>
        {subtitle && <div className="truncate text-xs text-muted">{subtitle}</div>}
      </div>
      {actions && <div className="flex shrink-0 items-center gap-1.5">{actions}</div>}
    </div>
  );
}

// ------------------------------------------------------------------ inputs

const FIELD = cn(
  "w-full rounded-[--radius] border border-line bg-panel-2 px-2.5 py-1.5",
  "text-sm text-fg placeholder:text-muted/70",
  "transition-colors hover:border-muted/60 focus:border-accent focus:outline-none",
  "disabled:opacity-50",
);

export const Input = forwardRef<HTMLInputElement, InputHTMLAttributes<HTMLInputElement>>(
  function Input({ className, ...rest }, ref) {
    return <input ref={ref} className={cn(FIELD, "h-9", className)} {...rest} />;
  });

export const Textarea = forwardRef<HTMLTextAreaElement,
                                   TextareaHTMLAttributes<HTMLTextAreaElement>>(
  function Textarea({ className, ...rest }, ref) {
    return <textarea ref={ref} className={cn(FIELD, "resize-y leading-relaxed", className)}
                     {...rest} />;
  });

export const Select = forwardRef<HTMLSelectElement, SelectHTMLAttributes<HTMLSelectElement>>(
  function Select({ className, children, ...rest }, ref) {
    return (
      <select ref={ref} className={cn(FIELD, "h-9 cursor-pointer", className)} {...rest}>
        {children}
      </select>
    );
  });

export function Field(
  { label, hint, htmlFor, children }:
  { label: ReactNode; hint?: ReactNode; htmlFor?: string; children: ReactNode },
) {
  return (
    <label className="block space-y-1" htmlFor={htmlFor}>
      <span className="block text-xs font-medium text-muted">{label}</span>
      {children}
      {hint && <span className="block text-xs leading-snug text-muted/80">{hint}</span>}
    </label>
  );
}

export function Checkbox(
  { label, hint, className, ...rest }:
  InputHTMLAttributes<HTMLInputElement> & { label: ReactNode; hint?: ReactNode },
) {
  return (
    <label className={cn("flex cursor-pointer items-start gap-2.5 py-1", className)}>
      <input
        type="checkbox"
        className={cn(
          "mt-0.5 size-4 shrink-0 cursor-pointer appearance-none rounded-[--radius-sm]",
          "border border-line bg-panel-2 transition-colors",
          "checked:border-accent checked:bg-accent",
          "checked:after:block checked:after:text-bg checked:after:content-['✓']",
          "checked:after:-mt-px checked:after:text-center checked:after:text-[11px]",
          "checked:after:leading-[14px]",
        )}
        {...rest}
      />
      <span className="min-w-0">
        <span className="block text-sm leading-snug">{label}</span>
        {hint && <span className="block text-xs leading-snug text-muted">{hint}</span>}
      </span>
    </label>
  );
}

// ------------------------------------------------------------------- misc

/** What to show where there is nothing yet. Always says why, never just
 *  "no data": an empty panel that does not explain itself reads as a bug. */
export function Empty(
  { title, hint, action }: { title: ReactNode; hint?: ReactNode; action?: ReactNode },
) {
  return (
    <div className="flex flex-col items-center justify-center gap-2 px-6 py-10 text-center">
      <div className="text-sm text-muted">{title}</div>
      {hint && <div className="max-w-md text-xs leading-relaxed text-muted/75">{hint}</div>}
      {action}
    </div>
  );
}

export function Divider({ className }: { className?: string }) {
  return <div className={cn("h-px w-full bg-line", className)} />;
}

/** Monospace block for anything an agent produced: output, diffs, prompts. */
export function Code(
  { children, className, ...rest }: HTMLAttributes<HTMLPreElement>,
) {
  return (
    <pre
      className={cn(
        "font-code overflow-x-auto rounded-[--radius] bg-bg/60 p-2.5",
        "whitespace-pre-wrap break-words text-fg/90", className,
      )}
      {...rest}
    >
      {children}
    </pre>
  );
}

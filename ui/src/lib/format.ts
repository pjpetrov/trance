/** Small formatters shared across screens. Each one exists because the same
 *  number was being rendered three different ways in the old UI. */

export function tokens(n: number | null | undefined): string {
  const value = Number(n) || 0;
  if (value < 1000) return String(value);
  if (value < 1_000_000) return `${(value / 1000).toFixed(value < 100_000 ? 1 : 0)}k`;
  return `${(value / 1_000_000).toFixed(1)}M`;
}

export function duration(seconds: number | null | undefined): string {
  const total = Math.max(0, Math.round(Number(seconds) || 0));
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  if (h) return `${h}h ${String(m).padStart(2, "0")}m`;
  if (m) return `${m}m ${String(s).padStart(2, "0")}s`;
  return `${s}s`;
}

export function clip(text: unknown, max: number): string {
  const value = String(text ?? "");
  return value.length > max ? `${value.slice(0, max - 1)}…` : value;
}

export function timeOf(ts: string): string {
  const at = new Date(ts);
  return Number.isNaN(at.getTime()) ? "" : at.toLocaleTimeString();
}

/** A model id is often longer than the column it sits in, and its distinctive
 *  part is rarely at the front. Prefer the preset name the user chose. */
export function shortModel(model?: string, preset?: string): string {
  if (preset) return preset;
  const name = String(model ?? "");
  const tail = name.split("/").pop() ?? name;
  return clip(tail.replace(/\.gguf$/i, ""), 28);
}

import { clsx, type ClassValue } from "clsx";
import { twMerge } from "tailwind-merge";

/** Merge class names, with later Tailwind utilities winning over earlier ones.
 *  Without the merge, a component's default `px-3` and a caller's `px-6` both
 *  land in the class list and which one applies is down to stylesheet order. */
export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}

import { clsx, type ClassValue } from "clsx";
import { twMerge } from "tailwind-merge";

/**
 * Tailwind-aware classname composer (shadcn canonical utility).
 */
export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}

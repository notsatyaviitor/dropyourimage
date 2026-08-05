import { clsx, type ClassValue } from 'clsx'
import { twMerge } from 'tailwind-merge'

/** Standard shadcn/ui helper: merge Tailwind classes without utility-order conflicts. */
export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs))
}

export function formatBytes(n: number): string {
  if (n < 1024) return `${n} B`
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`
  return `${(n / (1024 * 1024)).toFixed(1)} MB`
}

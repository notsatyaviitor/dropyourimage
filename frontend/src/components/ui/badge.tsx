import * as React from 'react'
import { cn } from '@/lib/utils'

type Tone = 'neutral' | 'success' | 'warning' | 'danger' | 'info'

const toneClasses: Record<Tone, string> = {
  neutral: 'bg-navy/10 text-navy',
  success: 'bg-accent/15 text-accent',
  warning: 'bg-amber-100 text-amber-800',
  danger: 'bg-red-100 text-red-700',
  info: 'bg-sky-100 text-sky-800',
}

export function Badge({
  tone = 'neutral',
  className,
  ...props
}: React.HTMLAttributes<HTMLSpanElement> & { tone?: Tone }) {
  return (
    <span
      className={cn(
        'inline-flex items-center rounded px-2 py-0.5 text-xs font-medium leading-tight',
        toneClasses[tone],
        className,
      )}
      {...props}
    />
  )
}

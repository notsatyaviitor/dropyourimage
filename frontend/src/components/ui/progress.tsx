import * as ProgressPrimitive from '@radix-ui/react-progress'
import { cn } from '@/lib/utils'

export function Progress({
  value,
  className,
  ...props
}: React.ComponentProps<typeof ProgressPrimitive.Root> & { value: number }) {
  return (
    <ProgressPrimitive.Root
      className={cn('relative h-2 w-full overflow-hidden rounded-full bg-navy/10', className)}
      {...props}
    >
      <ProgressPrimitive.Indicator
        className="h-full bg-accent transition-transform duration-300 ease-out"
        style={{ transform: `translateX(-${100 - Math.min(100, Math.max(0, value))}%)` }}
      />
    </ProgressPrimitive.Root>
  )
}

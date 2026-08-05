import * as CheckboxPrimitive from '@radix-ui/react-checkbox'
import { Check } from './icons'
import { cn } from '@/lib/utils'

export function Checkbox({
  className,
  ...props
}: React.ComponentProps<typeof CheckboxPrimitive.Root>) {
  return (
    <CheckboxPrimitive.Root
      className={cn(
        'peer h-4 w-4 shrink-0 rounded-sm border border-navy/30 bg-white',
        'data-[state=checked]:border-accent data-[state=checked]:bg-accent',
        'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent',
        className,
      )}
      {...props}
    >
      <CheckboxPrimitive.Indicator className="flex items-center justify-center text-white">
        <Check className="h-3 w-3" />
      </CheckboxPrimitive.Indicator>
    </CheckboxPrimitive.Root>
  )
}

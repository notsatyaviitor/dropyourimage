import * as SliderPrimitive from '@radix-ui/react-slider'
import { cn } from '@/lib/utils'

export function Slider({
  className,
  ...props
}: React.ComponentProps<typeof SliderPrimitive.Root>) {
  return (
    <SliderPrimitive.Root
      className={cn('relative flex w-full touch-none select-none items-center', className)}
      {...props}
    >
      <SliderPrimitive.Track className="relative h-1.5 w-full grow overflow-hidden rounded-full bg-navy/10">
        <SliderPrimitive.Range className="absolute h-full bg-accent" />
      </SliderPrimitive.Track>
      <SliderPrimitive.Thumb className="block h-4 w-4 rounded-full border-2 border-accent bg-white shadow transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent" />
    </SliderPrimitive.Root>
  )
}

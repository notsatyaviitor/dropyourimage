import * as TabsPrimitive from '@radix-ui/react-tabs'
import { cn } from '@/lib/utils'

export const Tabs = TabsPrimitive.Root

export function TabsList({ className, ...props }: React.ComponentProps<typeof TabsPrimitive.List>) {
  return (
    <TabsPrimitive.List
      className={cn(
        // nav_typography font-family (Manrope) from the live site, used here for the tab bar
        // since it plays the same role as their top navigation.
        'inline-flex items-center gap-1 rounded-form bg-navy/5 p-1 font-nav',
        className,
      )}
      {...props}
    />
  )
}

export function TabsTrigger({
  className,
  ...props
}: React.ComponentProps<typeof TabsPrimitive.Trigger>) {
  return (
    <TabsPrimitive.Trigger
      className={cn(
        'rounded-button px-4 py-2 text-sm font-medium text-navy/70 transition-colors',
        'hover:text-navy data-[state=active]:bg-navy data-[state=active]:text-white',
        'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent',
        className,
      )}
      {...props}
    />
  )
}

export function TabsContent({
  className,
  ...props
}: React.ComponentProps<typeof TabsPrimitive.Content>) {
  return <TabsPrimitive.Content className={cn('mt-4 focus-visible:outline-none', className)} {...props} />
}

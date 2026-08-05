import * as React from 'react'
import { Slot } from '@radix-ui/react-slot'
import { cn } from '@/lib/utils'

type Variant = 'primary' | 'secondary' | 'ghost' | 'destructive'
type Size = 'default' | 'sm' | 'lg'

const variantClasses: Record<Variant, string> = {
  // Navy gradient + white text, matching the live site's button (--button_gradient_top/bottom
  // #152339 -> hover #2a3951, --button_accent_color #ffffff) rather than an invented style.
  primary: 'bg-navy text-white hover:bg-navy-hover',
  secondary: 'bg-white text-navy border border-navy/20 hover:bg-page-bg',
  ghost: 'bg-transparent text-navy hover:bg-navy/5',
  destructive: 'bg-red-600 text-white hover:bg-red-700',
}

const sizeClasses: Record<Size, string> = {
  // button_padding: 11px 23px on the live site.
  default: 'px-[23px] py-[11px] text-sm',
  sm: 'px-3 py-1.5 text-xs',
  lg: 'px-8 py-3.5 text-base',
}

export interface ButtonProps extends React.ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: Variant
  size?: Size
  asChild?: boolean
}

export const Button = React.forwardRef<HTMLButtonElement, ButtonProps>(
  ({ className, variant = 'primary', size = 'default', asChild = false, ...props }, ref) => {
    const Comp = asChild ? Slot : 'button'
    return (
      <Comp
        ref={ref}
        className={cn(
          // button_typography: Open Sans 600, radius 2px (nearly square) — matches the live site.
          'inline-flex items-center justify-center gap-2 rounded-button font-body font-semibold',
          'transition-colors disabled:pointer-events-none disabled:opacity-50',
          'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent focus-visible:ring-offset-2',
          variantClasses[variant],
          sizeClasses[size],
          className,
        )}
        {...props}
      />
    )
  },
)
Button.displayName = 'Button'

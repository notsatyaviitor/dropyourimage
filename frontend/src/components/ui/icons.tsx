/**
 * Small inline SVG icons. Kept as plain markup rather than adding an icon package for a handful
 * of glyphs — every one here is used in at least two places.
 */
import type { SVGProps } from 'react'

export function Check(props: SVGProps<SVGSVGElement>) {
  return (
    <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth={2.5} {...props}>
      <path d="M3 8.5 6.5 12 13 4.5" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  )
}

export function AlertTriangle(props: SVGProps<SVGSVGElement>) {
  return (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2} {...props}>
      <path
        d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0Z"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
      <line x1="12" y1="9" x2="12" y2="13" strokeLinecap="round" />
      <line x1="12" y1="17" x2="12.01" y2="17" strokeLinecap="round" />
    </svg>
  )
}

export function UploadCloud(props: SVGProps<SVGSVGElement>) {
  return (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.8} {...props}>
      <path
        d="M8 17a5 5 0 0 1-1-9.9A6 6 0 0 1 18.5 8.4 4.5 4.5 0 0 1 17 17"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
      <path d="M12 12v9" strokeLinecap="round" />
      <path d="m9 15 3-3 3 3" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  )
}

export function Download(props: SVGProps<SVGSVGElement>) {
  return (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.8} {...props}>
      <path d="M12 3v12" strokeLinecap="round" />
      <path d="m7 10 5 5 5-5" strokeLinecap="round" strokeLinejoin="round" />
      <path d="M4 20h16" strokeLinecap="round" />
    </svg>
  )
}

export function ChevronDown(props: SVGProps<SVGSVGElement>) {
  return (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2} {...props}>
      <path d="m6 9 6 6 6-6" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  )
}

export function XCircle(props: SVGProps<SVGSVGElement>) {
  return (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.8} {...props}>
      <circle cx="12" cy="12" r="10" />
      <path d="m15 9-6 6" strokeLinecap="round" />
      <path d="m9 9 6 6" strokeLinecap="round" />
    </svg>
  )
}

export function Loader(props: SVGProps<SVGSVGElement>) {
  return (
    <svg viewBox="0 0 24 24" fill="none" className="animate-spin" {...props}>
      <circle cx="12" cy="12" r="10" stroke="currentColor" strokeOpacity={0.2} strokeWidth={3} />
      <path d="M22 12a10 10 0 0 0-10-10" stroke="currentColor" strokeWidth={3} strokeLinecap="round" />
    </svg>
  )
}

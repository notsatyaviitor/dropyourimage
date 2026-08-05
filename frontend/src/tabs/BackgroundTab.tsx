import type { BackgroundSpec } from '@/api/types'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Checkbox } from '@/components/ui/checkbox'
import { Input, Label } from '@/components/ui/input'

const HEX_RE = /^#?([0-9a-fA-F]{3}|[0-9a-fA-F]{6})$/

/**
 * Tab 2 — named "Background Services" to match the live site's own service name.
 *
 * Requirement 2 (exact hex background colour) and the shadow-preservation feature both live
 * here. Everything on this tab is deterministic pixel maths — no model involved.
 */
export function BackgroundTab({
  value,
  onChange,
}: {
  value: BackgroundSpec
  onChange: (next: BackgroundSpec) => void
}) {
  const hexValid = !value.color || HEX_RE.test(value.color)

  return (
    <Card>
      <CardHeader>
        <CardTitle>Background Services</CardTitle>
        <CardDescription>
          Set the delivered background to an exact colour, or leave it transparent. The shadow
          from the original photograph can be preserved onto the new colour.
        </CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        <label className="flex items-center gap-2 text-sm text-navy">
          <Checkbox
            checked={value.transparent}
            onCheckedChange={(checked) => onChange({ ...value, transparent: checked === true })}
          />
          Transparent background (PNG/WebP only — not compatible with JPEG output)
        </label>

        {!value.transparent && (
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="bg-color">Background colour (hex)</Label>
            <div className="flex items-center gap-2">
              <input
                type="color"
                aria-label="Pick a background colour"
                value={hexValid ? normaliseForSwatch(value.color) : '#ffffff'}
                onChange={(e) => onChange({ ...value, color: e.target.value })}
                className="h-9 w-9 cursor-pointer rounded-form border border-navy/20 bg-white p-1"
              />
              <Input
                id="bg-color"
                value={value.color ?? ''}
                onChange={(e) => onChange({ ...value, color: e.target.value })}
                placeholder="#F5F5F5"
                className="max-w-[160px] font-mono"
                aria-invalid={!hexValid}
              />
            </div>
            {!hexValid && (
              <p className="text-xs text-red-600">
                Not a valid hex colour — use the form #RRGGBB or #RGB.
              </p>
            )}
            <p className="text-xs text-body/70">
              Filled with the exact colour requested, compositied in linear light — never an
              approximation from a generative model.
            </p>
          </div>
        )}

        <div className="flex flex-col gap-2 border-t border-navy/10 pt-4">
          <Label>Shadow from the original photograph</Label>
          <div className="flex flex-col gap-2">
            <label className="flex items-center gap-2 text-sm text-navy">
              <input
                type="radio"
                name="shadow-mode"
                checked={value.shadow === 'preserve'}
                onChange={() => onChange({ ...value, shadow: 'preserve' })}
                className="accent-accent"
              />
              Preserve — reconstruct the real shadow onto the new background
            </label>
            <label className="flex items-center gap-2 text-sm text-navy">
              <input
                type="radio"
                name="shadow-mode"
                checked={value.shadow === 'remove'}
                onChange={() => onChange({ ...value, shadow: 'remove' })}
                className="accent-accent"
              />
              Remove — flat background colour, no shadow
            </label>
          </div>
          <p className="text-xs text-body/70">
            Needs a reasonably uniform original background to work. On a busy or lifestyle photo
            this falls back to &ldquo;shadow removed&rdquo; automatically, and the result says so.
          </p>
        </div>

        <label className="flex items-center gap-2 text-sm text-navy">
          <Checkbox
            checked={value.decontaminate_edges}
            onCheckedChange={(checked) => onChange({ ...value, decontaminate_edges: checked === true })}
          />
          Remove background-colour spill from the cut-out edge (recommended)
        </label>
      </CardContent>
    </Card>
  )
}

function normaliseForSwatch(hex: string | null | undefined): string {
  if (!hex) return '#ffffff'
  const h = hex.startsWith('#') ? hex.slice(1) : hex
  if (h.length === 3) return `#${[...h].map((c) => c + c).join('')}`
  if (h.length === 6) return `#${h}`
  return '#ffffff'
}

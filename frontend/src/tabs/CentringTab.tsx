import type { CentringMode, CentringSpec } from '@/api/types'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Checkbox } from '@/components/ui/checkbox'
import { Label } from '@/components/ui/input'

/**
 * Tab 4 — requirement 4 (centre the image). Deterministic geometry on the alpha channel — the
 * client's own report puts this in its "measured, not judged" column.
 */
export function CentringTab({
  value,
  onChange,
}: {
  value: CentringSpec
  onChange: (next: CentringSpec) => void
}) {
  return (
    <Card>
      <CardHeader>
        <CardTitle>Centring</CardTitle>
        <CardDescription>
          Positions the product on the canvas by measuring the alpha channel — never a model
          guessing where the centre &ldquo;looks&rdquo; right.
        </CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        <div className="flex flex-col gap-2">
          <Label>Centring mode</Label>
          <label className="flex items-center gap-2 text-sm text-navy">
            <input
              type="radio"
              name="centring-mode"
              checked={value.mode === 'bbox'}
              onChange={() => onChange({ ...value, mode: 'bbox' as CentringMode })}
              className="accent-accent"
            />
            Bounding box — centres the product&rsquo;s overall extent (the e-commerce default)
          </label>
          <label className="flex items-center gap-2 text-sm text-navy">
            <input
              type="radio"
              name="centring-mode"
              checked={value.mode === 'centroid'}
              onChange={() => onChange({ ...value, mode: 'centroid' as CentringMode })}
              className="accent-accent"
            />
            Centroid — centres the product&rsquo;s centre of mass (better for asymmetric shapes)
          </label>
        </div>

        <label className="flex items-center gap-2 text-sm text-navy">
          <Checkbox
            checked={value.include_shadow_in_bounds}
            onCheckedChange={(checked) =>
              onChange({ ...value, include_shadow_in_bounds: checked === true })
            }
          />
          Include a preserved shadow when measuring the product&rsquo;s bounds
        </label>
        <p className="text-xs text-body/70">
          Off centres the product itself, which is usually what a catalogue wants. On keeps the
          product-plus-shadow group centred as one unit.
        </p>
      </CardContent>
    </Card>
  )
}

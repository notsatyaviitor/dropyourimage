import type { PsdSpec } from '@/api/types'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Checkbox } from '@/components/ui/checkbox'
import { Input, Label } from '@/components/ui/input'

/**
 * Tab 5 — requirement 5 (layered PSD delivery, including a vector clipping path).
 *
 * The only tab whose "enabled" toggle also has to update the export.formats list elsewhere
 * (the contract rejects psd.enabled disagreeing with whether 'psd' is in export.formats) — that
 * coordination lives in App.tsx's handler, not here, so this component only knows about PsdSpec.
 */
export function PsdTab({
  value,
  adobeConfigured,
  onChange,
}: {
  value: PsdSpec
  adobeConfigured: boolean
  onChange: (next: PsdSpec) => void
}) {
  return (
    <Card>
      <CardHeader>
        <CardTitle>PSD</CardTitle>
        <CardDescription>
          A layered Photoshop file — named product, shadow and background layers, plus a real
          vector clipping path traced from the alpha channel.
        </CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        {!adobeConfigured && (
          <div className="rounded-form border border-amber-300 bg-amber-50 px-3 py-2 text-xs text-amber-800">
            Adobe Photoshop API credentials are not configured on this server. PSD requests will
            fall back to layers with a raster mask only — no vector path in the Paths panel — or
            fail, depending on how the backend is currently set up. See docs/PSD.md.
          </div>
        )}

        <label className="flex items-center gap-2 text-sm text-navy">
          <Checkbox
            checked={value.enabled}
            onCheckedChange={(checked) => onChange({ ...value, enabled: checked === true })}
          />
          Include a layered PSD in the output
        </label>

        {value.enabled && (
          <>
            <label className="flex items-center gap-2 text-sm text-navy">
              <Checkbox
                checked={value.vector_clipping_path}
                onCheckedChange={(checked) =>
                  onChange({ ...value, vector_clipping_path: checked === true })
                }
              />
              Trace a vector clipping path into the Paths panel
            </label>

            <div className="grid max-w-md grid-cols-2 gap-3">
              <Field label="Product layer" val={value.product_layer_name} onChange={(v) => onChange({ ...value, product_layer_name: v })} />
              <Field label="Shadow layer" val={value.shadow_layer_name} onChange={(v) => onChange({ ...value, shadow_layer_name: v })} />
              <Field label="Background layer" val={value.background_layer_name} onChange={(v) => onChange({ ...value, background_layer_name: v })} />
              <Field label="Path name" val={value.path_name} onChange={(v) => onChange({ ...value, path_name: v })} />
            </div>

            <p className="text-xs text-body/70">
              Colour profile: Adobe RGB (1998), 8-bit — the client&rsquo;s specified deliverable
              format, embedded automatically.
            </p>
          </>
        )}
      </CardContent>
    </Card>
  )
}

function Field({ label, val, onChange }: { label: string; val: string; onChange: (v: string) => void }) {
  return (
    <div className="flex flex-col gap-1.5">
      <Label>{label}</Label>
      <Input value={val} onChange={(e) => onChange(e.target.value)} maxLength={63} />
    </div>
  )
}

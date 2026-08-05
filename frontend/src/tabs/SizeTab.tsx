import type { ExportSpec, FitMode, OutputFormat, SizeSpec } from '@/api/types'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Checkbox } from '@/components/ui/checkbox'
import { Input, Label } from '@/components/ui/input'
import { Slider } from '@/components/ui/slider'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'

const PRESETS: Array<{ label: string; width: number; height: number }> = [
  { label: '500 × 500 (PDP)', width: 500, height: 500 },
  { label: '1000 × 1000', width: 1000, height: 1000 },
  { label: '2048 × 2048', width: 2048, height: 2048 },
  { label: '1200 × 628 (banner)', width: 1200, height: 628 },
]

const FORMAT_OPTIONS: Array<{ value: OutputFormat; label: string }> = [
  { value: 'png', label: 'PNG' },
  { value: 'jpeg', label: 'JPEG' },
  { value: 'tiff', label: 'TIFF' },
  { value: 'webp', label: 'WebP' },
]

/** Tab 3 — requirement 3 (exact output size). Deterministic geometry; no model involved. */
export function SizeTab({
  size,
  exportSpec,
  transparent,
  onSizeChange,
  onExportChange,
}: {
  size: SizeSpec
  exportSpec: ExportSpec
  transparent: boolean
  onSizeChange: (next: SizeSpec) => void
  onExportChange: (next: ExportSpec) => void
}) {
  const isPreset = (w: number, h: number) => PRESETS.some((p) => p.width === w && p.height === h)

  function toggleFormat(fmt: OutputFormat, checked: boolean) {
    const formats = checked
      ? [...exportSpec.formats, fmt]
      : exportSpec.formats.filter((f) => f !== fmt)
    onExportChange({ ...exportSpec, formats: formats.length ? formats : ['png'] })
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle>Size</CardTitle>
        <CardDescription>
          The output canvas is exactly the pixel size requested — asserted before the image is
          returned, never approximate.
        </CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        <div className="flex flex-col gap-1.5">
          <Label>Preset</Label>
          <div className="flex flex-wrap gap-2">
            {PRESETS.map((p) => (
              <button
                key={p.label}
                type="button"
                onClick={() => onSizeChange({ ...size, width: p.width, height: p.height })}
                className={
                  'rounded-button border px-3 py-1.5 text-xs font-medium transition-colors ' +
                  (isPreset(size.width, size.height) && size.width === p.width && size.height === p.height
                    ? 'border-accent bg-accent/10 text-accent'
                    : 'border-navy/20 text-navy hover:bg-navy/5')
                }
              >
                {p.label}
              </button>
            ))}
          </div>
        </div>

        <div className="grid max-w-xs grid-cols-2 gap-3">
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="width">Width (px)</Label>
            <Input
              id="width"
              type="number"
              min={1}
              max={20000}
              value={size.width}
              onChange={(e) => onSizeChange({ ...size, width: Number(e.target.value) || 1 })}
            />
          </div>
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="height">Height (px)</Label>
            <Input
              id="height"
              type="number"
              min={1}
              max={20000}
              value={size.height}
              onChange={(e) => onSizeChange({ ...size, height: Number(e.target.value) || 1 })}
            />
          </div>
        </div>

        <div className="flex flex-col gap-1.5">
          <Label>Fit mode</Label>
          <Select value={size.fit} onValueChange={(fit: FitMode) => onSizeChange({ ...size, fit })}>
            <SelectTrigger className="max-w-xs">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="contain">Contain — never crops, may leave margin</SelectItem>
              <SelectItem value="pad">Pad — always fills the exact canvas</SelectItem>
              <SelectItem value="cover">Cover — fills the canvas, may crop the product</SelectItem>
            </SelectContent>
          </Select>
        </div>

        <div className="flex flex-col gap-1.5">
          <div className="flex items-center justify-between">
            <Label>Margin</Label>
            <span className="text-xs text-body/70">{size.margin_pct}%</span>
          </div>
          <Slider
            value={[size.margin_pct]}
            min={0}
            max={45}
            step={0.5}
            onValueChange={([v]) => onSizeChange({ ...size, margin_pct: v })}
            className="max-w-xs"
          />
        </div>

        <label className="flex items-center gap-2 text-sm text-navy">
          <Checkbox
            checked={size.allow_upscale}
            onCheckedChange={(checked) => onSizeChange({ ...size, allow_upscale: checked === true })}
          />
          Allow enlarging a smaller source (never stretched — padded, or AI-upscaled if enabled)
        </label>

        <div className="flex flex-col gap-2 border-t border-navy/10 pt-4">
          <Label>Output formats</Label>
          <div className="flex flex-wrap gap-4">
            {FORMAT_OPTIONS.map((opt) => (
              <label key={opt.value} className="flex items-center gap-2 text-sm text-navy">
                <Checkbox
                  checked={exportSpec.formats.includes(opt.value)}
                  disabled={opt.value === 'jpeg' && transparent}
                  onCheckedChange={(checked) => toggleFormat(opt.value, checked === true)}
                />
                {opt.label}
                {opt.value === 'jpeg' && transparent && (
                  <span className="text-xs text-body/50">(disabled — background is transparent)</span>
                )}
              </label>
            ))}
          </div>
        </div>
      </CardContent>
    </Card>
  )
}

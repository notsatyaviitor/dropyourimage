import type { CutoutSpec, EngineId, EngineStrategy } from '@/api/types'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Checkbox } from '@/components/ui/checkbox'
import { Label } from '@/components/ui/input'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'

/**
 * Tab 1 — named "Clipping" to match the live site's own service name for this capability
 * (see frontend/CLAUDE.md — reuse their vocabulary rather than inventing our own).
 *
 * The only tab whose choice is genuinely an AI decision: which segmentation engine(s) remove the
 * background. Everything on every other tab is deterministic code.
 */
export function ClippingTab({
  value,
  onChange,
}: {
  value: CutoutSpec
  onChange: (next: CutoutSpec) => void
}) {
  return (
    <Card>
      <CardHeader>
        <CardTitle>Clipping</CardTitle>
        <CardDescription>
          Removes the background. This is the one step in the whole pipeline that calls an AI
          service — every other tab is deterministic code, not a model.
        </CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        <div className="flex flex-col gap-1.5">
          <Label>Engine strategy</Label>
          <Select
            value={value.strategy}
            onValueChange={(strategy: EngineStrategy) =>
              onChange({ ...value, strategy, engine: strategy === 'single' ? value.engine ?? 'photoroom' : null })
            }
          >
            <SelectTrigger className="max-w-xs">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="auto">Auto (compare two engines, keep the better cut-out)</SelectItem>
              <SelectItem value="single">Single engine</SelectItem>
            </SelectContent>
          </Select>
          <p className="text-xs text-body/70">
            Auto runs two segmentation engines and picks the better mask automatically — the same
            dual-engine routing the client&rsquo;s AI Enablement Report recommends for this stage.
          </p>
        </div>

        {value.strategy === 'single' && (
          <div className="flex flex-col gap-1.5">
            <Label>Engine</Label>
            <Select
              value={value.engine ?? 'photoroom'}
              onValueChange={(engine: EngineId) => onChange({ ...value, engine })}
            >
              <SelectTrigger className="max-w-xs">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="photoroom">Photoroom (~$0.02/image)</SelectItem>
                <SelectItem value="removebg">remove.bg (~$0.20/image)</SelectItem>
                <SelectItem value="falai">fal.ai (~$0.03&ndash;0.05/image)</SelectItem>
                <SelectItem value="local">Local (offline test engine, no cost, demo quality only)</SelectItem>
              </SelectContent>
            </Select>
          </div>
        )}

        <label className="flex items-center gap-2 text-sm text-navy">
          <Checkbox
            checked={value.keep_losing_candidate}
            onCheckedChange={(checked) => onChange({ ...value, keep_losing_candidate: checked === true })}
          />
          Keep the losing candidate for side-by-side review
        </label>
      </CardContent>
    </Card>
  )
}

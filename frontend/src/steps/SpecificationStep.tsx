/**
 * Step 1 — the specification card.
 *
 * The design prototype showed three services (background removal, output size, auto-centre). The
 * pipeline has more than three knobs, and the ones the prototype omitted are not cosmetic: without
 * the subject prompt, a photograph containing several objects silently produces a cut-out of the
 * wrong one. So every `JobConfig` field is reachable here, laid out in the prototype's own
 * service-row pattern.
 *
 * Fields the *backend* does not implement are absent rather than shown-and-ignored. The one
 * exception is PSD, which is real but needs Adobe credentials — it says so instead of pretending.
 */

import type {
  BackgroundSpec,
  CentringSpec,
  CutoutSpec,
  EngineId,
  ExportSpec,
  JobConfig,
  OutputFormat,
  PsdSpec,
  SizeSpec,
} from '@/api/types'

const HEX_RE = /^#?([0-9a-fA-F]{3}|[0-9a-fA-F]{6})$/

const SIZE_PRESETS: { label: string; w: number; h: number }[] = [
  { label: '500 × 500 — PDP', w: 500, h: 500 },
  { label: '1000 × 1000', w: 1000, h: 1000 },
  { label: '2048 × 2048', w: 2048, h: 2048 },
  { label: '1200 × 628 — banner', w: 1200, h: 628 },
]

// BMP and EPS join the writable set so a BMP or EPS source can be handed back as itself.
// Neither carries alpha, so both are disabled while the background is transparent — same rule
// JPEG has always had, and the backend rejects the combination outright.
const RASTER_FORMATS: OutputFormat[] = ['png', 'jpeg', 'tiff', 'webp', 'bmp', 'eps']
const NO_ALPHA_FORMATS: OutputFormat[] = ['jpeg', 'bmp', 'eps']

export function SpecificationStep({
  config,
  adobeConfigured,
  onChange,
  onNext,
}: {
  config: JobConfig
  adobeConfigured: boolean
  onChange: (next: JobConfig) => void
  onNext: () => void
}) {
  const { cutout, background, size, centring, export: exportSpec, psd } = config

  const setCutout = (v: Partial<CutoutSpec>) => onChange({ ...config, cutout: { ...cutout, ...v } })
  const setSize = (v: Partial<SizeSpec>) => onChange({ ...config, size: { ...size, ...v } })
  const setCentring = (v: Partial<CentringSpec>) =>
    onChange({ ...config, centring: { ...centring, ...v } })
  const setExport = (v: Partial<ExportSpec>) =>
    onChange({ ...config, export: { ...exportSpec, ...v } })

  /**
   * transparent + jpeg is rejected by the contract, so drop it rather than let a 422 happen.
   * BMP and EPS carry no alpha either and the encoder raises on them, so they go the same way —
   * the backend guard covers all three, and this keeps the UI from ever posting the bad combo.
   */
  const setBackground = (v: Partial<BackgroundSpec>) => {
    const next: BackgroundSpec = { ...background, ...v }
    const formats = next.transparent
      ? exportSpec.formats.filter((f) => !NO_ALPHA_FORMATS.includes(f))
      : exportSpec.formats
    onChange({ ...config, background: next, export: { ...exportSpec, formats } })
  }

  /** psd.enabled must agree with 'psd' in export.formats — same reasoning. */
  const setPsd = (v: Partial<PsdSpec>) => {
    const next: PsdSpec = { ...psd, ...v }
    const has = exportSpec.formats.includes('psd')
    const formats = next.enabled
      ? has
        ? exportSpec.formats
        : [...exportSpec.formats, 'psd' as OutputFormat]
      : exportSpec.formats.filter((f) => f !== 'psd')
    onChange({ ...config, psd: next, export: { ...exportSpec, formats } })
  }

  const toggleFormat = (fmt: OutputFormat) => {
    const has = exportSpec.formats.includes(fmt)
    if (has && exportSpec.formats.length === 1) return // at least one format is required
    setExport({
      formats: has ? exportSpec.formats.filter((f) => f !== fmt) : [...exportSpec.formats, fmt],
    })
  }

  const hexValid = background.transparent || HEX_RE.test(background.color ?? '')

  return (
    <div className="wizard-page active">
      <div className="page-title-row">
        <div className="ptrow-left">
          <h1 className="page-title">Select a specification</h1>
        </div>
        <button type="button" className="btn-next" onClick={onNext} disabled={!hexValid}>
          Next
        </button>
      </div>

      <p className="section-label">Personal specifications</p>

      <div className="spec-cards-wrap">
        <div className="spec-card">
          <div className="spec-card-head">
            <div>
              <div className="spec-card-name">POC Configuration</div>
              <div className="spec-card-desc">
                clipping · background {background.transparent ? 'transparent' : background.color} ·{' '}
                {size.width}×{size.height} · {centring.mode === 'bbox' ? 'bounding box' : 'centroid'}
              </div>
            </div>
            <div className="spec-checkbox">
              <svg viewBox="0 0 14 14" fill="none" stroke="white" strokeWidth="2.2" strokeLinecap="round">
                <polyline points="2,7 6,11 12,3" />
              </svg>
            </div>
          </div>

          <div className="service-rows">
            {/* ── Clipping ───────────────────────────────────────── */}
            <ServiceRow
              icon={<ClippingIcon />}
              iconClass="srow-icon-bg"
              name="Clipping"
              detail="Remove the original background. The only step in this pipeline that calls an AI service — everything below is deterministic code."
              alwaysOn
            >
              <label className="opt-check">
                <input
                  type="checkbox"
                  checked={cutout.multi_object}
                  onChange={(e) => setCutout({ multi_object: e.target.checked })}
                />
                Layer every object separately (scenes, not packshots)
              </label>
              <p className="opt-help">
                For a room or lifestyle shot: a model lists the objects and each is cut out into
                its own named PSD layer with its own saved path — bed, pillows, plant, and so on.
                <strong> Costs one segmentation call per object</strong>, so a nine-object room is
                nine times the price of a single cut-out. Centring, shadow reconstruction and
                background replacement are all skipped, because none of them mean anything for a
                scene. Leave off for a single product.
              </p>

              <p className="opt-sublabel">Engine strategy</p>
              <select
                className="opt-input opt-select"
                value={cutout.strategy}
                onChange={(e) =>
                  setCutout({
                    strategy: e.target.value as CutoutSpec['strategy'],
                    engine: e.target.value === 'single' ? (cutout.engine ?? 'photoroom') : null,
                  })
                }
              >
                <option value="auto">Auto — compare two engines, keep the better cut-out</option>
                <option value="single">Single engine</option>
              </select>

              {cutout.strategy === 'single' && (
                <>
                  <p className="opt-sublabel">Engine</p>
                  <select
                    className="opt-input opt-select"
                    value={cutout.engine ?? 'photoroom'}
                    onChange={(e) => setCutout({ engine: e.target.value as EngineId })}
                  >
                    {/* remove.bg is deliberately absent: retired on accuracy 7 Aug 2026. The
                        backend adapter still exists, so an old saved config naming it keeps
                        working — it is just not offered as a new choice. */}
                    <option value="photoroom">Photoroom — soft edge preserved</option>
                    <option value="gemini">Gemini — hard edge, no soft alpha</option>
                    <option value="falai">fal.ai</option>
                    <option value="local">Local (offline control engine — demo quality only)</option>
                  </select>

                  {/* Stated at the point of choosing, not only after the job runs. The same fact
                      comes back per-image as the HARD_EDGED_MASK note. */}
                  {cutout.engine === 'gemini' && (
                    <p className="opt-note opt-note-warn">
                      Gemini returns the mask as a polygon, so the cut-out edge is hard — measured
                      at 0 soft edge pixels against 248 for the same reference image. Edge
                      decontamination has nothing to correct, so expect a visible halo against
                      saturated background colours. Choose Photoroom for soft-edged subjects.
                    </p>
                  )}
                </>
              )}

              <p className="opt-sublabel">
                Subject <span className="opt-optional">optional</span>
              </p>
              <input
                type="text"
                className="opt-input"
                maxLength={120}
                placeholder="e.g. coffee table"
                value={cutout.subject_prompt ?? ''}
                onChange={(e) => setCutout({ subject_prompt: e.target.value.trim() ? e.target.value : null })}
              />
              <p className="opt-help">
                Leave empty for a packshot. Name the object when the photograph contains several —
                background removal separates foreground from background and cannot tell <em>which</em>{' '}
                foreground you meant, so without this it may return the sofa when you wanted the table.
              </p>

              {cutout.subject_prompt && (
                <>
                  <p className="opt-sublabel">
                    Context around the subject — {cutout.subject_padding_pct}%
                  </p>
                  <input
                    type="range"
                    className="opt-range"
                    min={0}
                    max={50}
                    step={1}
                    value={cutout.subject_padding_pct}
                    onChange={(e) => setCutout({ subject_padding_pct: Number(e.target.value) })}
                  />
                  <p className="opt-help">
                    Raise this if the cut-out clips the subject; lower it if neighbouring objects
                    creep in.
                  </p>
                </>
              )}

              <label className="opt-check">
                <input
                  type="checkbox"
                  checked={cutout.keep_losing_candidate}
                  onChange={(e) => setCutout({ keep_losing_candidate: e.target.checked })}
                />
                Keep the losing candidate for side-by-side review
              </label>
            </ServiceRow>

            {/* ── Background ─────────────────────────────────────── */}
            <ServiceRow
              icon={<BackgroundIcon />}
              iconClass="srow-icon-bg"
              name="Background Services"
              detail="Replace the removed background with a solid colour, or leave it transparent."
              enabled={!background.transparent}
              onToggle={(on) => setBackground({ transparent: !on })}
              toggleTitle="Off = transparent output (PNG alpha preserved)"
            >
              {background.transparent ? (
                <p className="opt-help">
                  Transparent output. Alpha is preserved end to end and JPEG is unavailable, since it
                  cannot carry transparency.
                </p>
              ) : (
                <>
                  <p className="opt-sublabel">Replacement colour</p>
                  <div className="color-input-row">
                    <div className="color-swatch" style={{ background: background.color ?? '#FFFFFF' }}>
                      <input
                        type="color"
                        className="opt-color-picker"
                        value={normaliseHex(background.color)}
                        onChange={(e) => setBackground({ color: e.target.value.toUpperCase() })}
                      />
                    </div>
                    <input
                      type="text"
                      className="opt-input hex-input"
                      maxLength={7}
                      placeholder="#RRGGBB"
                      value={background.color ?? ''}
                      onChange={(e) => setBackground({ color: e.target.value })}
                    />
                  </div>
                  {!hexValid && <p className="opt-error">Enter a hex colour like #F5F5F5 or #FFF.</p>}
                  <p className="opt-help">
                    Delivered byte-exact. The value is treated as sRGB and converted, not copied, when
                    the output profile is Adobe RGB.
                  </p>
                </>
              )}

              <p className="opt-sublabel">Original shadow</p>
              <div className="opt-radio-row">
                <label className="opt-radio">
                  <input
                    type="radio"
                    name="shadow"
                    checked={background.shadow === 'preserve'}
                    onChange={() => setBackground({ shadow: 'preserve' })}
                  />
                  Preserve
                </label>
                <label className="opt-radio">
                  <input
                    type="radio"
                    name="shadow"
                    checked={background.shadow === 'remove'}
                    onChange={() => setBackground({ shadow: 'remove' })}
                  />
                  Remove
                </label>
              </div>
              <p className="opt-help">
                Preserving reconstructs the photographed shadow onto the new colour. It needs a
                uniform original backdrop, so on a lifestyle scene it is skipped and says so.
              </p>

              <label className="opt-check">
                <input
                  type="checkbox"
                  checked={background.decontaminate_edges}
                  onChange={(e) => setBackground({ decontaminate_edges: e.target.checked })}
                />
                Decontaminate edges — removes the halo left by the original backdrop
              </label>
            </ServiceRow>

            {/* ── Output size ────────────────────────────────────── */}
            <ServiceRow
              icon={<SizeIcon />}
              iconClass="srow-icon-size"
              name="Output Size"
              detail="Resize the output canvas to exact pixel dimensions for marketplace compliance."
              alwaysOn
            >
              <div className="preset-row">
                {SIZE_PRESETS.map((p) => (
                  <button
                    key={p.label}
                    type="button"
                    className={`preset-btn${size.width === p.w && size.height === p.h ? ' active' : ''}`}
                    onClick={() => setSize({ width: p.w, height: p.h })}
                  >
                    {p.label}
                  </button>
                ))}
              </div>

              <p className="opt-sublabel">Width × Height</p>
              <div className="size-input-row">
                <input
                  type="number"
                  className="opt-input size-num"
                  min={1}
                  max={20000}
                  value={size.width}
                  onChange={(e) => setSize({ width: clampDim(e.target.value) })}
                />
                <span className="size-sep">×</span>
                <input
                  type="number"
                  className="opt-input size-num"
                  min={1}
                  max={20000}
                  value={size.height}
                  onChange={(e) => setSize({ height: clampDim(e.target.value) })}
                />
                <span className="size-unit">px</span>
              </div>
              <p className="opt-help">Asserted before delivery — 500 × 500 means exactly 500 × 500.</p>

              <p className="opt-sublabel">Fit</p>
              <select
                className="opt-input opt-select"
                value={size.fit}
                onChange={(e) => setSize({ fit: e.target.value as SizeSpec['fit'] })}
              >
                <option value="contain">Contain — never crops, may leave margin</option>
                <option value="cover">Cover — fills the canvas, may crop the product</option>
              </select>

              <p className="opt-sublabel">
                Margin — {size.margin_pct}% per side
                {size.fit === 'cover' && <span className="opt-optional">ignored by Cover</span>}
              </p>
              <input
                type="range"
                className="opt-range"
                min={0}
                max={45}
                step={0.5}
                value={size.margin_pct}
                disabled={size.fit === 'cover'}
                onChange={(e) => setSize({ margin_pct: Number(e.target.value) })}
              />

              <label className="opt-check">
                <input
                  type="checkbox"
                  checked={size.allow_upscale}
                  onChange={(e) => setSize({ allow_upscale: e.target.checked })}
                />
                Allow enlarging a source smaller than the canvas
              </label>
              <p className="opt-help">
                Off by default: an undersized master is padded rather than stretched, and the result
                says which happened.
              </p>

              <label className="opt-check">
                <input
                  type="checkbox"
                  checked={exportSpec.match_source}
                  onChange={(e) =>
                    onChange({
                      ...config,
                      export: { ...exportSpec, match_source: e.target.checked },
                    })
                  }
                />
                Also return each image in the format it was uploaded as
              </label>
              <p className="opt-help">
                A PNG in gives a PNG out, a TIFF a TIFF, and so on — combined with anything ticked
                below. Camera raw (CR2, CR3, NEF, CRW, DNG, RAW) is the exception: it decodes but
                cannot be written back, so those deliver 16-bit TIFF and say so on the result.
              </p>

              <p className="opt-sublabel">Output formats</p>
              <div className="fmt-row">
                {RASTER_FORMATS.map((f) => {
                  const disabled = NO_ALPHA_FORMATS.includes(f) && background.transparent
                  return (
                    <label key={f} className={`fmt-chip${disabled ? ' disabled' : ''}`}>
                      <input
                        type="checkbox"
                        checked={exportSpec.formats.includes(f)}
                        disabled={disabled}
                        onChange={() => toggleFormat(f)}
                      />
                      {f.toUpperCase()}
                    </label>
                  )
                })}
              </div>
              {background.transparent && (
                <p className="opt-help">
                  JPEG, BMP and EPS are unavailable while the background is transparent — none of
                  them can carry an alpha channel.
                </p>
              )}

              <p className="opt-sublabel">Colour profile</p>
              <select
                className="opt-input opt-select"
                value={exportSpec.profile}
                onChange={(e) => setExport({ profile: e.target.value as ExportSpec['profile'] })}
              >
                <option value="srgb">sRGB</option>
                <option value="adobe_rgb">Adobe RGB (1998)</option>
              </select>
            </ServiceRow>

            {/* ── Centring ───────────────────────────────────────── */}
            <ServiceRow
              icon={<CentreIcon />}
              iconClass="srow-icon-centre"
              name="Auto-Centre"
              detail="Centre the subject within the output canvas. Deterministic measurement from the alpha channel — no model involved."
              alwaysOn
            >
              <div className="centre-preview">
                <div className="centre-preview-frame">
                  <div className="centre-preview-guide centre-guide-h" />
                  <div className="centre-preview-guide centre-guide-v" />
                  <div className="centre-preview-subject" />
                </div>
                <span className="centre-preview-label">
                  Subject centred on {size.width} × {size.height} canvas
                </span>
              </div>

              <p className="opt-sublabel">Centre on</p>
              <div className="opt-radio-row">
                <label className="opt-radio">
                  <input
                    type="radio"
                    name="centring-mode"
                    checked={centring.mode === 'bbox'}
                    onChange={() => setCentring({ mode: 'bbox' })}
                  />
                  Bounding box
                </label>
                <label className="opt-radio">
                  <input
                    type="radio"
                    name="centring-mode"
                    checked={centring.mode === 'centroid'}
                    onChange={() => setCentring({ mode: 'centroid' })}
                  />
                  Centre of mass
                </label>
              </div>
              <p className="opt-help">
                These disagree for an asymmetric product — a mug with a handle looks centred by
                bounding box but sits off-centre by mass. The residual offset is measured and reported
                either way.
              </p>

              <label className="opt-check">
                <input
                  type="checkbox"
                  checked={centring.include_shadow_in_bounds}
                  onChange={(e) => setCentring({ include_shadow_in_bounds: e.target.checked })}
                />
                Count a preserved shadow toward the subject bounds
              </label>

              <p className="opt-sublabel">Alpha threshold — {centring.alpha_threshold.toFixed(2)}</p>
              <input
                type="range"
                className="opt-range"
                min={0}
                max={0.5}
                step={0.01}
                value={centring.alpha_threshold}
                onChange={(e) => setCentring({ alpha_threshold: Number(e.target.value) })}
              />
              <p className="opt-help">
                Which pixels count when measuring the subject&rsquo;s bounds. Measurement only — the
                stored alpha is never thresholded.
              </p>
            </ServiceRow>

            {/* ── PSD ────────────────────────────────────────────── */}
            <ServiceRow
              icon={<PsdIcon />}
              iconClass="srow-icon-size"
              name="Layered PSD"
              detail="Deliver a layered PSD with a vector clipping path, alongside the raster outputs."
              enabled={psd.enabled}
              onToggle={(on) => setPsd({ enabled: on })}
            >
              {!adobeConfigured && (
                <div className="method-poc-banner">
                  Adobe Photoshop API credentials are not configured on this server, so the PSD is
                  written by the pure-Python fallback. Layers and the clipping path are real; the
                  Adobe path is untested. See docs/PSD.md.
                </div>
              )}
              <label className="opt-check">
                <input
                  type="checkbox"
                  checked={psd.vector_clipping_path}
                  onChange={(e) => setPsd({ vector_clipping_path: e.target.checked })}
                />
                Include a vector clipping path
              </label>
              <div className="psd-names">
                {(
                  [
                    ['product_layer_name', 'Product layer'],
                    ['shadow_layer_name', 'Shadow layer'],
                    ['background_layer_name', 'Background layer'],
                    ['path_name', 'Path name'],
                  ] as const
                ).map(([key, label]) => (
                  <div key={key} className="psd-name-field">
                    <label>{label}</label>
                    <input
                      type="text"
                      className="opt-input"
                      maxLength={63}
                      value={psd[key]}
                      onChange={(e) => setPsd({ [key]: e.target.value } as Partial<PsdSpec>)}
                    />
                  </div>
                ))}
              </div>
            </ServiceRow>
          </div>
        </div>
      </div>
    </div>
  )
}

/** One service row in the prototype's layout. `alwaysOn` renders a locked toggle, as it did there. */
function ServiceRow({
  icon,
  iconClass,
  name,
  detail,
  children,
  enabled,
  alwaysOn,
  onToggle,
  toggleTitle,
}: {
  icon: React.ReactNode
  iconClass: string
  name: string
  detail: string
  children: React.ReactNode
  enabled?: boolean
  alwaysOn?: boolean
  onToggle?: (on: boolean) => void
  toggleTitle?: string
}) {
  const open = alwaysOn || enabled
  return (
    <div className="service-row">
      <div className="service-row-left">
        <div className="srow-header">
          <div className={`srow-icon ${iconClass}`}>{icon}</div>
          <div>
            <div className="service-row-name">{name}</div>
            <div className="service-row-detail">{detail}</div>
          </div>
        </div>
        <div className={`service-options${open ? ' open' : ''}`}>{children}</div>
      </div>
      <div className="toggle-wrap">
        <label className="toggle-switch" title={alwaysOn ? 'Always applied' : toggleTitle}>
          <input
            type="checkbox"
            checked={open}
            disabled={alwaysOn}
            onChange={(e) => onToggle?.(e.target.checked)}
          />
          <span className="toggle-track" />
        </label>
      </div>
    </div>
  )
}

function clampDim(raw: string): number {
  const n = parseInt(raw, 10)
  if (Number.isNaN(n)) return 1
  return Math.min(20000, Math.max(1, n))
}

/** `<input type="color">` only accepts #RRGGBB, so shorthand and blanks need expanding. */
function normaliseHex(v: string | null | undefined): string {
  if (!v) return '#FFFFFF'
  const h = v.replace('#', '')
  if (h.length === 3) return `#${h.split('').map((c) => c + c).join('')}`
  if (h.length === 6) return `#${h}`
  return '#FFFFFF'
}

function ClippingIcon() {
  return (
    <svg viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
      <circle cx="4.5" cy="7" r="2" />
      <circle cx="4.5" cy="13" r="2" />
      <path d="M6.3 8L16 3.5M6.3 12L16 16.5M10 10h6" />
    </svg>
  )
}

function BackgroundIcon() {
  return (
    <svg viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
      <rect x="2.5" y="2.5" width="15" height="15" rx="2" />
      <path d="M2.5 13l4-4 3.5 3.5 3-3 4.5 4.5" />
      <circle cx="7" cy="7" r="1.4" />
    </svg>
  )
}

function SizeIcon() {
  return (
    <svg viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round">
      <rect x="2" y="2" width="16" height="16" rx="2" />
      <path d="M2 7h16M7 2v16" />
    </svg>
  )
}

function CentreIcon() {
  return (
    <svg viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round">
      <circle cx="10" cy="10" r="3" />
      <line x1="10" y1="2" x2="10" y2="6.5" />
      <line x1="10" y1="13.5" x2="10" y2="18" />
      <line x1="2" y1="10" x2="6.5" y2="10" />
      <line x1="13.5" y1="10" x2="18" y2="10" />
    </svg>
  )
}

function PsdIcon() {
  return (
    <svg viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round">
      <path d="M10 2.5L17 6l-7 3.5L3 6z" />
      <path d="M3 10l7 3.5 7-3.5" />
      <path d="M3 14l7 3.5 7-3.5" />
    </svg>
  )
}

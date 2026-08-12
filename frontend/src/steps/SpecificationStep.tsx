/**
 * Step 1 — the specification card.
 *
 * The design prototype showed three services (background removal, output size, auto-centre). The
 * pipeline has more than three knobs, and the ones the prototype omitted are not cosmetic: without
 * the subject prompt, a photograph containing several objects silently produces a cut-out of the
 * wrong one. So every `JobConfig` field is reachable here, laid out in the prototype's own
 * service-row pattern.
 *
 * Fields the *backend* does not implement are absent rather than shown-and-ignored.
 *
 * **Layered PSD is currently hidden** behind `SHOW_PSD`, on client request. It is implemented and
 * working — the flag hides the control, not the capability. See the comment on that constant.
 *
 * ## Reachable, but not all at once
 *
 * Every field being reachable turned into every field being *shouted*: five cards, seventeen
 * controls, and twelve paragraphs of rationale, all competing at the same visual weight. The page
 * read as documentation rather than a form, and the three settings an operator actually changes —
 * colour, size, format — were buried among the ones they never touch.
 *
 * Two mechanisms fix that without deleting anything:
 *
 * * **`<Advanced>`** — a collapsed `<details>` per card holding the controls that have a correct
 *   default and are rarely moved (fit, margin, alpha threshold, layer names).
 * * **`explain`** — one page-level switch, off by default, that reveals the rationale prose.
 *
 * **What is never folded away**: anything that is a *consequence* rather than an explanation —
 * the Gemini hard-edge warning, the per-object cost of multi-object, the hex validation error, the
 * Adobe-fallback banner, and which formats transparency has disabled. Those are the same class of
 * thing as a `Note` on a result, and `frontend/CLAUDE.md` is explicit that they must not be
 * swallowed. A default being hidden is fine; a consequence being hidden is not.
 */

import { useState } from 'react'

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

/**
 * Layered PSD is hidden from the specification for now, on client request.
 *
 * A flag rather than a deletion: the feature works — `app/psd/` writes a real layered PSD with a
 * vector clipping path through the pytoshop fallback, and it is covered by tests. Only the control
 * is hidden, so turning this back on restores it with no other change.
 *
 * Hiding the card leaves `psd.enabled` false, which is its default, so nothing can request a PSD
 * from here while it is off. A PSD *upload* still comes back as a PSD via `export.match_source` —
 * that is a different question ("give me my format back") and is unaffected.
 */
const SHOW_PSD = false

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

  /**
   * Un-ticking the last format is allowed **when "return each image in its own format" is on**.
   *
   * That combination is how you ask for "give me back exactly what I uploaded, and nothing else".
   * Previously at least one format was always required, so every image picked up an extra file
   * nobody asked for — a PNG beside every PSD, turning a 132-image order into 264 files. With
   * match_source off, one format is still mandatory or the job would deliver nothing.
   */
  const toggleFormat = (fmt: OutputFormat) => {
    const has = exportSpec.formats.includes(fmt)
    if (has && exportSpec.formats.length === 1 && !exportSpec.match_source) return
    setExport({
      formats: has ? exportSpec.formats.filter((f) => f !== fmt) : [...exportSpec.formats, fmt],
    })
  }

  /** Turning match_source off with no formats ticked would deliver nothing; restore a default. */
  const setMatchSource = (on: boolean) =>
    onChange({
      ...config,
      export: {
        ...exportSpec,
        match_source: on,
        formats: !on && exportSpec.formats.length === 0 ? ['png'] : exportSpec.formats,
      },
    })

  /** Rationale prose, off by default. See the module docstring for what is NOT gated by this. */
  const [explain, setExplain] = useState(false)

  const hexValid = background.transparent || HEX_RE.test(background.color ?? '')

  return (
    <div className="wizard-page active">
      <div className="page-title-row">
        <div className="ptrow-left">
          <h1 className="page-title">Select a specification</h1>
        </div>
        <div className="ptrow-right">
          <label className="explain-switch" title="Show why each setting exists">
            <input
              type="checkbox"
              checked={explain}
              onChange={(e) => setExplain(e.target.checked)}
            />
            Explain settings
          </label>
          <button type="button" className="btn-next" onClick={onNext} disabled={!hexValid}>
            Next
          </button>
        </div>
      </div>

      <p className="section-label">Personal specifications</p>

      <div className="spec-cards-wrap">
        <div className="spec-card">
          <div className="spec-card-head">
            <div>
              <div className="spec-card-name">POC Configuration</div>
              <div className="spec-card-desc">
                background removal · {background.transparent ? 'transparent' : background.color} ·{' '}
                {size.match_source ? 'original size' : `${size.width}×${size.height}`} ·{' '}
                {centring.mode === 'bbox' ? 'bounding box' : 'centroid'}
              </div>
            </div>
            <div className="spec-checkbox">
              <svg viewBox="0 0 14 14" fill="none" stroke="white" strokeWidth="2.2" strokeLinecap="round">
                <polyline points="2,7 6,11 12,3" />
              </svg>
            </div>
          </div>

          <div className="service-rows">
            {/* ── Background Removal ─────────────────────────────── */}
            <ServiceRow
              icon={<BackgroundRemovalIcon />}
              iconClass="srow-icon-bg"
              name="Background Removal"
              detail="Remove the original background. The only step in this pipeline that calls an AI service — everything below is deterministic code."
              alwaysOn
            >
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
              {explain && (
                <p className="opt-help">
                  Leave empty for a packshot. Name the object when the photograph contains several —
                  background removal separates foreground from background and cannot tell{' '}
                  <em>which</em> foreground you meant, so without this it may return the sofa when
                  you wanted the table.
                </p>
              )}

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
                  {explain && (
                    <p className="opt-help">
                      Raise this if the cut-out clips the subject; lower it if neighbouring objects
                      creep in.
                    </p>
                  )}
                </>
              )}

              {/* A cost consequence, not an explanation — shown whenever the mode is ON, even
                  though the control itself lives under Advanced. Nine objects is nine calls. */}
              {cutout.multi_object && (
                <p className="opt-note opt-note-warn">
                  Layer-per-object is on: this bills <strong>one segmentation call per object</strong>,
                  so a nine-object room costs nine times a single cut-out. Centring, shadow
                  reconstruction and background replacement are all skipped.
                </p>
              )}

              <Advanced>
              <label className="opt-check">
                <input
                  type="checkbox"
                  checked={cutout.multi_object}
                  onChange={(e) => setCutout({ multi_object: e.target.checked })}
                />
                Layer every object separately (scenes, not packshots)
              </label>
              {explain && (
                <p className="opt-help">
                  For a room or lifestyle shot: a model lists the objects and each is cut out into
                  its own named PSD layer with its own saved path — bed, pillows, plant, and so on.
                  Leave off for a single product.
                </p>
              )}

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

              <label className="opt-check">
                <input
                  type="checkbox"
                  checked={cutout.keep_losing_candidate}
                  onChange={(e) => setCutout({ keep_losing_candidate: e.target.checked })}
                />
                Keep the losing candidate for side-by-side review
              </label>
              </Advanced>
            </ServiceRow>

            {/* ── Background ─────────────────────────────────────── */}
            <ServiceRow
              icon={<BackgroundIcon />}
              iconClass="srow-icon-bg"
              name="Background Services"
              detail="Replace the removed background with a solid colour, or leave it transparent."
              alwaysOn
            >
              {/*
                Transparency is a first-class choice here, beside the colour, rather than something
                you reach by switching the whole service OFF — which is how it used to work, and
                which hid the colour box the moment you found it. Two problems with that: "turn
                Background Services off" does not read as "give me a transparent PNG", and a user
                looking for transparency saw nothing but a colour picker. Client feedback, and
                fair.

                The colour row stays mounted and visible in both modes, greyed when transparent, so
                the setting you picked earlier is still legible and comes back untouched when you
                switch away.
              */}
              <p className="opt-sublabel">Background</p>
              <div className="opt-radio-row">
                <label className="opt-radio">
                  <input
                    type="radio"
                    name="bgmode"
                    checked={!background.transparent}
                    onChange={() => setBackground({ transparent: false })}
                  />
                  Solid colour
                </label>
                <label className="opt-radio">
                  <input
                    type="radio"
                    name="bgmode"
                    checked={background.transparent}
                    onChange={() => setBackground({ transparent: true })}
                  />
                  Transparent
                </label>
              </div>

              <p className="opt-sublabel">Replacement colour</p>
              <div className={`color-input-row${background.transparent ? ' opt-disabled' : ''}`}>
                <div className="color-swatch" style={{ background: background.color ?? '#FFFFFF' }}>
                  <input
                    type="color"
                    className="opt-color-picker"
                    disabled={background.transparent}
                    value={normaliseHex(background.color)}
                    onChange={(e) => setBackground({ color: e.target.value.toUpperCase() })}
                  />
                </div>
                <input
                  type="text"
                  className="opt-input hex-input"
                  maxLength={7}
                  placeholder="#RRGGBB"
                  disabled={background.transparent}
                  value={background.color ?? ''}
                  onChange={(e) => setBackground({ color: e.target.value })}
                />
              </div>
              {background.transparent ? (
                <p className="opt-note">
                  Transparent output — JPEG, BMP and EPS are unavailable, since none carries alpha.
                </p>
              ) : (
                <>
                  {!hexValid && <p className="opt-error">Enter a hex colour like #F5F5F5 or #FFF.</p>}
                  {explain && (
                    <p className="opt-help">
                      Delivered byte-exact. The value is treated as sRGB and converted, not copied,
                      when the output profile is Adobe RGB.
                    </p>
                  )}
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
              {explain && (
                <p className="opt-help">
                  Preserving reconstructs the photographed shadow onto the new colour. It needs a
                  uniform original backdrop, so on a lifestyle scene it is skipped and says so.
                </p>
              )}

              <Advanced>
                <label className="opt-check">
                  <input
                    type="checkbox"
                    checked={background.decontaminate_edges}
                    onChange={(e) => setBackground({ decontaminate_edges: e.target.checked })}
                  />
                  Decontaminate edges — removes the halo left by the original backdrop
                </label>
                {explain && (
                  <p className="opt-help">
                    On by default. Un-mixes the original backdrop out of the edge band, which is
                    what stops a white studio sweep leaving a halo against a saturated colour.
                  </p>
                )}
              </Advanced>
            </ServiceRow>

            {/* ── Output size ────────────────────────────────────── */}
            <ServiceRow
              icon={<SizeIcon />}
              iconClass="srow-icon-size"
              name="Output Size"
              detail="Resize the output canvas to exact pixel dimensions for marketplace compliance."
              alwaysOn
            >
              {/*
                Offered first, and above the presets, because it is the answer to "the download
                looks softer than what I uploaded". That is a canvas choice, not a resampling
                fault: a 50 MP raw delivered at the 500x500 default keeps 0.5% of its pixels.
              */}
              <label className="opt-check">
                <input
                  type="checkbox"
                  checked={size.match_source}
                  onChange={(e) => setSize({ match_source: e.target.checked })}
                />
                Keep the original resolution of each image
              </label>
              {explain && (
                <p className="opt-help">
                  Delivers at the uploaded photograph’s own pixel dimensions instead of a fixed
                  canvas — every image keeps the detail it arrived with. Tick this when the output
                  looks less sharp than the source. Leave it off when the order needs one exact
                  size for a marketplace.
                </p>
              )}

              <div className={`preset-row${size.match_source ? ' opt-disabled' : ''}`}>
                {SIZE_PRESETS.map((p) => (
                  <button
                    key={p.label}
                    type="button"
                    disabled={size.match_source}
                    className={`preset-btn${!size.match_source && size.width === p.w && size.height === p.h ? ' active' : ''}`}
                    onClick={() => setSize({ width: p.w, height: p.h })}
                  >
                    {p.label}
                  </button>
                ))}
              </div>

              <p className="opt-sublabel">Width × Height</p>
              <div className={`size-input-row${size.match_source ? ' opt-disabled' : ''}`}>
                <input
                  type="number"
                  className="opt-input size-num"
                  min={1}
                  max={20000}
                  disabled={size.match_source}
                  value={size.width}
                  onChange={(e) => setSize({ width: clampDim(e.target.value) })}
                />
                <span className="size-sep">×</span>
                <input
                  type="number"
                  className="opt-input size-num"
                  min={1}
                  max={20000}
                  disabled={size.match_source}
                  value={size.height}
                  onChange={(e) => setSize({ height: clampDim(e.target.value) })}
                />
                <span className="size-unit">px</span>
              </div>
              {explain && !size.match_source && (
                <p className="opt-help">Asserted before delivery — 500 × 500 means exactly 500 × 500.</p>
              )}

              <label className="opt-check">
                <input
                  type="checkbox"
                  checked={exportSpec.match_source}
                  onChange={(e) => setMatchSource(e.target.checked)}
                />
                Return each image in the format it was uploaded as
              </label>
              {explain && (
                <p className="opt-help">
                  A PNG in gives a PNG out, a TIFF a TIFF, a PSD a PSD — combined with anything
                  ticked below. Camera raw (CR2, CR3, NEF, CRW, DNG, RAW) is the exception: it
                  decodes but cannot be written back, so those deliver 16-bit TIFF and say so.
                </p>
              )}

              <p className="opt-sublabel">
                Also deliver
                {exportSpec.formats.length === 0 && (
                  <span className="opt-sublabel-note"> — nothing extra; uploaded format only</span>
                )}
              </p>
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
              {exportSpec.match_source && exportSpec.formats.length > 0 && (
                <p className="opt-note">
                  Each image is delivered twice: in its own format, plus{' '}
                  {exportSpec.formats.map((f) => f.toUpperCase()).join(' + ')}. Un-tick all of the
                  above for one file per upload.
                </p>
              )}

              <Advanced>
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
              {explain && (
                <p className="opt-help">
                  Off by default: an undersized master is padded rather than stretched, and the
                  result says which happened.
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
              </Advanced>
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

              <Advanced>
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
              {explain && (
                <p className="opt-help">
                  These disagree for an asymmetric product — a mug with a handle looks centred by
                  bounding box but sits off-centre by mass. The residual offset is measured and
                  reported either way.
                </p>
              )}

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
              {explain && (
                <p className="opt-help">
                  Which pixels count when measuring the subject&rsquo;s bounds. Measurement only —
                  the stored alpha is never thresholded.
                </p>
              )}
              </Advanced>
            </ServiceRow>

            {/* ── PSD ────────────────────────────────────────────── */}
            {SHOW_PSD && (
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
                <Advanced>
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
                </Advanced>
              </ServiceRow>
            )}
          </div>
        </div>
      </div>
    </div>
  )
}

/**
 * Controls with a correct default that are rarely moved. Collapsed, native `<details>`.
 *
 * Native rather than a hand-rolled disclosure so keyboard, screen readers and in-page find all
 * work without any code — the same reasoning as using Radix for the zoom dialog rather than a
 * click-outside div.
 */
function Advanced({ label = 'Advanced', children }: { label?: string; children: React.ReactNode }) {
  return (
    <details className="opt-advanced">
      <summary>{label}</summary>
      <div className="opt-advanced-body">{children}</div>
    </details>
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
    <section className={`service-row${open ? '' : ' is-off'}`}>
      {/*
        Description on the left, controls on the right — the layout settings pages use, and the
        answer to a real problem the card grid could not solve: Background Removal holds one input while
        Output Size holds a dozen, so any multi-column grid of boxes left one card a third the
        height of its neighbour with the difference as dead space. In a single column, unequal
        content cannot produce a void; each section is simply as tall as it needs to be.
      */}
      <div className="srow-aside">
        <div className="srow-header">
          <div className={`srow-icon ${iconClass}`}>{icon}</div>
          <div className="service-row-name">{name}</div>
        </div>
        <p className="service-row-detail">{detail}</p>
        {/*
          No switch for a stage that cannot be switched off. Rendering a disabled toggle reading
          "ALWAYS ON" put four identical dead controls down the page, each inviting a click that
          does nothing. Sections that are genuinely optional still get a real one.
        */}
        {!alwaysOn && (
          <label className="toggle-switch" title={toggleTitle}>
            <input
              type="checkbox"
              checked={open}
              onChange={(e) => onToggle?.(e.target.checked)}
            />
            <span className="toggle-track" />
            <span className="toggle-label">{open ? 'On' : 'Off'}</span>
          </label>
        )}
      </div>
      <div className={`service-options${open ? ' open' : ''}`}>{children}</div>
    </section>
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

function BackgroundRemovalIcon() {
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

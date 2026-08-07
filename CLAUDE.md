# DropYourImage POC — project context

## What this is

A **5-day stakeholder demo** proving five image-processing capabilities: background removal, background colour
by hex code, exact output resize, product centring, and layered PSD delivery with a vector clipping path. Plus
preservation of the original photograph's shadows/reflections onto the new background.

Client source material: `Dropyourimage_AI_Platform_Model_Cost_and_Delivery_Plan1 1.docx` — their AI Enablement
Report, which scopes 9 platform features. This POC builds a slice of Features 1, 2 and 3.
Approved delivery plan: `~/.claude/plans/the-week-is-for-polymorphic-bubble.md`.

## Prime directive — do not violate this

**Only stage 1 (segmentation) is an AI API call. Stages 2–5 are deterministic code.**

Stages 2–5 are: shadow reconstruction, geometry, background fill, export. Never route these through a
generative or vision model, no matter how convenient it looks. Reasons, in priority order:

1. A generative model cannot return `#F5F5F5` byte-exact. Exactness *is* the requirement.
2. It cannot return the same result twice — which breaks the determinism test and any audit story.
3. It re-renders the product pixels, destroying packshot fidelity.
4. It is ~50× more expensive and thousands of times slower.

The client's own report says the same: *"Sending this work to a vision model would be both slower and roughly
50x more expensive than needed."* If a task is measurable, measure it. Models are for judgement only.

Legitimate AI uses in this codebase, and the complete list of them:
- Background removal / segmentation (stage 1) — commercial APIs, dual-engine
- The auto-pick **tie-break** when deterministic scoring cannot separate two candidate cut-outs
  (`gemini-3.5-flash`; comparisons are counterbalanced because position bias was measured)
- **Subject localisation** from a text prompt, when a photograph contains several objects
  (`app/engines/locate.py`). Background removal separates foreground from background and structurally
  cannot say *which* foreground you meant; on a furnished-room shot it returned a clean mask of the
  sofa when the coffee table was wanted. The model returns one bounding box — semantics, i.e.
  judgement. The box is then padded and cropped by deterministic code, and the mask still comes from
  the segmentation engine at full precision. No model touches output pixels.
- Optional AI upscale when the requested canvas exceeds the master resolution
- Optional per-output QC score (a buy-back option, not built by default)

Note what each of these has in common: the model supplies a *decision* — which engine, which object —
never a pixel. If a proposed AI use would produce or alter output pixels, it is not on this list.

## Constraints

- 5 working days, 3 developers, **no schedule buffer on anyone**. Prefer the simple version that ships.
- Demo target: no auth, no multi-tenancy, no production infra, no autoscaling.
- Paid APIs are explicitly approved where they buy time or quality. Budget ~$1,300 for the POC month, ~87% of
  which is Adobe's monthly minimum.
- **No GPU, no self-hosted models.** Architectural constraint from the client's report. Everything AI is a
  metered API call.

## Structure — two independent projects

```
backend/     Python · FastAPI · own .venv + requirements
frontend/    TypeScript · React · own package.json
data/        real test photos — GITIGNORED, never committed
docs/        shared reference; docs/API_CONTRACT.md is the only coupling
```

Neither project has a Dockerfile — both run natively during the sprint, and `docker-compose.yml`
provides only Redis and MinIO. Containerising the apps is out of scope.

**Nothing in `frontend/` imports from `backend/` or vice versa.** They talk over HTTP through the contract in
`docs/API_CONTRACT.md`, which is frozen — changing it requires updating both sides and the doc in the same
change. Keep modules small and behind interfaces; assume any one of them gets swapped.

Each project has its own `CLAUDE.md` with rules specific to it. Read those when working in them.

## Conventions

- Python deps live in `backend/.venv` — **never** install into the system interpreter (it is apt-managed).
- `pytest` must pass on a clean checkout with **no API keys and no images**. Tests use synthetic fixtures from
  `backend/tests/fixtures/`; they never read `data/`.
- Secrets come from env via `pydantic-settings`, server-side only. Never in a response body, log line, or
  anything the browser sees.
- Real client photographs stay in `data/` and out of git.

## Out of scope — do not add these, and keep them out of the demo set

Glass and clear plastic, sheer fabric (tulle/lace/mesh), dedicated hair/fur handling. Soft alpha is preserved
throughout so these degrade gracefully rather than being actively broken, but they are **not** supported and
will look bad if shown. Keep samples in `data/input/edge-cases/` so we know how bad, rather than guessing.

Also not in scope: measured quality claims. There is no ground-truth mask set and no SAD/MSE/ΔE harness, so the
demo can show quality but cannot put a number on it, nor answer "which API should we buy". Do not imply
otherwise in UI copy or docs.

## Known blocker

Adobe Firefly Services / Photoshop API access is **enterprise-quoted, not self-serve card signup**. The PSD
stage depends on it. Until credentials exist, the PSD path runs the fallback (see `docs/PSD.md`). Do not assume
this key exists.

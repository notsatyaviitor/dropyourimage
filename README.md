# DropYourImage — Image Processing POC

A 5-day stakeholder demo proving five capabilities end to end: **background removal**, **background colour by
hex code**, **exact output resize**, **product centring**, and **layered PSD delivery with a vector clipping
path** — plus preservation of the original photograph's **shadows and reflections** onto the new background.

Upload product photos, configure the pipeline in a four-step order wizard, download processed assets.

> **This is a demo, not a product.** No auth, no multi-tenancy, no production infrastructure. It shows quality
> convincingly but cannot support a *numeric* quality claim — see [docs/LIMITATIONS.md](docs/LIMITATIONS.md).

## The one design decision that explains everything else

Only **stage 1 (segmentation)** is an AI API call. Stages 2–5 — hex fill, resize, centring, shadow
recomposition — are deterministic imaging code.

This is not a cost decision. A generative model cannot return `#F5F5F5` byte-exact, cannot return the same
result twice, and re-renders your product pixels while trying. The client's own AI Enablement Report reaches
the same conclusion: *"Sending this work to a vision model would be both slower and roughly 50x more expensive
than needed."*

Exactness is the requirement, so exactness gets code. Judgement gets a model.

## Two independent projects

`backend/` and `frontend/` are **separate deployables with no shared build**. They communicate only over HTTP,
through the contract in [docs/API_CONTRACT.md](docs/API_CONTRACT.md). Either can be replaced wholesale without
touching the other.

```
dropyourimage/
├── backend/          Python · FastAPI · own .venv and requirements   → see backend/README.md
├── frontend/         TypeScript · React · own package.json           → see frontend/README.md
├── data/             test images (real photos, gitignored)            → see data/README.md
├── docs/             shared reference — the contract lives here
└── docker-compose.yml    Redis and MinIO only; the apps run natively
```

There are no Dockerfiles. Both apps run directly (`uvicorn` / `vite`) for fast reloads, and
containerising them is out of scope for the demo.

Nothing in `frontend/` imports from `backend/`, and vice versa. The contract doc is the only coupling, and it
is frozen on Day 1.

## Documentation

Read in this order.

| Doc | What it covers |
|---|---|
| [docs/SETUP.md](docs/SETUP.md) | Running the whole stack locally |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Services, the five pipeline stages, why they run in that order, and the module boundaries |
| [docs/WALKTHROUGH.md](docs/WALKTHROUGH.md) | The four core capabilities traced from API field to output pixel |
| [docs/API_CONTRACT.md](docs/API_CONTRACT.md) | **Frozen Day 1 contract.** Three devs work in parallel against this |
| [docs/IMAGE_PIPELINE.md](docs/IMAGE_PIPELINE.md) | The imaging maths: linear light, decontamination, shadow ratio maps |
| [docs/ENGINES.md](docs/ENGINES.md) | Segmentation engines, dual-engine auto-pick, per-call costs |
| [docs/PSD.md](docs/PSD.md) | Layered PSD via the Adobe Photoshop API, and the fallback ladder |
| [docs/UI.md](docs/UI.md) | Brand tokens, the order wizard, and what is shown but deliberately inert |
| [docs/SECURITY.md](docs/SECURITY.md) | Hardening checklist — zip ingest is the attack surface |
| [docs/TESTING.md](docs/TESTING.md) | What is asserted automatically vs checked by eye |
| [docs/LIMITATIONS.md](docs/LIMITATIONS.md) | **Read before demoing.** What is out of scope and will look bad |

Per-project setup lives with each project: [backend/README.md](backend/README.md),
[frontend/README.md](frontend/README.md).

Source material: `Dropyourimage_AI_Platform_Model_Cost_and_Delivery_Plan1 1.docx` (the client's AI Enablement
Report). The approved delivery plan is at `~/.claude/plans/the-week-is-for-polymorphic-bubble.md`.

## Quick start

```bash
docker compose up -d                  # Redis + MinIO
cd backend && ./scripts/setup.sh      # creates backend/.venv, installs deps
source .venv/bin/activate && pytest -q # imaging tests need no API keys
```

Then [docs/SETUP.md](docs/SETUP.md) for the full path including the frontend and API keys.

## Status

The four core capabilities — background removal, hex background, exact resize, centring — are implemented and
verified end to end: `652 passed, 0 skipped`, with byte-exact hex and exact canvas dimensions asserted both in
tests and over HTTP. The imaging core (stages 2–5) needs **no API credentials** and is fully testable against
synthetic fixtures, so procurement cannot block it.

Run `python scripts/demo.py` for output you can look at, and see [docs/WALKTHROUGH.md](docs/WALKTHROUGH.md) for
how each capability is implemented.

**Known blocker:** Adobe Firefly Services / Photoshop API access is enterprise-quoted, not self-serve. Until it
is live the PSD stage runs the fallback path — see [docs/PSD.md](docs/PSD.md).

**Layered PSD now works** (fixed 7 Aug 2026). It had been failing outright for two reasons, both
hidden by the fact that the two PSD suites *skipped* on a missing `pytoshop` — a green suite over a
dead feature. `pytoshop==1.2.1` is now pinned in `requirements.txt`, and the validator no longer
string-matches an `IntEnum` (Python 3.11 changed `IntEnum.__str__`, so it had been rejecting every
correctly-written RGB PSD). Verified live against both Photoroom and Gemini cut-outs: three layers
`BG`/`SHADOW`/`PROD`, RGB 8-bit, a real 16-vertex vector clipping path, ICC embedded.

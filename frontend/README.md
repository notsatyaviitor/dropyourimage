# Frontend — DropYourImage POC

React + TypeScript + Vite. Five tabs configure one `JobConfig`; one upload runs the whole
pipeline. See `CLAUDE.md` for the working rules (brand tokens, no vendor calls from the browser,
guard-rail states must render).

## Run

```bash
npm install
npm run dev          # http://localhost:5173, proxies /api -> http://localhost:8000
```

If something else already holds port 8000:

```bash
VITE_BACKEND_URL=http://localhost:8001 npm run dev
```

The backend must be running (see `../backend/README.md`) — memory mode (no Redis/MinIO) is
enough for local development; nothing here talks to a vendor API directly.

## Structure

```
src/
  theme/tokens.ts     brand tokens pulled from the live dropyourimage.com CSS — see the file header
  api/                types.ts (contract mirror), client.ts, useJobPolling.ts
  components/ui/      Radix-based primitives (button, card, tabs, select, ...)
  tabs/               the five pipeline-stage config panels
  upload/             zip dropzone
  results/            results grid + per-image card (checkerboard, pixel-peep, candidates)
```

## Verified working

Driven end to end with Playwright against a live backend (memory mode) on 2026-08-05: all five
tabs render and update state, zip upload → job creation → polling → results grid all work with
zero console errors and zero failed HTTP requests. A synthetic two-image batch (coloured product
on a light backdrop) was processed correctly — background removed, composited onto an exact
requested hex, centred, with `single_engine_only` and `upscale_skipped` notes surfaced honestly
since no vendor keys are configured in this environment.

One real bug was found and fixed this way: a global `h1 { color: navy }` rule outside any
Tailwind `@layer` was beating the header's `text-white` utility class regardless of specificity
(unlayered CSS always wins over layered CSS), rendering "DropYourImage" as invisible navy-on-navy
text. Fixed by wrapping base element styles in `@layer base` in `src/index.css` — see the comment
there for the full explanation.

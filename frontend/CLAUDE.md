# Frontend — working rules

TypeScript · React · Vite · shadcn/ui + Radix. Read the root `CLAUDE.md` first.

## Environment

```bash
cd frontend && npm install && npm run dev     # :5173, expects the API on :8000
```

Independent of `backend/` — no shared build, no imports across the boundary. The only contract is
`docs/API_CONTRACT.md`; mirror its types in `src/api/types.ts` and keep them in sync by hand when the contract
changes.

## Design: match dropyourimage.com

Target **visually consistent, not pixel-perfect.** Pixel-matching a marketing site is not worth demo days.

**Real tokens, pulled from the live CSS (not eyeballed) — see `src/theme/tokens.ts` for the full
source dump.** Fetched directly via `curl`, since WebFetch's markdown conversion strips external
CSS: `#152339` navy (primary colour, headings, header background), `#2cbc63` green (the real
accent — checklist/CTA highlight), `#023a51` for links, `#474747` body text, `#ffffff` content
background, `#e2e2e2` page background. Headings in Ubuntu, body in Open Sans, nav in Manrope.
Buttons are nearly square (2px radius); forms/cards are more rounded (8px). Corporate B2B tone —
trustworthy and efficiency-focused, not playful.

**Correction, worth flagging because it changes the palette:** an earlier casual visual pass
called the accent "blue". The actual CSS custom properties say the accent is **green**
(`#2cbc63`), and navy is the primary/dark colour, not blue. Trust `tokens.ts` and the CSS it was
pulled from over any prose description, including this one.

- Import from `src/theme/tokens.ts` everywhere. Do not hardcode hex values anywhere else — if a
  colour isn't in `tokens.ts`, that's a sign to go re-check the live CSS rather than guess.
- Reuse their card-grid pattern for the results grid and their button styling throughout.
- **Use their vocabulary, not ours.** Their existing services map onto this POC — *Clipping* and *Background
  Services*. Name tabs with their terms.
- English only. Skip their EN/NL/DE language switcher.

## No paid component kit

Use shadcn/ui + Radix (free, headless). A paid kit's value is its opinionated visual design, which we would be
overriding to match the brand anyway. If you find yourself wanting a paid kit, you want a styled component —
build it on the Radix primitive instead.

## The five tabs are ONE pipeline, not five tools

Upload a zip once; tabs 1–5 configure stages of a single run; one submit produces the final assets. Do not
build five independent uploaders or five job types. There is one `JobConfig` and one job.

Tab order mirrors the pipeline: cut-out → background → size → centring → PSD.

## Rules

- **Never call a vendor API from the browser.** Not Photoroom, not Gemini, not Adobe. All AI goes through our
  backend so keys stay server-side. If the UI seems to need a vendor key, the design is wrong.
- Poll `GET /jobs/{id}` for progress. No websockets — not worth the complexity for a demo.
- Show the alpha channel honestly: checkerboard transparency view and a pixel-peep zoom. Reviewers must be able
  to see edge quality, which is the whole point.
- Surface *which engine won* per image, and keep the losing candidate viewable side by side. The demo needs to
  explain why an output looks the way it does.
- Show guard-rail states as real UI, not silent fallbacks: when the background-uniformity gate trips and the
  shadow cannot be preserved, say so on the image. A silent fallback reads as a bug during a demo.
- Error states must distinguish "vendor rate-limited" from "unsupported file" from "our bug" — the API returns a
  taxonomy, so use it rather than a generic failure toast.

## Do not imply measured quality

There is no ground-truth benchmark behind this POC. Never label anything an accuracy score, a confidence
percentage, or a quality grade unless it comes from a real computed metric in the API response. Invented
numbers in a demo become promises.

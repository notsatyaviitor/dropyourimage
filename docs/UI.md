# Frontend UI

Five tabs configure one `JobConfig`; one upload runs the whole pipeline once. See
`frontend/CLAUDE.md` for the working rules this section summarises.

## Brand — pulled from the live site, not eyeballed

WebFetch strips external stylesheets when converting to markdown, so an initial visual pass
missed the real palette. Fetched directly via `curl -A "Mozilla/5.0" https://www.dropyourimage.com/`
and read the theme's inline CSS custom properties:

| Token | Value | Source |
|---|---|---|
| Navy (primary, headings, header bg) | `#152339` | `--primary_color`, `--awb_header_bg_color` |
| **Accent — green, not blue** | `#2cbc63` | `--checklist_circle_color` |
| Link | `#023a51` | `--link_color` |
| Body text | `#474747` | `--body_typography-color` |
| Content background | `#ffffff` | `--content_bg_color` |
| Page background | `#e2e2e2` | `--bg_color` |
| Button radius | `2px` (nearly square) | `--button_border_radius` |
| Form/card radius | `8px` | `--form_border_radius` |
| Headings | Ubuntu | `--h1..h6_typography-font-family` |
| Body | Open Sans | `--body_typography-font-family` |
| Nav | Manrope | `--nav_typography-font-family` |

**Correction worth flagging:** an earlier casual visual pass called the accent "blue". The actual
CSS says green. `frontend/src/theme/tokens.ts` is the source of truth going forward.

## Tab structure

Tabs mirror the pipeline stage order and reuse the client's own service vocabulary where it
exists (per the delivery plan): **Clipping** (cutout), **Background Services** (hex/shadow),
**Size** (canvas + output formats), **Centring**, **PSD**. All five edit slices of one
`JobConfig` object held in `App.tsx` — there is one job per upload, not five separate tools.

## A real bug found by actually running it, not by compiling it

The frontend was driven end-to-end with Playwright (installed for verification only, removed
afterward) against a live backend. It caught a genuine defect: the "DropYourImage" header
rendered invisible — navy text on the navy header background. A plain `h1 { color: navy }` rule
in `index.css`, written outside any Tailwind `@layer`, was beating the header's `text-white`
utility class regardless of CSS specificity — unlayered CSS always wins over layered CSS in the
cascade, independent of selector specificity. Fixed by wrapping base element styles in
`@layer base`. See `index.css`'s comment for the full explanation and `frontend/README.md` for
what else the same verification pass confirmed working (all five tabs, upload → process → results,
zero console errors, zero failed requests).

## Rules that matter

- **Never call a vendor API from the browser.** All AI goes through the backend so keys stay
  server-side.
- **Show guard-rail states as real UI**, not silent fallbacks — every `Note` from the contract has
  a rendered label (`NOTE_LABELS` in `api/types.ts`).
- **Surface which engine won**, with the losing candidate viewable side-by-side, so a demo can
  explain why an output looks the way it does.
- **Never label anything an accuracy score or confidence percentage** unless it is a real number
  from the API response — there is no ground-truth benchmark behind this POC (see
  `docs/LIMITATIONS.md`).

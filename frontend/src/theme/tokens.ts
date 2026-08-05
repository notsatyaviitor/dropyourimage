/**
 * Brand tokens pulled from the live dropyourimage.com CSS — not eyeballed.
 *
 * Source: `curl -A "Mozilla/5.0" https://www.dropyourimage.com/` on 2026-08-05, reading the
 * theme's inline `<style id="litespeed-ucss">` CSS custom properties directly (an Avada/WordPress
 * Fusion Builder site — the tokens live as `--name: value;` declarations in that block). Every
 * value below is quoted from there, not approximated:
 *
 *   --primary_color: #152339                (also the header/nav background)
 *   --h2..h6_typography-color: #152339
 *   --h1_typography-color: #ffffff           (white, on the dark hero)
 *   --body_typography-color: #474747
 *   --link_color: #023a51
 *   --checklist_circle_color: #2cbc63        (the real accent — see note below)
 *   --countdown_background_color: #65bc7b    (accent hover/light variant)
 *   --content_bg_color: #ffffff
 *   --bg_color: #e2e2e2                       (page background outside content)
 *   --button_border_radius: 2px
 *   --form_border_radius: 8px
 *   --button_gradient_top/bottom_color: #152339 -> hover #2a3951
 *   --button_padding: 11px 23px
 *   --h1..h6_typography-font-family: Ubuntu, Arial, Helvetica, sans-serif
 *   --body_typography-font-family: "Open Sans", "MS Sans Serif", Geneva, sans-serif
 *   --nav_typography-font-family: Manrope, "MS Sans Serif", Geneva, sans-serif
 *   --button_typography-font-family: Open Sans (600 weight)
 *   .fusion-row max-width: 1260px
 *
 * CORRECTION vs. an earlier casual read of the page: the accent is GREEN (#2cbc63), not blue.
 * "Blue accent" was a visual first impression from a screenshot-level pass; the actual CSS custom
 * properties say otherwise, and this file follows the CSS, not the impression.
 *
 * Approach note: exported as plain values (not Tailwind's `@theme` CSS-first syntax) so the same
 * source of truth can be consumed by both `tailwind.config` extensions and any plain-TS styling
 * (checkerboard overlay math, canvas grid), without duplicating the numbers in two files.
 */

export const brand = {
  color: {
    /** #152339 — primary_color / h2-h6 text / header background */
    navy: '#152339',
    /** #2a3951 — button gradient hover state; used as navy's hover/active shade throughout */
    navyHover: '#2a3951',
    /** #023a51 — link_color */
    link: '#023a51',
    /** #474747 — body_typography-color */
    body: '#474747',
    /** #2cbc63 — checklist_circle_color, the real accent (see file-level note) */
    accent: '#2cbc63',
    /** #65bc7b — countdown_background_color, accent's lighter/hover variant */
    accentLight: '#65bc7b',
    /** #ffffff — content_bg_color */
    surface: '#ffffff',
    /** #e2e2e2 — bg_color, page background outside the content column */
    pageBg: '#e2e2e2',
  },
  radius: {
    /** button_border_radius — the live site's buttons are nearly square */
    button: '2px',
    /** form_border_radius — inputs/cards read as more rounded than buttons on the real site */
    form: '8px',
  },
  font: {
    /** h1-h6 font-family */
    heading: 'Ubuntu, Arial, Helvetica, sans-serif',
    /** body_typography font-family */
    body: '"Open Sans", "MS Sans Serif", Geneva, sans-serif',
    /** nav_typography font-family, used here for the tab bar to match their nav */
    nav: 'Manrope, "MS Sans Serif", Geneva, sans-serif',
  },
  layout: {
    /** .fusion-row max-width */
    maxWidth: '1260px',
  },
} as const

/**
 * Fonts are loaded from Google Fonts rather than self-hosting Avada's own font files (those are
 * theme assets, not published for reuse). Google Fonts serves the same families; see index.html.
 */
export const GOOGLE_FONTS_URL =
  'https://fonts.googleapis.com/css2?family=Ubuntu:wght@400;500;700&family=Open+Sans:wght@400;600&family=Manrope:wght@500;600&display=swap'

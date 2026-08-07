# Design prototype

The original clickable mockup of the order wizard: `poc.html`, `poc.css`, `poc.js`.

**This is not the frontend.** It is kept for provenance and so the intended design can be opened
directly in a browser for comparison. The live implementation is `frontend/`, which uses the same
design driven by the real API.

## What was ported

`poc.css` is now `frontend/src/styles/poc.css`, near-verbatim. Three changes, all explained in that
file's header: the global `*` reset and `body` rules moved into `frontend/src/index.css` (unlayered
CSS would have beaten every Tailwind utility), the Google Fonts `@import` was dropped because
`index.html` already preloads those families, and the `:root` palette now aliases the `@theme`
tokens instead of re-declaring the same hex values.

The markup became React components: `frontend/src/shell/` for the sidebar and wizard header,
`frontend/src/steps/` for the four steps and the processing overlay.

## What was deliberately not ported

`poc.js` simulated the pipeline. Nothing in it reached a backend:

- `_runProcessing()` advanced four stage rows on a 1100 ms timer and always finished at 100%. It had
  no failure state.
- The four sample products were Unsplash URLs, and each `afterUri` was an **unrelated** stock photo
  that happened to already be on white. Nothing had been processed.
- For a file the user actually uploaded, `afterUri: url` — the "after" was the untouched original.
- The replacement background colour was a CSS `background-color` on the wrapping div, so it could
  never be byte-exact and sat behind an opaque JPEG.
- Every result card was stamped `AUTO-PASS`; `downloadResults()` was an `alert()`.
- `€ 9.65` per image, VAT and total were invented figures.

A demo whose purpose is to prove four capabilities cannot show output the pipeline did not produce,
so all of the above is driven by `JobStatus` in the port. Features with no backend behind them —
SFTP ingest, pricing, the other nav sections — are rendered visibly disabled and labelled rather
than removed, so the shape of the full product is still legible without implying it works.

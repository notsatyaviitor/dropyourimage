# Security

This is a demo with no auth and no multi-tenancy. The checklist below is what *is* in place,
scoped to the risks that actually apply to a POC: untrusted uploads and vendor credentials.

## Zip ingest is the attack surface (`app/ingest/zip_reader.py`)

Every guard here is a control against a specific attack, tested against a constructed exploit,
not asserted in the abstract (`tests/test_zip_ingest.py`, 22 tests):

| Guard | Stops |
|---|---|
| Path/traversal rejection (`../`, absolute paths, drive letters, NUL bytes) | Zip-slip |
| Entry count cap | A million-tiny-file archive exhausting the queue |
| Total uncompressed size cap | The classic zip bomb (KB → GB) |
| Per-entry compression-ratio cap | One entry that is mostly zeroes, slipping under a *total* cap |
| MIME sniffing (never trusts the extension) | A `.jpg` that is actually something else |
| Read capped at `declared_size + 1` regardless of what the entry claims to expand to | A forged central-directory size lying about real content |

One rejected entry never blocks the rest of the batch — rejections are reported per-file in the
results grid, not silently dropped or fatal to the whole job.

## Upload size

`_read_capped()` in `app/api/routes.py` enforces the byte cap against bytes **actually read**,
independent of any `Content-Length` header a client could lie about — the same principle as the
zip-bomb guards above.

## Secrets

- Read server-side only, via `pydantic-settings`. Never in a response body, a log line, or
  anything the browser can see.
- **A `.env` gotcha, found and fixed:** an inline comment on the same line as an unset key
  (`KEY=   # note`) makes `python-dotenv` read the comment text *as the value* — the app then
  believes a vendor key is configured, sends the comment to the vendor, and gets a 401 that looks
  exactly like a genuinely bad key. `Settings` now validates against this directly
  (`_reject_stray_comment_as_a_key`), and every `.env.example` comment is on its own line.
- `scripts/set_env_key.py` sets a single key via hidden input, so a secret never touches shell
  history.

## Signed URLs

Output assets are the only thing this POC exposes publicly, and there is no auth — every asset
URL is short-lived and unguessable (`SIGNED_URL_TTL_SECONDS`).

## What is deliberately out of scope for a demo

No auth, no rate limiting per user, no CSRF protection (no cookies/sessions exist to protect), no
production TLS termination (left to the deployment environment), CORS wide open (`*`) since there
are no cookies and no authenticated sessions to leak. Tighten CORS to the real frontend origin
before this touches anything beyond the demo.

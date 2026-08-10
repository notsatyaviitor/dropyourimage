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
| **`MAX_IMAGE_PIXELS`** (80 MP) at decode | The decompression bomb — this is the guard that matters |

**There is deliberately no per-image byte cap** (`MAX_IMAGE_BYTES` defaults to `0`, meaning
unlimited). A byte count measures the container, not the threat, and it was actively harmful here:
50 MB rejected an entire 132-file PSD set at 130 MB each, and 200 MB then rejected a 260 MB one —
legitimate source material refused, each time costing a debugging round to trace back to a number
in a config file. Meanwhile a 500 KB PNG can still decode to 400 MP, which the byte cap would wave
straight through.

`MAX_IMAGE_PIXELS` is the real control: it bounds what `decode` allocates, which is what a bomb
attacks. What removing the byte cap actually changes is that **available RAM becomes the ceiling**
instead of a configured number, since an entry is held in memory while it is read. Set
`MAX_IMAGE_BYTES` to a non-zero value on a memory-constrained deployment; every byte setting in
this codebase treats `0` as "no limit".

One rejected entry never blocks the rest of the batch — rejections are reported per-file in the
results grid, not silently dropped or fatal to the whole job.

### Streaming ingest screens identically

A bulk job reads entries one at a time (`plan_entries` + `read_entry`) instead of all at once, so
400 images do not have to be resident before processing starts. Both paths run **the same
screening code** — `read_images` is implemented on top of the streaming primitives rather than
keeping its own copy, because a second copy of a security screen is how one of them silently falls
behind the other.

One check moved: the `declared_size + 1` read now happens in `read_entry` rather than during
planning. That is not a weakening — it runs at the moment the bytes actually enter memory, which is
the moment the guard protects. Planning reads at most 4096 bytes per entry, which is all the MIME
sniff can use anyway.

## Upload size

`_read_capped()` in `app/api/routes.py` enforces the byte cap against bytes **actually read**,
independent of any `Content-Length` header a client could lie about — the same principle as the
zip-bomb guards above.

### Job-level caps, because a job is now several uploads

The guards above bound **one archive**. A bulk order arrives as several archives against one job
id, so without a second layer, N uploads would multiply straight through every per-archive cap.
Enforced in `routes.py::_accept_batch` at the point a batch is appended:

| Cap | Default | Stops |
|---|---|---|
| `MAX_JOB_IMAGES` | 500 | Unbounded images accumulated across batches |
| `MAX_JOB_UPLOAD_BYTES` | 4 GB | Unbounded storage consumed by one job id |

Both return `413` with `batch_too_large` — deliberately *not* `malicious_archive`, because nothing
here is hostile or malformed, there is simply too much of it, and mislabelling it teaches the user
the wrong thing.

**A later batch is not a trusted caller.** Every archive is screened independently; passing an
earlier batch buys nothing. The config is read back from storage rather than taken from the
request, so a later batch cannot alter what an accepted job does.

## Spend, as a safety control

A 400-image order on the dual-engine pool is roughly $24 of vendor calls, against a POC budget of
about $1,300/month — from an endpoint with no auth. `MAX_JOB_COST_USD` (default $25) stops a job
that reaches it and keeps what it produced. Each image reserves its projected cost before starting,
so the ceiling binds on *spent + in flight*; comparing accumulated spend alone let a whole
concurrency window overshoot it (measured: six $1 images ran against a $2 ceiling).

`POST /jobs/{id}/cancel` is the manual counterpart. The flag lives under its own store key rather
than on `JobStatus`, because the worker holds that record in memory and writes it back wholesale —
a flag set on the API's copy would be overwritten within seconds.

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
URL is short-lived and unguessable (`SIGNED_URL_TTL_SECONDS`, 15 minutes).

**URLs are minted when a record is read, not when the asset was written.** A 400-image job runs
far longer than the TTL, so freezing a URL at write time meant the early images' links were dead
by the time anyone saw them. The tempting fix — raising the TTL to cover the longest plausible job
— weakens the control for every job to solve a bookkeeping problem. Instead each asset stores its
object `key` and every read path re-signs from it, so the TTL stays short and links are always
fresh. Do not store a minted URL and do not cache one past the response it arrived in.

## What is deliberately out of scope for a demo

No auth, no rate limiting per user, no CSRF protection (no cookies/sessions exist to protect), no
production TLS termination (left to the deployment environment), CORS wide open (`*`) since there
are no cookies and no authenticated sessions to leak. Tighten CORS to the real frontend origin
before this touches anything beyond the demo.

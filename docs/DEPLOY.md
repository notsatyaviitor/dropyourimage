# Deploying DropYourImage

A runbook for a single Linux server. Artefacts referenced here live in [`deploy/`](../deploy).

> **Read this first.** The application has **no authentication of its own**, and `POST /jobs`
> spends about **$0.02 per image** on a segmentation vendor. The nginx config in `deploy/` is not
> optional decoration — it is the only thing standing between an unknown caller and your vendor
> account. Do not expose port 8000 directly.

## What you need

| | Component | Notes |
|---|---|---|
| ☐ | **Linux server** | 32 GB RAM recommended — see [Sizing](#sizing), memory is the binding constraint |
| ☐ | **Object storage** | GCS bucket (private) or S3. Already integrated; see [Storage](#storage) |
| ☐ | **Redis** | Required. Without it, anything over `INLINE_JOB_MAX_IMAGES` (25) is refused |
| ☐ | **Domain + TLS cert** | Vendor keys are server-side; the browser must never reach storage or vendors directly |
| ☐ | **Photoroom API key** | The volume engine. A second key (`FAL_KEY`) enables dual-engine auto-pick |

No GPU. Every AI call is a metered API call — an architectural constraint, not a cost decision.

## 1. System packages

```bash
sudo apt-get update
sudo apt-get install -y python3-venv python3-dev build-essential libmagic1 nginx apache2-utils
```

- **`libmagic1`** — `python-magic` is only a binding. Without the system library, per-entry MIME
  sniffing on zip uploads fails, and that is a **security control**, not a nicety.
- **`build-essential` / `python3-dev`** — `pytoshop` compiles a Cython extension from sdist.
  Without them PSD jobs raise a typed `psd_unavailable`; every other format still works.
- `rawpy` needs nothing extra — the manylinux wheel bundles libraw.

Node is **build-time only**. Build the frontend anywhere and copy `dist/`.

## 2. Service user and code

```bash
sudo useradd --system --home /srv/dyi --shell /usr/sbin/nologin dyi
sudo mkdir -p /srv/dyi && sudo chown dyi:dyi /srv/dyi

sudo -u dyi git clone <your-repo> /srv/dyi
cd /srv/dyi/backend && sudo -u dyi ./scripts/setup.sh      # creates .venv, installs requirements
```

Build and place the frontend:

```bash
cd /srv/dyi/frontend && npm ci && npm run build             # -> frontend/dist
```

## 3. Configuration

```bash
sudo -u dyi cp backend/.env.example backend/.env
sudo -u dyi chmod 600 backend/.env
```

Set at minimum:

```bash
PHOTOROOM_API_KEY=…
CORS_ALLOW_ORIGINS=https://dyi.example.com      # never leave "*" on a reachable deployment
REDIS_URL=redis://localhost:6379/3              # a dedicated db index, not shared db0
QUEUE_NAME=dyi
WORKER_CONCURRENCY=4                            # see Sizing
GCP_PROJECT_ID=…
GCP_BUCKET_NAME=…
GCP_KEY_FILE=/etc/dyi/gcp-sa.json               # ABSOLUTE path
```

### The service-account key

```bash
sudo mkdir -p /etc/dyi
sudo install -o dyi -g dyi -m 600 gcp-sa.json /etc/dyi/gcp-sa.json
```

**Absolute path, always.** A relative `GCP_KEY_FILE` resolves against each process's working
directory, and the API and the worker do not necessarily share one — so it can work for one and
fail for the other, with an error that reads like bad credentials. Mode `600`, owned by `dyi`, and
**never inside the repo**.

## 4. Services

```bash
sudo cp deploy/dyi-api.service deploy/dyi-worker.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now dyi-api dyi-worker
```

**Both are required.** The API enqueues and returns `202` immediately, so a missing worker looks
like a healthy API and a UI that never finishes. Check:

```bash
curl -s localhost:8000/health | jq '.config.limits.bulk_enabled'   # must be true
```

`false` means the queue is not usable: Redis is unreachable, or `QUEUE_NAME` does not match the
queue the worker is listening on.

## 5. nginx

```bash
sudo htpasswd -c /etc/nginx/.htpasswd-dyi <username>
sudo cp deploy/nginx.conf /etc/nginx/sites-available/dyi
sudo ln -s /etc/nginx/sites-available/dyi /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
```

Edit `server_name` and the certificate paths first. The config provides the four things the
application does not: **basic auth**, **rate limiting** (tighter on `/api/jobs`, the endpoint that
costs money), **a 1.2 GB upload cap** above `MAX_ZIP_BYTES`, and **TLS**.

## Sizing

Memory is the constraint, and it scales with **megapixels, not file size**. Measured on a real
50.6 MP DNG through the full Photoroom path: **4,435 MB peak for one image**. A 51 MB DNG needs
more RAM than a 200 MB JPEG, because the pipeline holds several float32 buffers at ~600 MB each.

| Server RAM | `WORKER_CONCURRENCY` | 200 images (est.) |
|---|---|---|
| 16 GB | 2 | ~60–90 min |
| 32 GB | 4 | ~45 min |
| 64 GB | 8 | ~25 min |

`app/core/sizing.py` checks this against actual RAM at startup and **refuses to start** if the
configuration does not fit, naming a value that does. Set `ENFORCE_MEMORY_HEADROOM=false` to
downgrade it to a warning on a box where the estimate does not apply (small images only, or a
cgroup limit it cannot see).

**Do not add `rq worker` processes to go faster.** Concurrency within a job is already
`WORKER_CONCURRENCY` (asyncio, in-process); a second worker process multiplies memory again. Scale
by adding machines.

⚠️ If you run at `WORKER_CONCURRENCY=1`, raise `_SECONDS_PER_IMAGE` in `app/queue.py` first. The RQ
timeout is `image_count × 60s`; serial processing of 200 large images takes about 3.1 hours against
a 200-minute budget, so RQ kills the job at roughly two-thirds — after paying for every image and
never writing the bundle.

## Storage

Setting `GCP_BUCKET_NAME` selects GCS; otherwise S3/MinIO. `/health` reports which is live under
`config.storage`.

- **Keep the bucket private.** Assets leave only through short-lived V4 signed URLs
  (`SIGNED_URL_TTL_SECONDS`, default 900). Verify: an unsigned `GET` must return 403.
- **The bucket is not created automatically** — it has a location, storage class and billing
  attached. A missing bucket is reported as a configuration error at startup.
- **Signing needs a private key.** The default compute service account on GCE/GKE cannot sign;
  startup fails with a clear message rather than every download breaking later. Supply a key file
  or grant `roles/iam.serviceAccountTokenCreator`.
- **Nothing is ever deleted.** Source archives, outputs, previews, bundles and the cut-out cache
  all accumulate. One 200-image order at source resolution is tens of gigabytes. Add a lifecycle
  rule on `jobs/`, and keep `cutouts/` longer — that cache is what stops you re-paying the vendor.

## Cost controls

| Setting | Default | What it does |
|---|---|---|
| `MAX_JOB_COST_USD` | 25.0 | Stops one job at its ceiling, keeping what it produced. Reserves each image's cost *before* starting it |
| `MAX_JOB_IMAGES` | 500 | Bounds the accumulated job across batches |
| `CACHE_CUTOUTS` | true | Re-running a zip to change colour or size costs **$0** — masks are cached on `sha256(image)+engine` |

This caps *one* job, not how many jobs arrive. That is what the nginx rate limit is for.

## Verifying a deployment

```bash
curl -s localhost:8000/health | jq '.config | {storage, queue, bulk_enabled: .limits.bulk_enabled}'
# storage: "gcs" | queue: "dyi" | bulk_enabled: true

sudo -u dyi /srv/dyi/backend/.venv/bin/pytest -q     # 709 passed, no keys or network needed
```

Then run one real image end to end and confirm: the job reaches `completed`, the asset URL is an
absolute signed storage link, an unsigned fetch of the same object returns 403, and
`GET /objects/<key>` returns **404** (that passthrough is memory-mode only by design).

## Known limitations at launch

- **No app-level authentication.** nginx basic auth is the gate. Add real accounts when you need
  per-user access or an audit trail.
- **No job recovery in the UI.** The `job_id` lives only in React state, so reloading the tab
  loses the results page while the worker keeps running and billing.
- **Adobe PSD is enterprise-quoted**, so the pytoshop fallback is the live path. It produces a real
  layered PSD with a vector clipping path.
- **Camera raw cannot round-trip.** No encoder exists for CR3/NEF/CR2/CRW. Raw is delivered as
  16-bit TIFF with the untouched original returned alongside.
- **Out of scope by design:** glass, sheer fabric, dedicated hair and fur handling.

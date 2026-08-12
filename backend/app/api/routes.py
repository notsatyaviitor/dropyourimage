"""HTTP routes.

Two execution modes, chosen by whether Redis is configured:

* **Real deployment** (`REDIS_URL` set to an actual Redis): the job is enqueued via RQ and a
  separate `rq worker` process runs it. `POST /jobs` returns as soon as the upload is stored.
* **Memory mode** (no Redis — the default until `docker compose up` runs, and always true under
  `pytest`): there is no worker to hand off to, so the job runs inline, synchronously, inside the
  request handler. `POST /jobs` blocks until processing finishes.

`JobCreated.state` is always `QUEUED` regardless of mode — it describes acceptance of the
request, not a live status snapshot. Callers poll `GET /jobs/{id}` for the real state, which is
correct in both modes.

## Uploading in batches

A single archive does not scale to a 400-image order, and the limit is the *browser*: it must
hold every file's bytes, then the assembled zip, then the `File` — roughly three times the total —
before a single byte is sent. So `POST /jobs` accepts an existing `job_id` and appends another
archive to it.

The extension is additive and today's single-shot call is untouched: `start` defaults to true, so
one POST with a config and a zip still creates, runs and returns exactly as it did. A batching
client sends `start=false` on every batch and finishes with `POST /jobs/{id}/start`.

Each batch is screened by the full set of archive guards independently. Being the second batch of
an accepted job confers no trust — see `app/ingest/zip_reader.py`.

## Why signed URLs are minted here rather than when the asset was written

`SIGNED_URL_TTL_SECONDS` is 15 minutes and a 400-image job is not. Freezing a URL into the record
when the image finished meant the first images' links were dead before the results page rendered.
Assets carry their storage `key`, and every read path re-signs from it, so a URL is always fresh
from the response that carried it and the TTL stays short. See `_sign_assets`.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import Response
from pydantic import ValidationError

from app.api.deps import get_jobstore, get_storage
from app.core import errors
from app.core.jobstore import JobStore
from app.core.settings import Settings, get_settings
from app import samples
from app.jobs import CONFIG_KEY, run_job_async, source_key
from app.models import (
    CostEstimate,
    ImagePage,
    JobConfig,
    JobCreated,
    JobState,
    JobStatus,
    SampleFile,
    SampleList,
)
from app.storage.base import StorageBackend, job_key

router = APIRouter()


def _settings_dep() -> Settings:
    return get_settings()


@router.get("/health")
def health(settings: Settings = Depends(_settings_dep)) -> dict:
    config = settings.redacted()
    # `Settings.redacted()` can only report what is *configured*; whether Redis actually answers is
    # a live fact and has to be probed. Reporting `bulk_enabled: true` off the config alone told
    # the UI to offer 400-image uploads on a machine where the queue was down and every job would
    # have 500'd at enqueue.
    config["limits"]["bulk_enabled"] = _using_real_queue(settings)
    return {"status": "ok", "config": config}


@router.post("/jobs", response_model=JobCreated, status_code=202)
async def create_job(
    file: UploadFile | None = File(
        None,
        description="A .zip of product images. Omit only when use_samples is true.",
    ),
    config: str = Form(..., description="JobConfig as JSON"),
    use_samples: bool = Form(
        False,
        description=(
            "Build this job from the server's SAMPLES_DIR instead of an upload. The samples are "
            "processed exactly like uploaded files — same archive, same pipeline, same caps."
        ),
    ),
    sample_names: list[str] | None = Form(
        None,
        description=(
            "Which samples to use, by name, when a caller has dismissed some. Omit for all of "
            "them. Names select from the server's own listing and are never used as a path."
        ),
    ),
    job_id: str | None = Form(
        None,
        description=(
            "Append this archive to an existing job instead of creating one. Omit for the first "
            "batch; the response's job_id is what later batches pass back."
        ),
    ),
    start: bool = Form(
        True,
        description=(
            "Run the job once this archive is stored. Default true, so a single-batch upload is "
            "one call. Batching clients send false and finish with POST /jobs/{id}/start."
        ),
    ),
    settings: Settings = Depends(_settings_dep),
    storage: StorageBackend = Depends(get_storage),
    store: JobStore = Depends(get_jobstore),
) -> JobCreated:
    if job_id is None:
        job_config = _parse_config(config)
        job_id = uuid.uuid4().hex
        status = JobStatus(
            job_id=job_id,
            state=JobState.QUEUED,
            config_hash=job_config.config_hash(),
            created_at=datetime.now(timezone.utc),
        )
        storage.put(
            job_key(job_id, CONFIG_KEY),
            job_config.model_dump_json().encode("utf-8"),
            "application/json",
        )
    else:
        status = _load_draft(job_id, store)
        job_config = _stored_config(job_id, storage)

    # Samples become an ordinary archive here and nothing downstream is aware of them: same
    # `_accept_batch`, same caps, same storage key, same worker. That is deliberate — a demo that
    # takes a different code path is not a demo of this product.
    if use_samples:
        data = _sample_archive(settings, sample_names)
    else:
        if file is None:
            raise HTTPException(
                status_code=422, detail="Provide a zip file, or set use_samples=true."
            )
        data = await _read_capped(file, settings.max_zip_bytes)

    _accept_batch(status, data, storage, settings)
    store.save(status)

    if not start:
        return _created(status, job_config, accepting=True)

    return await _start(status, job_config, storage, store, settings)


@router.get("/samples", response_model=SampleList)
def get_samples(settings: Settings = Depends(_settings_dep)) -> SampleList:
    """Demo images the server can start a job from without an upload.

    Returns an empty list when `SAMPLES_DIR` is unset or missing, which is how the UI knows to hide
    the option — a demo convenience must degrade to absent, never to an error.
    """
    found = samples.list_samples(settings)
    return SampleList(
        files=[SampleFile(name=s.name, size_bytes=s.size_bytes) for s in found],
        total_bytes=sum(s.size_bytes for s in found),
    )


def _sample_archive(settings: Settings, names: list[str] | None = None) -> bytes:
    """The requested samples as an archive, or a clear 409 if there are none to give.

    A name that matches nothing is not an error on its own — `samples.resolve` drops it — but a
    request that ends up selecting *zero* files is, because the alternative is an empty job that
    completes successfully having done nothing.
    """
    try:
        chosen = samples.resolve(settings, names)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=409, detail=_NO_SAMPLES) from exc

    if not chosen:
        raise HTTPException(status_code=409, detail=_NO_SAMPLES)

    try:
        return samples.build_archive(settings, names)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=409, detail=_NO_SAMPLES) from exc


_NO_SAMPLES = "No sample images are available on this server for that request (SAMPLES_DIR)."


@router.post("/jobs/{job_id}/start", response_model=JobCreated, status_code=202)
async def start_job(
    job_id: str,
    settings: Settings = Depends(_settings_dep),
    storage: StorageBackend = Depends(get_storage),
    store: JobStore = Depends(get_jobstore),
) -> JobCreated:
    """Run a job whose batches were uploaded with `start=false`."""
    status = _load_draft(job_id, store)
    if status.batches == 0:
        raise HTTPException(status_code=400, detail="No images were uploaded for this job.")

    return await _start(status, _stored_config(job_id, storage), storage, store, settings)


@router.post("/jobs/{job_id}/cancel", response_model=JobStatus)
def cancel_job(
    job_id: str,
    storage: StorageBackend = Depends(get_storage),
    store: JobStore = Depends(get_jobstore),
    settings: Settings = Depends(_settings_dep),
) -> JobStatus:
    """Ask a running job to stop after its in-flight images.

    Returns immediately with the current status; the state does not flip to CANCELLED here. The
    worker owns that transition, and claiming it up front would report a stop that has not
    happened while images are still being billed. Poll for the real state.

    Images already finished stay downloadable — a cancel is "stop spending", not "throw away what
    I paid for". Idempotent, and accepted on a terminal job so a racing click is not an error.
    """
    status = store.load(job_id)
    if status is None:
        raise HTTPException(status_code=404, detail=f"No job with id {job_id!r}.")

    store.request_cancel(job_id)
    return _prepare(status, storage, settings)


@router.post("/jobs/estimate", response_model=CostEstimate)
def estimate_job(
    config: str = Form(..., description="JobConfig as JSON"),
    images: int = Form(..., ge=0, description="How many images the batch contains"),
    settings: Settings = Depends(_settings_dep),
) -> CostEstimate:
    """What a batch of this size would cost on the engines this config selects.

    Every figure is an estimate built from the per-engine list prices already recorded in
    `app/engines/`, and `CostEstimate` says so in its own docstring. It exists so a 400-image
    click is an informed one — not as a quote. See the pricing note in the upload step's UI.
    """
    from app.engines import registry

    job_config = _parse_config(config)
    pool = registry.select_pool(settings, job_config.cutout)

    per_image = round(sum(e.cost_per_image_usd for e in pool), 6)
    estimated = round(per_image * images, 4)
    ceiling = settings.max_job_cost_usd

    return CostEstimate(
        images=images,
        engines=[e.id for e in pool],
        cost_per_image_usd=per_image,
        estimated_cost_usd=estimated,
        per_object_pricing=job_config.cutout.multi_object,
        ceiling_usd=ceiling,
        exceeds_ceiling=ceiling > 0 and estimated >= ceiling,
    )


@router.get("/jobs/{job_id}", response_model=JobStatus)
def get_job(
    job_id: str,
    include_images: bool = Query(
        True,
        description=(
            "Set false while polling a large job. The per-image records are the bulk of this "
            "response and a 400-image job polled every 1.2s moves megabytes per second of data "
            "the progress bar does not read. Counts stay accurate either way; fetch the records "
            "from GET /jobs/{id}/images when the job finishes."
        ),
    ),
    storage: StorageBackend = Depends(get_storage),
    store: JobStore = Depends(get_jobstore),
    settings: Settings = Depends(_settings_dep),
) -> JobStatus:
    status = store.load(job_id)
    if status is None:
        raise HTTPException(status_code=404, detail=f"No job with id {job_id!r}.")

    status = _prepare(status, storage, settings)
    if not include_images:
        # images_total is set by _prepare before this, so the client can still see how many
        # records exist without receiving them.
        status.images = []
    return status


@router.get("/jobs/{job_id}/images", response_model=ImagePage)
def get_job_images(
    job_id: str,
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    storage: StorageBackend = Depends(get_storage),
    store: JobStore = Depends(get_jobstore),
    settings: Settings = Depends(_settings_dep),
) -> ImagePage:
    """One page of per-image results, with fresh signed URLs for that page only."""
    status = store.load(job_id)
    if status is None:
        raise HTTPException(status_code=404, detail=f"No job with id {job_id!r}.")

    window = status.images[offset : offset + limit]
    for image in window:
        _sign_assets(image, storage, settings)

    return ImagePage(
        job_id=job_id,
        offset=offset,
        limit=limit,
        total=len(status.images),
        images=window,
    )


@router.get("/objects/{object_key:path}")
def get_object(
    object_key: str,
    storage: StorageBackend = Depends(get_storage),
    settings: Settings = Depends(_settings_dep),
) -> Response:
    """Passthrough for memory-mode storage only.

    **Gated deliberately.** This used to serve from whatever backend was configured, on the
    assumption — stated in this docstring, enforced nowhere — that a real deployment would only
    ever follow presigned URLs. It does not work that way: the route stayed reachable, so
    `GET /objects/jobs/<id>/out/x.png` returned any object in the live bucket with no signature,
    no expiry and no auth, defeating the entire signed-URL model that `docs/SECURITY.md` relies on
    (verified against a real GCS bucket: HTTP 200 with the object body).

    Keys are not secret enough to be the control. A job id appears in every client URL, and every
    other key is derived from it by a documented rule (`job_key`, `output_key`, `source_key`), so
    one leaked id would expose that job's uploads and outputs permanently.

    In S3/GCS mode the API hands out short-lived signed URLs instead, which is the only intended
    way an asset leaves storage.
    """
    if settings.storage_backend_name != "memory":
        raise HTTPException(
            status_code=404,
            detail="Not found.",  # deliberately not "disabled": do not confirm the key exists
        )

    try:
        data = storage.get(object_key)
    except KeyError:
        raise HTTPException(status_code=404, detail="Not found.") from None

    content_type = getattr(storage, "content_type", lambda _k: "application/octet-stream")(
        object_key
    )
    # Mirrors what S3/GCS serve from the stored object, so a bundle downloads under the same
    # `AI_<source>.zip` name in memory mode as it does in a real deployment.
    disposition = getattr(storage, "content_disposition", lambda _k: None)(object_key)
    headers = {"Content-Disposition": disposition} if disposition else None
    return Response(content=data, media_type=content_type, headers=headers)


# ---------------------------------------------------------------------------
# Job creation helpers
# ---------------------------------------------------------------------------


def _parse_config(config: str) -> JobConfig:
    try:
        return JobConfig.model_validate_json(config)
    except ValidationError as exc:
        # exc.errors() can carry a raw exception object under "ctx" (e.g. the ValueError from a
        # custom validator like BackgroundSpec's hex check), which json.dumps cannot serialise.
        # Drop "ctx" per-error rather than relying on jsonable_encoder to reach inside a list.
        detail = [{k: v for k, v in e.items() if k != "ctx"} for e in exc.errors(include_url=False)]
        raise HTTPException(status_code=422, detail=detail) from exc


def _stored_config(job_id: str, storage: StorageBackend) -> JobConfig:
    """Re-read the config stored with the first batch.

    Later batches do not get to change it. One job is one `JobConfig` (see `frontend/CLAUDE.md`),
    and letting batch 5 alter the background colour would deliver an order whose images disagree
    with each other — with no record of which setting produced which file.
    """
    try:
        raw = storage.get(job_key(job_id, CONFIG_KEY))
    except KeyError:
        raise HTTPException(status_code=404, detail=f"No job with id {job_id!r}.") from None
    return JobConfig.model_validate_json(raw.decode("utf-8"))


def _load_draft(job_id: str, store: JobStore) -> JobStatus:
    """Load a job that is still accepting batches, or explain why it is not."""
    status = store.load(job_id)
    if status is None:
        raise HTTPException(status_code=404, detail=f"No job with id {job_id!r}.")
    if status.state is not JobState.QUEUED:
        raise HTTPException(
            status_code=409,
            detail=f"Job {job_id!r} has already started; it cannot accept more images.",
        )
    return status


def _accept_batch(
    status: JobStatus, data: bytes, storage: StorageBackend, settings: Settings
) -> None:
    """Store one archive against a job, enforcing the job-level caps.

    The per-archive guards in `zip_reader` bound a single upload. Without a job-level cap on top,
    N uploads against one job id multiply straight through every one of them — which is exactly
    what the batching client does, legitimately, so the cap has to live at this layer.

    Counting entries here means parsing the archive index a second time (the worker does it again
    when planning). That is cheap, index-only work, and the alternative is discovering at
    processing time that a job is over its limit — after every byte has been uploaded and stored.
    """
    count = _count_entries(data)

    if status.images_total + count > settings.max_job_images:
        raise HTTPException(
            status_code=413,
            detail=errors.BatchTooLarge(
                f"This job would hold {status.images_total + count} images; the limit is "
                f"{settings.max_job_images}. Split it into separate orders."
            ).message,
        )

    if status.upload_bytes + len(data) > settings.max_job_upload_bytes:
        raise HTTPException(
            status_code=413,
            detail=errors.BatchTooLarge(
                f"This job would hold more than the "
                f"{settings.max_job_upload_bytes // 1_048_576} MB total upload limit."
            ).message,
        )

    storage.put(job_key(status.job_id, source_key(status.batches)), data, "application/zip")
    status.batches += 1
    status.upload_bytes += len(data)
    # Reused as the running count of accepted images while the job is a draft; the worker
    # overwrites it with the real record count once results exist.
    status.images_total += count
    status.total = status.images_total
    status.accepting_uploads = True


def _count_entries(data: bytes) -> int:
    """How many non-junk entries an archive holds, from its index alone.

    An unreadable archive counts as zero rather than raising. The contract is explicit that a bad
    archive produces a job in the FAILED state, not a rejected upload
    (`docs/API_CONTRACT.md`) — the client gets a job id to poll and a typed `malicious_archive`
    error, in the same shape as every other failure. Rejecting it here instead would make one
    failure mode arrive as a bare HTTP status while all its neighbours arrive as job records.

    The count is only used for the job-level caps, and zero is the safe direction to be wrong in:
    the worker re-screens the archive properly and fails it there.
    """
    from app.ingest.zip_reader import iter_names

    try:
        return sum(1 for _ in iter_names(data))
    except Exception:
        return 0


async def _start(
    status: JobStatus,
    job_config: JobConfig,
    storage: StorageBackend,
    store: JobStore,
    settings: Settings,
) -> JobCreated:
    """Hand the job to a worker, or run it inline if it is small enough to."""
    status.accepting_uploads = False
    store.save(status)

    if _using_real_queue(settings):
        from app.queue import enqueue_job

        enqueue_job(status.job_id, settings, image_count=status.images_total)
        return _created(status, job_config, accepting=False)

    # No worker to hand off to — see the module docstring. Inline processing blocks the request
    # for the whole job, which is fine for a handful of images and a timeout for hundreds, so it
    # is refused rather than attempted.
    if status.images_total > settings.inline_job_max_images:
        configured = settings.redis_url not in ("", "memory")
        why = (
            "the queue at REDIS_URL is not reachable"
            if configured
            else "this server has no queue configured"
        )
        raise HTTPException(
            status_code=503,
            detail=errors.BulkRequiresWorker(
                f"This job has {status.images_total} images and {why}, so it can only process "
                f"{settings.inline_job_max_images} at a time. Start Redis and an `rq worker` "
                "(see docs/SETUP.md), or upload fewer images."
            ).message,
        )

    # Errors inside are not re-raised: a failure becomes a FAILED job status, which
    # GET /jobs/{id} reports, rather than a 500 for a request that was itself perfectly valid.
    await run_job_async(status.job_id, storage, store, settings)
    finished = store.load(status.job_id)
    if finished is not None:
        status = finished
    return _created(status, job_config, accepting=False)


def _created(status: JobStatus, job_config: JobConfig, *, accepting: bool) -> JobCreated:
    return JobCreated(
        job_id=status.job_id,
        total=status.total,
        config_hash=job_config.config_hash(),
        batches=status.batches,
        accepting_uploads=accepting,
    )


def _using_real_queue(settings: Settings) -> bool:
    """Whether there is a worker to hand this job to.

    Reachability, not configuration. `REDIS_URL` defaults to `redis://localhost:6379`, so every
    developer is "configured" from the first run — including before `docker compose up`. Treating
    that as a queue enqueued into nothing and returned a 500 for a request that was perfectly
    valid; a small job should simply run inline instead.
    """
    if settings.redis_url in ("", "memory"):
        return False

    from app.queue import queue_available

    return queue_available(settings)


# ---------------------------------------------------------------------------
# Read-time URL signing
# ---------------------------------------------------------------------------


def _prepare(status: JobStatus, storage: StorageBackend, settings: Settings) -> JobStatus:
    """Fill in fresh signed URLs and the record count before a status goes out."""
    status.images_total = len(status.images)
    for image in status.images:
        _sign_assets(image, storage, settings)

    if status.bundle_key:
        status.bundle_url = storage.signed_url(status.bundle_key, settings.signed_url_ttl_seconds)

    return status


def _sign_assets(image, storage: StorageBackend, settings: Settings) -> None:
    """Mint a URL for every asset that carries a key.

    Records written before `OutputAsset.key` existed have an empty key and keep whatever URL they
    were stored with, rather than losing their link to a migration.
    """
    for asset in image.outputs:
        if asset.key:
            asset.url = storage.signed_url(asset.key, settings.signed_url_ttl_seconds)

    # The "before" thumbnail is signed on the same terms. It lives outside `outputs` deliberately:
    # it is not a deliverable, and putting it there would land it in the download bundle.
    if image.source_preview_key:
        image.source_preview_url = storage.signed_url(
            image.source_preview_key, settings.signed_url_ttl_seconds
        )


async def _read_capped(file: UploadFile, limit: int) -> bytes:
    """Read an upload with a hard byte cap, independent of any Content-Length header.

    A client can lie about Content-Length, so the cap is enforced against bytes actually read —
    the same principle as the zip-bomb guards in `zip_reader.py`.

    `limit` of 0 means no cap, matching every other byte setting.
    """
    chunks: list[bytes] = []
    total = 0
    chunk_size = 1024 * 1024

    while True:
        chunk = await file.read(chunk_size)
        if not chunk:
            break
        total += len(chunk)
        if limit and total > limit:
            raise HTTPException(
                status_code=413,
                detail=f"Upload exceeds the {limit // 1_048_576} MB limit.",
            )
        chunks.append(chunk)

    return b"".join(chunks)

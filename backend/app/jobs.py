"""Job orchestration: unpack the uploaded archives, run every image through the pipeline, store
the outputs.

The RQ worker entrypoint is `run_job`, which takes only a job id — everything else is read back
from storage. That keeps the queue payload tiny and means a retried job re-reads the same inputs
rather than depending on whatever was serialised at enqueue time.

Progress is written back during a batch so the UI advances instead of jumping from 0% to 100%.

## What changes at 400 images

The single-archive, everything-in-memory, save-after-every-image shape was right for the 20-image
case it was written for and fails in four separate ways at bulk scale. Each fix is marked at its
site; collected here because they only make sense together:

1. **A job is now N archives, processed one at a time.** A browser cannot build one 2 GB zip
   without dying, so it sends batches (`app/api/routes.py`). Holding them all open would put the
   problem back on this side of the wire, so exactly one archive is resident at a time.
2. **Entry bytes are read when the image's turn comes, not upfront.** See `zip_reader`'s
   `plan_entries`/`read_entry`. Resident cost is one image, not the whole archive.
3. **Status writes are coalesced.** Rewriting a growing record once per image is quadratic in the
   image count; at 400 images with candidates and notes on each, the record is megabytes and it
   was being re-serialised 400 times.
4. **The job can stop.** Cancellation, and a spend ceiling. A 400-image run on the dual-engine
   pool is real money, and before this there was no way to halt one mid-flight.
"""

from __future__ import annotations

import asyncio
import contextlib
import tempfile
import time
import zipfile
from datetime import datetime, timezone

from app import pipeline
from app.core import errors
from app.core.jobstore import JobStore
from app.core.settings import Settings, get_settings
from app.engines import registry, throttle
from app.engines.cache import StorageCutoutCache
from app.ingest import zip_reader
from app.models import (
    ErrorInfo,
    ImageResult,
    ImageState,
    JobConfig,
    JobState,
    JobStatus,
    OutputFormat,
)
from app.storage.base import StorageBackend, content_type_for, job_key

CONFIG_KEY = "config.json"
BUNDLE_KEY = "outputs.zip"

# The first archive keeps the original name so a job uploaded before chunked upload existed, or
# by any client that sends one archive, is found unchanged.
SOURCE_KEY = "source.zip"


def source_key(index: int) -> str:
    """Key for upload batch `index`. Batch 0 is `source.zip`; later ones are numbered."""
    return SOURCE_KEY if index == 0 else f"source-{index:03d}.zip"


# --- write coalescing ---------------------------------------------------------------------
#
# Save when either threshold trips. The count bound keeps a fast run (cache hits, small images)
# from writing on every image; the time bound keeps a slow one (large images, a throttled vendor)
# from going quiet for minutes and looking hung. Terminal transitions always force a save, so
# neither bound can lose the final state.
_SAVE_EVERY_N_IMAGES = 10
_SAVE_EVERY_SECONDS = 2.0


def _now() -> datetime:
    return datetime.now(timezone.utc)


def output_key(job_id: str, source_name: str, fmt: OutputFormat) -> str:
    stem = source_name.rsplit(".", 1)[0] or source_name
    return job_key(job_id, "out", f"{stem}.{fmt.value}")


class _ProgressWriter:
    """Coalesces `store.save` calls so a long job does not rewrite its whole record per image."""

    def __init__(self, store: JobStore, status: JobStatus) -> None:
        self._store = store
        self._status = status
        self._pending = 0
        self._last = 0.0

    def touch(self) -> None:
        """Record a change; write it out only if a threshold has been reached."""
        self._pending += 1
        elapsed = time.monotonic() - self._last
        if self._pending >= _SAVE_EVERY_N_IMAGES or elapsed >= _SAVE_EVERY_SECONDS:
            self.flush()

    def flush(self) -> None:
        self._status.images_total = len(self._status.images)
        self._store.save(self._status)
        self._pending = 0
        self._last = time.monotonic()


async def run_job_async(
    job_id: str,
    storage: StorageBackend,
    store: JobStore,
    settings: Settings | None = None,
) -> JobStatus:
    """Process one job to completion. Returns the final status."""
    settings = settings or get_settings()

    status = store.load(job_id)
    if status is None:
        raise errors.PipelineError(f"job {job_id} not found")

    status.state = JobState.RUNNING
    status.started_at = _now()
    status.accepting_uploads = False
    store.save(status)

    try:
        config = JobConfig.model_validate_json(
            storage.get(job_key(job_id, CONFIG_KEY)).decode("utf-8")
        )
        batches = _batch_keys(job_id, status, storage)
        plans, rejections = _plan_all(batches, storage, settings)
    except errors.PipelineError as exc:
        return _fail_job(status, store, exc)
    except Exception:
        return _fail_job(status, store, errors.PipelineError("The archive could not be read."))

    # Rejected entries are reported, not dropped: "17 of 20 processed" with no explanation is
    # indistinguishable from a bug.
    #
    # The rejection's OWN code is used, not a blanket `unsupported_file`. Flattening them made a
    # 130 MB PSD — a fully supported format, merely over the byte cap — read as "not a supported
    # image file", which sends someone looking for a format problem that does not exist. The
    # reason string survives too; the UI shows it beneath the code's copy.
    status.images = [
        ImageResult(
            source_name=r.name,
            state=ImageState.SKIPPED,
            error=ErrorInfo(code=r.code, message=r.reason, retryable=False),
        )
        for r in rejections
    ]
    status.total = sum(len(p.entries) for p in plans) + len(rejections)
    status.failed = len(rejections)
    store.save(status)

    engines = registry.select_pool(settings, config.cutout)
    # Shared across every image in the job and across jobs: cache entries are namespaced under
    # `cutouts/`, not under this job id, so re-running the same zip with a different colour or
    # canvas costs nothing. See app/engines/cache.py.
    cutout_cache = StorageCutoutCache(storage)
    progress = _ProgressWriter(store, status)

    # One governor per vendor, installed for the whole job. Every vendor call underneath — in any
    # engine, through any number of frames — passes through it. See app/engines/throttle.py.
    job_throttle = throttle.JobThrottle(ceiling=max(1, settings.worker_concurrency))
    # Both engines in an AUTO pair are billed whether or not their mask wins, so the projection is
    # the sum over the pool, not the winner's price.
    stop = _StopSignal(
        job_id, store, settings,
        projected_per_image=sum(e.cost_per_image_usd for e in engines),
    )

    with throttle.use(job_throttle):
        for plan in plans:
            if stop.tripped(status):
                break
            await _run_batch(
                plan, job_id, config, settings, storage, store, status,
                engines, cutout_cache, progress, stop,
            )

    status.rate_limit_events = job_throttle.rate_limit_events()
    _record_unprocessed(status, plans, stop)
    _finalise(status, job_id, config, storage, store, settings, progress, stop)
    return status


def _fail_job(status: JobStatus, store: JobStore, exc: errors.PipelineError) -> JobStatus:
    status.state = JobState.FAILED
    status.error = exc.to_info()
    status.finished_at = _now()
    status.accepting_uploads = False
    store.save(status)
    return status


class _BatchPlan:
    """One upload archive, with its usable entries identified but not yet read."""

    def __init__(self, key: str, entries: list[zip_reader.PlannedEntry]) -> None:
        self.key = key
        self.entries = entries


def _batch_keys(job_id: str, status: JobStatus, storage: StorageBackend) -> list[str]:
    """Every archive belonging to this job, in upload order.

    `status.batches` is authoritative, but falls back to probing for `source.zip` so a job record
    written before chunked upload existed still runs.
    """
    count = max(1, status.batches)
    keys = [job_key(job_id, source_key(i)) for i in range(count)]
    return [k for k in keys if storage.exists(k)]


def _plan_all(
    keys: list[str], storage: StorageBackend, settings: Settings
) -> tuple[list[_BatchPlan], list[zip_reader.ZipRejection]]:
    """Screen every archive up front, without retaining any entry's bytes.

    Two passes over the archives rather than one is a deliberate trade. The alternative — plan and
    process each archive in turn — would mean `status.total` climbing as the job ran, so the
    progress bar would move backwards whenever a new batch was opened. An honest total from the
    first poll is worth re-reading some already-compressed bytes.
    """
    plans: list[_BatchPlan] = []
    rejections: list[zip_reader.ZipRejection] = []

    for key in keys:
        data = storage.get(key)
        with zip_reader.open_archive(data, settings) as archive:
            entries, batch_rejections = zip_reader.plan_entries(archive, settings)
        plans.append(_BatchPlan(key, entries))
        rejections.extend(batch_rejections)
        del data

    return plans, rejections


class _StopSignal:
    """Why a job should stop early: cancelled by an operator, or out of budget.

    Checked between images rather than mid-image. An image already in flight has been billed, so
    abandoning it would spend the money and throw away the result.

    ## The budget counts money in flight, not just money spent

    Comparing `status.cost_usd` against the ceiling on its own does not bind: `cost_usd` is only
    updated when an image *finishes*, and up to `WORKER_CONCURRENCY` images start before the first
    one does. With a concurrency of 8 that is an eight-image overshoot — and on the multi-object
    path, where one image is one call per object, considerably more than eight images' worth of
    money. Measured: six $1 images against a $2 ceiling all ran, for $6.

    So each image reserves its projected cost before starting and releases it once the real cost
    has landed. The ceiling then binds on `spent + in flight + this one`.

    It remains approximate in one direction that cannot be fixed here: `multi_object` bills per
    object, and the object count is not known until Gemini has enumerated the scene. The
    projection is one image's pool cost, so a scene-heavy job can still overshoot. That is
    reported honestly in `CostEstimate.per_object_pricing` rather than papered over.
    """

    # The cancel flag lives in Redis and `tripped` is called once per image, so the read is
    # rate-limited. A cancel taking up to this long to be noticed is invisible next to a
    # multi-second vendor call, and it keeps a 400-image job from adding 400 round trips.
    _POLL_SECONDS = 0.5

    def __init__(
        self,
        job_id: str,
        store: JobStore,
        settings: Settings,
        projected_per_image: float = 0.0,
    ) -> None:
        self._job_id = job_id
        self._store = store
        self._ceiling = settings.max_job_cost_usd
        self._projected = max(0.0, projected_per_image)
        self._reserved = 0.0
        self._checked_at = 0.0
        self.reason: errors.PipelineError | None = None

    @contextlib.contextmanager
    def reservation(self):
        """Hold this image's projected cost against the ceiling for as long as it runs."""
        self._reserved += self._projected
        try:
            yield
        finally:
            self._reserved = max(0.0, self._reserved - self._projected)

    def tripped(self, status: JobStatus) -> bool:
        if self.reason is not None:
            return True

        # Local state, so it costs nothing to check every time — and it must be checked every
        # time, since it is what stops the spend.
        if self._ceiling > 0:
            committed = status.cost_usd + self._reserved + self._projected
            if committed > self._ceiling:
                self.reason = errors.BudgetExceeded(
                    f"This job reached its ${self._ceiling:.2f} vendor-spend ceiling and stopped. "
                    "Images already finished are still available."
                )
                return True

        now = time.monotonic()
        if now - self._checked_at < self._POLL_SECONDS:
            return False
        self._checked_at = now

        # Read from the store, never from `status` — the worker owns `status` and overwrites it
        # wholesale, so a flag set there by the API would not survive. See app/core/jobstore.py.
        try:
            requested = self._store.is_cancel_requested(self._job_id)
        except Exception:
            # A store hiccup must not look like a cancellation; carry on and re-check next time.
            return False

        if requested:
            self.reason = errors.JobCancelled(
                "Cancelled before this image started. Images already finished are still available."
            )
            return True

        return False


async def _run_batch(
    plan: _BatchPlan,
    job_id: str,
    config: JobConfig,
    settings: Settings,
    storage: StorageBackend,
    store: JobStore,
    status: JobStatus,
    engines: list,
    cutout_cache: StorageCutoutCache,
    progress: _ProgressWriter,
    stop: _StopSignal,
) -> None:
    """Process one archive's images, holding only that archive open.

    The archive stays open for the batch so its central directory is parsed once; each entry's
    bytes are read inside `handle`, used, and dropped. Peak memory is therefore one compressed
    archive plus `worker_concurrency` decoded images — not the whole job.
    """
    archive_bytes = storage.get(plan.key)
    semaphore = asyncio.Semaphore(max(1, settings.worker_concurrency))
    lock = asyncio.Lock()

    with zip_reader.open_archive(archive_bytes, settings) as archive:

        async def handle(item: zip_reader.PlannedEntry) -> None:
            async with semaphore:
                # Re-checked inside the slot, not just before the gather: a 400-image batch queues
                # every coroutine at once, so without this a cancellation would still let all 400
                # run through the semaphore one by one. `tripped` and `reservation` are both
                # synchronous, so no other image can slip between them.
                if stop.tripped(status):
                    return

                with stop.reservation():
                    try:
                        data = zip_reader.read_entry(archive, item)
                    except Exception as exc:
                        async with lock:
                            _record_failure(status, item.name, exc, progress)
                        return

                    result, outputs = await pipeline.process_image(
                        data, item.name, config, settings, engines=engines, cache=cutout_cache
                    )
                    _attach_source_preview(result, data, job_id, storage, settings)
                    del data

                    assets = []
                    for fmt, blob in outputs.items():
                        key = output_key(job_id, item.name, fmt)
                        storage.put(key, blob, content_type_for(f"x.{fmt.value}"))
                        assets.append((fmt, key, blob))

                    # Serialised so concurrent images cannot interleave a read-modify-write on
                    # the record. Inside the reservation so the real cost lands before the
                    # projection is released — otherwise there is a window in which this image's
                    # spend is counted by neither.
                    async with lock:
                        _attach_outputs(result, assets, config.export.profile)
                        status.images.append(result)
                        if result.state is ImageState.DONE:
                            status.completed += 1
                        else:
                            status.failed += 1
                        status.cost_usd = round(status.cost_usd + result.cost_usd, 6)
                        progress.touch()

        outcomes = await asyncio.gather(
            *(handle(e) for e in plan.entries), return_exceptions=True
        )

        # `return_exceptions=True` used to drop these on the floor, so an image whose handler
        # raised — a storage write failing, say — silently never appeared in `status.images` and
        # `completed + failed` quietly stopped matching `total`. At 20 images that is a rare
        # puzzle; at 400 it is a routine one. Every entry now lands somewhere.
        async with lock:
            recorded = {i.source_name for i in status.images}
            for item, outcome in zip(plan.entries, outcomes):
                if isinstance(outcome, BaseException) and item.name not in recorded:
                    _record_failure(status, item.name, outcome, progress)

    del archive_bytes
    progress.flush()


def _record_failure(
    status: JobStatus, name: str, exc: BaseException, progress: _ProgressWriter
) -> None:
    """Land a per-image failure on the record, whatever raised it."""
    if isinstance(exc, errors.PipelineError):
        info = exc.to_info()
    else:
        # Unclassified means our bug, not the user's file. Detail stays out of the response.
        info = errors.PipelineError("An unexpected error occurred processing this image.").to_info()

    status.images.append(ImageResult(source_name=name, state=ImageState.FAILED, error=info))
    status.failed += 1
    progress.touch()


def _record_unprocessed(
    status: JobStatus, plans: list["_BatchPlan"], stop: _StopSignal
) -> None:
    """Account for images the job never reached, **by name**, and say why.

    Leaving them off the record entirely would make `completed + failed` disagree with `total`,
    which reads as a lost image rather than a deliberate stop. Naming them matters as much as
    counting them: after a cancel or a budget stop the operator's next question is "which ones do
    I still need?", and "37 images were skipped" does not answer it.
    """
    if stop.reason is None:
        return

    seen = {i.source_name for i in status.images}
    info = stop.reason.to_info()

    for plan in plans:
        for item in plan.entries:
            if item.name in seen:
                continue
            status.images.append(
                ImageResult(source_name=item.name, state=ImageState.SKIPPED, error=info)
            )
            status.failed += 1
            seen.add(item.name)


def _attach_source_preview(
    result: ImageResult,
    data: bytes,
    job_id: str,
    storage: StorageBackend,
    settings: Settings,
) -> None:
    """Store a small PNG of the uploaded file, when the browser cannot paint the original.

    The results page shows the user's source beside the processed output, and put the raw upload
    straight into an `<img>` — which is blank for EPS, PSD, TIFF and camera raw. That is the same
    fact `preview_format` already handles for delivered outputs, never applied to the input side,
    and it made the before/after comparison useless for this client's entire PSD library.

    Skipped entirely for formats a browser renders natively; those cost nothing because the client
    already holds the file. Failure is swallowed: a missing "before" thumbnail must never turn a
    successfully processed image into a failed one.
    """
    from app.imaging import export as E
    from app.imaging import formats as F

    if result.source_format is None or F.is_browser_renderable_source(result.source_format):
        return

    try:
        thumbnail = E.source_thumbnail(
            data, max_pixels=settings.max_image_pixels, source=result.source_format
        )
    except Exception:
        return

    key = job_key(job_id, "src", f"{result.source_name.rsplit('.', 1)[0] or result.source_name}.png")
    storage.put(key, thumbnail, "image/png")
    result.source_preview_key = key


def _attach_outputs(
    result: ImageResult,
    assets: list[tuple[OutputFormat, str, bytes]],
    profile,
) -> None:
    """Record each stored asset on the result: its key, dimensions, size and colour profile.

    Every raster output in a job shares one profile (`JobConfig.export.profile`), so it is passed
    straight through rather than re-derived per asset.

    **No URL is minted here.** A signed URL created when the image finished would already have
    expired by the time a long job's results page rendered — the TTL is 15 minutes and a
    400-image job is not. `url` is filled in at read time from `key`; see
    `app/api/routes.py::_sign_assets`.
    """
    from app.imaging import export as E
    from app.models import OutputAsset

    for fmt, key, blob in assets:
        width, height = E.dimensions_of(blob)
        result.outputs.append(
            OutputAsset(
                format=fmt,
                url="",
                key=key,
                width=width,
                height=height,
                bytes=len(blob),
                profile=profile,
            )
        )


def _finalise(
    status: JobStatus,
    job_id: str,
    config: JobConfig,
    storage: StorageBackend,
    store: JobStore,
    settings: Settings,
    progress: _ProgressWriter,
    stop: _StopSignal,
) -> None:
    """Build the download bundle and mark the job complete.

    COMPLETED means "every image reached a terminal state", not "every image succeeded" — per-image
    failures are visible in the results grid. A job only FAILS as a whole when nothing could be
    processed at all.
    """
    done = [i for i in status.images if i.state is ImageState.DONE]

    if done:
        key = job_key(job_id, BUNDLE_KEY)
        _write_bundle(done, job_id, key, storage, status, config, settings)
        status.bundle_key = key
        status.bundle_url = storage.signed_url(key, settings.signed_url_ttl_seconds)

    stopped_early = isinstance(stop.reason, errors.JobCancelled)

    if done:
        # Partial results still count as COMPLETED even after a cancel or a budget stop: the
        # assets exist and are downloadable, and the per-image SKIPPED records carry the reason.
        # Reporting the whole job as CANCELLED would imply nothing came back, and the operator
        # would not go looking for the images that did.
        status.state = JobState.COMPLETED
    elif stopped_early:
        status.state = JobState.CANCELLED
        if status.error is None:
            status.error = errors.JobCancelled("Cancelled before any image finished.").to_info()
    else:
        status.state = JobState.FAILED
        if status.error is None:
            status.error = (
                stop.reason or errors.PipelineError("No image in the archive could be processed.")
            ).to_info()

    status.finished_at = _now()
    status.accepting_uploads = False
    progress.flush()


#: Prefix on the delivered zip, so a processed order is never mistaken for the source folder.
BUNDLE_PREFIX = "AI_"


def bundle_filename(done: list[ImageResult], created_at: datetime | None = None) -> str:
    """What the browser should save the download as.

    Named after the order rather than left as `outputs.zip`, which every job arrived as and which
    is indistinguishable in a downloads folder the moment a second order lands.

    **One image gives its own name; several do not.** A batch used to take the first image's name,
    so a four-product order downloaded as `AI_Avenafyt 100ml 2.zip` — a file that names one product
    and silently hides the other three. That is worse than a generic name, because it reads as
    correct. A multi-image order is identified by its size and the time it was placed:

        1 image    ->  AI_Avenafyt 100ml 2.zip
        4 images   ->  AI_4-files_2026-08-12_1443.zip

    The timestamp comes from the job's own `created_at`, not from the clock at packaging time, so
    re-downloading an order always yields the same filename. Minutes are included because a client
    placing several orders in a day is the normal case, and a date alone would collide.

    Sanitised because this ends up in a `Content-Disposition` header, where a quote or a newline
    is a header-injection bug rather than a cosmetic one, and a path separator would suggest a
    directory that does not exist.
    """
    if len(done) == 1:
        name = done[0].source_name
        stem = name.rsplit(".", 1)[0] or name
        safe = "".join(c for c in stem if c.isprintable() and c not in '"\\/:*?<>|\r\n').strip()
        return f"{BUNDLE_PREFIX}{safe or 'order'}.zip"

    stamp = (created_at or _now()).strftime("%Y-%m-%d_%H%M")
    return f"{BUNDLE_PREFIX}{len(done)}-files_{stamp}.zip"


def _content_disposition(filename: str) -> str:
    """RFC 6266 header value, with a UTF-8 fallback for non-ASCII names.

    Both forms are sent: `filename` for anything old, `filename*` for the real name. A client
    filename can carry accents or CJK, and a bare `filename=` is ASCII-only — so without the
    starred form those orders download under a mangled name.
    """
    from urllib.parse import quote

    ascii_name = filename.encode("ascii", "replace").decode("ascii")
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(filename)}"


def _write_bundle(
    done: list[ImageResult],
    job_id: str,
    key: str,
    storage: StorageBackend,
    status: JobStatus,
    config: JobConfig,
    settings: Settings,
) -> None:
    """Assemble the download zip through a temp file rather than in memory.

    A 400-image job with two formats each is a multi-gigabyte bundle. Built in a `BytesIO` that
    is a multi-gigabyte `bytes` object in the worker, on top of everything else the worker is
    holding — and it is entirely avoidable, since the zip is written once and read once.

    `storage.put` still takes bytes, so the final read is one copy; that is the `StorageBackend`
    Protocol's shape and changing it is a bigger change than this one. The saving is the assembly,
    which is where the peak was.
    """
    with tempfile.TemporaryFile() as scratch:
        with zipfile.ZipFile(scratch, "w", zipfile.ZIP_DEFLATED) as bundle:
            for image in done:
                for asset in image.outputs:
                    # The browser preview exists only so the results grid has something to paint
                    # (TIFF/EPS/PSD render as an empty card). Nobody asked for it, so shipping it
                    # in the download would put an unrequested PNG next to every deliverable.
                    if asset.format is image.preview_format:
                        continue
                    stem = image.source_name.rsplit(".", 1)[0] or image.source_name
                    name = f"{stem}.{asset.format.value}"
                    try:
                        bundle.writestr(
                            name, storage.get(output_key(job_id, image.source_name, asset.format))
                        )
                    except KeyError:
                        continue

            _add_unwritable_originals(bundle, done, job_id, storage, status, config, settings)

        scratch.seek(0)
        # Streamed, NOT `storage.put(key, scratch.read(), ...)`. The temp file above exists so a
        # multi-gigabyte bundle is never a `bytes` object, and reading it back here threw that
        # away at the last step. Measured against this session's own assets: 200 images at source
        # resolution is ~45 GB of TIFFs plus returned originals — an OOM on a job that had already
        # succeeded and paid for every image.
        storage.put_stream(
            key,
            scratch,
            "application/zip",
            content_disposition=_content_disposition(bundle_filename(done, status.created_at)),
        )


def _add_unwritable_originals(
    bundle: zipfile.ZipFile,
    done: list[ImageResult],
    job_id: str,
    storage: StorageBackend,
    status: JobStatus,
    config: JobConfig,
    settings: Settings,
) -> None:
    """Put the untouched upload in the bundle for sources whose format cannot be written.

    Camera raw (CRW/CR2/CR3/DNG/NEF/RAW) has no encoder in any library, so `match_source` can only
    ever substitute 16-bit TIFF for it — which reads as "my NEF never came back". Shipping the
    original alongside means the delivered folder does contain the `.nef` the order was placed
    with. It is the *source*, not a processed asset, and cannot carry the cut-out: that is a fact
    about the format, and `Note.FORMAT_SUBSTITUTED` on the result already states it.

    Only when `export.match_source` asked for same-format-out. A job that specified PNG only
    should not have a 130 MB raw appear in its download.

    Copied entry by entry straight from the stored upload archives, so nothing is duplicated in
    object storage and at most one original is in memory at a time — the same rule the ingest path
    follows (`backend/CLAUDE.md`, bulk invariant 1).
    """
    if not config.export.match_source:
        return

    from app.imaging import formats as F

    wanted = {
        image.source_name
        for image in done
        if image.source_format is not None and not F.can_round_trip(image.source_format)
    }
    if not wanted:
        return

    for archive_key in _batch_keys(job_id, status, storage):
        if not wanted:
            break
        data = storage.get(archive_key)
        try:
            with zip_reader.open_archive(data, settings) as archive:
                planned, _ = zip_reader.plan_entries(archive, settings)
                for entry in planned:
                    if entry.name not in wanted:
                        continue
                    try:
                        bundle.writestr(entry.name, zip_reader.read_entry(archive, entry))
                    except (KeyError, errors.PipelineError):
                        continue
                    wanted.discard(entry.name)
        finally:
            del data


def run_job(job_id: str) -> None:
    """RQ entrypoint. Synchronous by necessity — RQ workers are not async.

    Storage comes from `get_storage`, the same selector the API uses, rather than constructing a
    backend directly. The worker and the API must agree on where objects live: the API stores the
    upload and reads the results, the worker does the opposite, so a worker hardcoded to S3 while
    the API is on GCS would write assets nobody can find — with no error anywhere.
    """
    from app.api.deps import get_storage
    from app.core.jobstore import RedisJobStore

    settings = get_settings()
    asyncio.run(
        run_job_async(job_id, get_storage(settings), RedisJobStore(settings.redis_url), settings)
    )

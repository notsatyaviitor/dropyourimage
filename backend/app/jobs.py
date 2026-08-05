"""Job orchestration: unpack a zip, run every image through the pipeline, store the outputs.

The RQ worker entrypoint is `run_job`, which takes only a job id — everything else is read back
from storage. That keeps the queue payload tiny and means a retried job re-reads the same inputs
rather than depending on whatever was serialised at enqueue time.

Progress is written back after every image so the UI advances during a batch instead of jumping
from 0% to 100%.
"""

from __future__ import annotations

import asyncio
import io
import zipfile
from datetime import datetime, timezone

from app import pipeline
from app.core import errors
from app.core.jobstore import JobStore
from app.core.settings import Settings, get_settings
from app.engines import registry
from app.ingest import zip_reader
from app.models import (
    ImageResult,
    ImageState,
    JobConfig,
    JobState,
    JobStatus,
    OutputFormat,
)
from app.storage.base import StorageBackend, content_type_for, job_key

SOURCE_KEY = "source.zip"
CONFIG_KEY = "config.json"
BUNDLE_KEY = "outputs.zip"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def output_key(job_id: str, source_name: str, fmt: OutputFormat) -> str:
    stem = source_name.rsplit(".", 1)[0] or source_name
    return job_key(job_id, "out", f"{stem}.{fmt.value}")


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
    store.save(status)

    try:
        config = JobConfig.model_validate_json(
            storage.get(job_key(job_id, CONFIG_KEY)).decode("utf-8")
        )
        archive = storage.get(job_key(job_id, SOURCE_KEY))
        entries, rejections = zip_reader.read_images(archive, settings)
    except errors.PipelineError as exc:
        status.state = JobState.FAILED
        status.error = exc.to_info()
        status.finished_at = _now()
        store.save(status)
        return status
    except Exception:
        status.state = JobState.FAILED
        status.error = errors.PipelineError("The archive could not be read.").to_info()
        status.finished_at = _now()
        store.save(status)
        return status

    # Rejected entries are reported, not dropped: "17 of 20 processed" with no explanation is
    # indistinguishable from a bug.
    status.images = [
        ImageResult(
            source_name=r.name,
            state=ImageState.SKIPPED,
            error=errors.UnsupportedFile(r.reason).to_info(),
        )
        for r in rejections
    ]
    status.total = len(entries) + len(rejections)
    status.failed = len(rejections)
    store.save(status)

    engines = registry.select_pool(settings, config.cutout)
    semaphore = asyncio.Semaphore(max(1, settings.worker_concurrency))
    lock = asyncio.Lock()

    async def handle(entry: zip_reader.ZipEntry) -> None:
        async with semaphore:
            result, outputs = await pipeline.process_image(
                entry.data, entry.name, config, settings, engines=engines
            )

        assets = []
        for fmt, blob in outputs.items():
            key = output_key(job_id, entry.name, fmt)
            storage.put(key, blob, content_type_for(f"x.{fmt.value}"))
            assets.append((fmt, key, blob))

        # Serialised so concurrent images cannot interleave a read-modify-write on the record.
        async with lock:
            _attach_outputs(result, assets, storage, settings, config.export.profile)
            status.images.append(result)
            if result.state is ImageState.DONE:
                status.completed += 1
            else:
                status.failed += 1
            status.cost_usd = round(status.cost_usd + result.cost_usd, 6)
            store.save(status)

    await asyncio.gather(*(handle(e) for e in entries), return_exceptions=True)

    _finalise(status, job_id, storage, store, settings)
    return status


def _attach_outputs(
    result: ImageResult,
    assets: list[tuple[OutputFormat, str, bytes]],
    storage: StorageBackend,
    settings: Settings,
    profile,
) -> None:
    """Record each stored asset on the result: its URL, dimensions, size and colour profile.

    Every raster output in a job shares one profile (`JobConfig.export.profile`), so it is passed
    straight through rather than re-derived per asset.
    """
    from app.imaging import export as E
    from app.models import OutputAsset

    for fmt, key, blob in assets:
        width, height = E.dimensions_of(blob)
        result.outputs.append(
            OutputAsset(
                format=fmt,
                url=storage.signed_url(key, settings.signed_url_ttl_seconds),
                width=width,
                height=height,
                bytes=len(blob),
                profile=profile,
            )
        )


def _finalise(
    status: JobStatus,
    job_id: str,
    storage: StorageBackend,
    store: JobStore,
    settings: Settings,
) -> None:
    """Build the download bundle and mark the job complete.

    COMPLETED means "every image reached a terminal state", not "every image succeeded" — per-image
    failures are visible in the results grid. A job only FAILS as a whole when nothing could be
    processed at all.
    """
    done = [i for i in status.images if i.state is ImageState.DONE]

    if done:
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as bundle:
            for image in done:
                for asset in image.outputs:
                    stem = image.source_name.rsplit(".", 1)[0] or image.source_name
                    name = f"{stem}.{asset.format.value}"
                    try:
                        bundle.writestr(
                            name, storage.get(output_key(job_id, image.source_name, asset.format))
                        )
                    except KeyError:
                        continue

        key = job_key(job_id, BUNDLE_KEY)
        storage.put(key, buffer.getvalue(), "application/zip")
        status.bundle_url = storage.signed_url(key, settings.signed_url_ttl_seconds)

    status.state = JobState.COMPLETED if done else JobState.FAILED
    if not done and status.error is None:
        status.error = errors.PipelineError(
            "No image in the archive could be processed."
        ).to_info()

    status.finished_at = _now()
    store.save(status)


def run_job(job_id: str) -> None:
    """RQ entrypoint. Synchronous by necessity — RQ workers are not async."""
    from app.core.jobstore import RedisJobStore
    from app.storage.s3 import S3Storage

    settings = get_settings()
    asyncio.run(
        run_job_async(job_id, S3Storage(settings), RedisJobStore(settings.redis_url), settings)
    )

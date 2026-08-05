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
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import Response
from pydantic import ValidationError

from app.api.deps import get_jobstore, get_storage
from app.core.jobstore import JobStore
from app.core.settings import Settings, get_settings
from app.jobs import CONFIG_KEY, SOURCE_KEY, run_job_async
from app.models import JobConfig, JobCreated, JobState, JobStatus
from app.storage.base import StorageBackend, job_key

router = APIRouter()


def _settings_dep() -> Settings:
    return get_settings()


@router.get("/health")
def health(settings: Settings = Depends(_settings_dep)) -> dict:
    return {"status": "ok", "config": settings.redacted()}


@router.post("/jobs", response_model=JobCreated, status_code=202)
async def create_job(
    file: UploadFile = File(..., description="A .zip of product images"),
    config: str = Form(..., description="JobConfig as JSON"),
    settings: Settings = Depends(_settings_dep),
    storage: StorageBackend = Depends(get_storage),
    store: JobStore = Depends(get_jobstore),
) -> JobCreated:
    try:
        job_config = JobConfig.model_validate_json(config)
    except ValidationError as exc:
        # exc.errors() can carry a raw exception object under "ctx" (e.g. the ValueError from a
        # custom validator like BackgroundSpec's hex check), which json.dumps cannot serialise.
        # Drop "ctx" per-error rather than relying on jsonable_encoder to reach inside a list.
        detail = [{k: v for k, v in e.items() if k != "ctx"} for e in exc.errors(include_url=False)]
        raise HTTPException(status_code=422, detail=detail) from exc

    data = await _read_capped(file, settings.max_zip_bytes)

    job_id = uuid.uuid4().hex
    storage.put(job_key(job_id, SOURCE_KEY), data, "application/zip")
    storage.put(job_key(job_id, CONFIG_KEY), job_config.model_dump_json().encode("utf-8"), "application/json")

    from datetime import datetime, timezone

    status = JobStatus(
        job_id=job_id,
        state=JobState.QUEUED,
        config_hash=job_config.config_hash(),
        created_at=datetime.now(timezone.utc),
    )
    store.save(status)

    total = 0
    if _using_real_queue(settings):
        from app.queue import enqueue_job

        enqueue_job(job_id, settings)
        # The zip has not been unpacked yet, so the true image count is unknown at this point.
        # 0 here means "not yet known" — poll GET /jobs/{id} once the worker has started.
    else:
        # No worker to hand off to — see module docstring. Errors inside are not re-raised: a
        # failure becomes a FAILED job status, which GET /jobs/{id} reports, rather than a 500
        # for a request that was itself perfectly valid.
        await run_job_async(job_id, storage, store, settings)
        finished = store.load(job_id)
        total = finished.total if finished else 0

    return JobCreated(job_id=job_id, total=total, config_hash=job_config.config_hash())


@router.get("/jobs/{job_id}", response_model=JobStatus)
def get_job(job_id: str, store: JobStore = Depends(get_jobstore)) -> JobStatus:
    status = store.load(job_id)
    if status is None:
        raise HTTPException(status_code=404, detail=f"No job with id {job_id!r}.")
    return status


@router.get("/objects/{object_key:path}")
def get_object(object_key: str, storage: StorageBackend = Depends(get_storage)) -> Response:
    """Passthrough for memory-mode storage. Real S3 uses presigned URLs directly and never
    reaches this route — see `MemoryStorage.signed_url` vs `S3Storage.signed_url`."""
    try:
        data = storage.get(object_key)
    except KeyError:
        raise HTTPException(status_code=404, detail="Not found.") from None

    content_type = getattr(storage, "content_type", lambda _k: "application/octet-stream")(
        object_key
    )
    return Response(content=data, media_type=content_type)


def _using_real_queue(settings: Settings) -> bool:
    return settings.redis_url not in ("", "memory")


async def _read_capped(file: UploadFile, limit: int) -> bytes:
    """Read an upload with a hard byte cap, independent of any Content-Length header.

    A client can lie about Content-Length, so the cap is enforced against bytes actually read —
    the same principle as the zip-bomb guards in `zip_reader.py`.
    """
    chunks: list[bytes] = []
    total = 0
    chunk_size = 1024 * 1024

    while True:
        chunk = await file.read(chunk_size)
        if not chunk:
            break
        total += len(chunk)
        if total > limit:
            raise HTTPException(
                status_code=413,
                detail=f"Upload exceeds the {limit // 1_048_576} MB limit.",
            )
        chunks.append(chunk)

    return b"".join(chunks)

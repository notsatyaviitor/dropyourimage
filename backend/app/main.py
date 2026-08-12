"""FastAPI entrypoint.

    uvicorn app.main:app --reload --app-dir backend

CORS defaults to `*`, which is right for local development and the test suite and wrong for
anything reachable. `CORS_ALLOW_ORIGINS` narrows it to the real frontend origin — see
docs/DEPLOY.md. **This application still has no authentication of its own**; the deployment is
expected to put one in front of it, because `POST /jobs` spends money per image.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.routes import router
from app.core.settings import Settings, get_settings
from app.core.sizing import enforce_memory_headroom

logger = logging.getLogger(__name__)


def _origins(settings: Settings) -> list[str]:
    raw = settings.cors_allow_origins.strip()
    if raw == "*":
        return ["*"]
    return [o.strip() for o in raw.split(",") if o.strip()]


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    print(f"DropYourImage POC starting — config: {settings.redacted()}")

    # Refuse to start rather than discover the shortfall via the OOM killer, partway through a
    # job the vendor has already been paid for. See app/core/sizing.py.
    enforce_memory_headroom(settings)

    # Loud, because "*" on a reachable deployment means any page on the internet can drive an
    # endpoint that spends money — and it is the default, so it is what you get by omission.
    if _origins(settings) == ["*"] and settings.storage_backend_name != "memory":
        logger.warning(
            "CORS_ALLOW_ORIGINS is '*' while real object storage is configured. Set it to the "
            "frontend origin before exposing this."
        )
    yield


app = FastAPI(
    title="DropYourImage Image Processing POC",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=_origins(get_settings()),
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(router)

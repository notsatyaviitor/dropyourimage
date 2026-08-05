"""FastAPI entrypoint.

    uvicorn app.main:app --reload --app-dir backend

CORS is wide open (`*`) deliberately: this is a demo with no auth and no cookies, served to a
handful of people over the sprint. Tightening this to the actual frontend origin is the first
thing to do before this touches anything beyond the demo.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.routes import router
from app.core.settings import get_settings


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    print(f"DropYourImage POC starting — config: {settings.redacted()}")
    yield


app = FastAPI(
    title="DropYourImage Image Processing POC",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(router)

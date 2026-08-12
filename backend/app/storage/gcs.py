"""Google Cloud Storage, behind the same `StorageBackend` Protocol as S3 and memory.

Selected by setting `GCP_BUCKET_NAME`; see `app/api/deps.py` for the precedence. Nothing outside
this file knows which backend is in use — that is the whole point of the Protocol, and it is why
adding GCS is one new file plus one wiring line rather than a change to the pipeline.

Two things differ from S3 in ways that matter operationally:

* **Signing needs a private key, not just credentials.** `generate_signed_url` (V4) has to sign
  locally, so the *default* compute service account on GCE/GKE cannot do it without either a key
  file or the IAM Credentials API. That failure surfaces at download time rather than at startup,
  which is the worst moment to find it, so `ensure_bucket` checks for it up front and says so.
* **The bucket is not created automatically.** S3Storage creates its bucket because MinIO starts
  empty on every developer's machine. A GCS bucket has a location, storage class, lifecycle rules
  and billing attached; guessing those in application code is how you end up with a multi-region
  bucket nobody meant to pay for. A missing bucket is a configuration error and is reported as one.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import BinaryIO

from app.core.settings import Settings


class GcsStorage:
    def __init__(self, settings: Settings) -> None:
        from google.cloud import storage as gcs

        self._bucket_name = settings.gcp_bucket_name

        if settings.gcp_key_file:
            # A relative GCP_KEY_FILE resolves against the *process's* working directory, and
            # uvicorn (backend/) and `rq worker` are not started from the same place — so the API
            # can come up cleanly while the worker dies on the identical config. The raw
            # FileNotFoundError names only the filename, not the directory searched, which reads
            # like a missing key rather than a path problem.
            key_path = Path(settings.gcp_key_file)
            if not key_path.is_file():
                raise RuntimeError(
                    f"GCP_KEY_FILE {settings.gcp_key_file!r} not found. Resolved to "
                    f"{key_path.resolve()} (working directory {Path.cwd()}). Use an absolute "
                    "path: uvicorn and the RQ worker do not share a working directory."
                )
            self._client = gcs.Client.from_service_account_json(
                str(key_path), project=settings.gcp_project_id or None
            )
        else:
            # Application Default Credentials: the attached service account on GCE/GKE/Cloud Run,
            # or `gcloud auth application-default login` locally.
            self._client = gcs.Client(project=settings.gcp_project_id or None)

        self._bucket = self._client.bucket(self._bucket_name)

    def ensure_bucket(self) -> None:
        """Verify the bucket is reachable and that we can sign URLs, at startup rather than later.

        Deliberately does **not** create the bucket — see the module docstring. Both failures here
        are configuration errors that are cheap to fix now and expensive to diagnose once a job is
        half-finished and a client is looking at a broken download link.
        """
        from google.cloud.exceptions import NotFound

        try:
            if not self._bucket.exists():
                raise RuntimeError(
                    f"GCS bucket {self._bucket_name!r} does not exist or is not visible to these "
                    "credentials. Create it, or check GCP_PROJECT_ID and the service account's "
                    "roles (Storage Object Admin on the bucket)."
                )
        except NotFound as exc:
            raise RuntimeError(f"GCS bucket {self._bucket_name!r} not found.") from exc

        # Signing is what breaks silently. A client built from ADC on a default compute service
        # account has no private key, so every download URL would raise at generation time — long
        # after startup, on a page a client is looking at.
        if not hasattr(self._client, "_credentials") or not getattr(
            self._client._credentials, "signer_email", None
        ):
            raise RuntimeError(
                "These GCS credentials cannot sign download URLs. Set GCP_KEY_FILE to a service "
                "account JSON, or grant the runtime service account roles/iam.serviceAccountTokenCreator "
                "so IAM-based signing works."
            )

    def put(self, key: str, data: bytes, content_type: str) -> None:
        self._bucket.blob(key).upload_from_string(data, content_type=content_type)

    def put_stream(
        self,
        key: str,
        fileobj: BinaryIO,
        content_type: str,
        content_disposition: str | None = None,
    ) -> None:
        """Resumable upload straight from the file object — see StorageBackend.put_stream."""
        blob = self._bucket.blob(key)
        if content_disposition:
            blob.content_disposition = content_disposition
        blob.upload_from_file(fileobj, content_type=content_type)

    def get(self, key: str) -> bytes:
        from google.cloud.exceptions import NotFound

        try:
            return self._bucket.blob(key).download_as_bytes()
        except NotFound as exc:
            raise KeyError(f"no object at {key!r}") from exc

    def signed_url(self, key: str, ttl_seconds: int) -> str:
        """A time-limited read URL.

        V4 signing to match the S3 backend's `s3v4`, so both deployments expose the same shape of
        link and the same expiry semantics. The bucket itself stays private — a signed URL is the
        only way out, which is what `docs/SECURITY.md` requires of an API with no auth.
        """
        return self._bucket.blob(key).generate_signed_url(
            version="v4", expiration=timedelta(seconds=ttl_seconds), method="GET"
        )

    def exists(self, key: str) -> bool:
        return self._bucket.blob(key).exists()

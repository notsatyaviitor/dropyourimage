"""S3-compatible storage. MinIO locally, S3 or any compatible service in a real deployment."""

from __future__ import annotations

import boto3
from botocore.client import Config
from botocore.exceptions import ClientError

from app.core.settings import Settings


class S3Storage:
    def __init__(self, settings: Settings) -> None:
        self._bucket = settings.s3_bucket
        self._client = boto3.client(
            "s3",
            endpoint_url=settings.s3_endpoint_url or None,
            aws_access_key_id=settings.s3_access_key,
            aws_secret_access_key=settings.s3_secret_key,
            # Path-style addressing: MinIO does not do virtual-host buckets by default, and
            # signature v4 is required for presigned URLs to validate.
            config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
            region_name="us-east-1",
        )

    def ensure_bucket(self) -> None:
        """Create the bucket if missing. Called once at startup, not per request."""
        try:
            self._client.head_bucket(Bucket=self._bucket)
        except ClientError:
            self._client.create_bucket(Bucket=self._bucket)

    def put(self, key: str, data: bytes, content_type: str) -> None:
        self._client.put_object(
            Bucket=self._bucket, Key=key, Body=data, ContentType=content_type
        )

    def get(self, key: str) -> bytes:
        try:
            return self._client.get_object(Bucket=self._bucket, Key=key)["Body"].read()
        except ClientError as exc:
            raise KeyError(f"no object at {key!r}") from exc

    def signed_url(self, key: str, ttl_seconds: int) -> str:
        return self._client.generate_presigned_url(
            "get_object",
            Params={"Bucket": self._bucket, "Key": key},
            ExpiresIn=ttl_seconds,
        )

    def exists(self, key: str) -> bool:
        try:
            self._client.head_object(Bucket=self._bucket, Key=key)
            return True
        except ClientError:
            return False

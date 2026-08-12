"""In-memory storage for tests and for running the API with no MinIO.

Keeps `pytest` free of infrastructure. Not for real use: nothing is persisted and the "signed"
URLs are local API paths with no expiry, which is fine because the process holding the bytes is
the same one serving them.
"""

from __future__ import annotations


class MemoryStorage:
    def __init__(self) -> None:
        self._objects: dict[str, tuple[bytes, str]] = {}

    def put(self, key: str, data: bytes, content_type: str) -> None:
        self._objects[key] = (data, content_type)

    def put_stream(self, key: str, fileobj, content_type: str) -> None:
        """Reads it all in — which is exactly what this backend is: everything is already RAM.

        The streaming contract exists for S3/GCS, where the bundle can be tens of gigabytes.
        Memory mode only ever runs small jobs (`INLINE_JOB_MAX_IMAGES`), so there is nothing to
        stream to.
        """
        self._objects[key] = (fileobj.read(), content_type)

    def get(self, key: str) -> bytes:
        try:
            return self._objects[key][0]
        except KeyError:
            raise KeyError(f"no object at {key!r}") from None

    def content_type(self, key: str) -> str:
        return self._objects[key][1]

    def signed_url(self, key: str, ttl_seconds: int) -> str:
        # Served by the API's own passthrough route; ttl is ignored deliberately rather than
        # pretended to be enforced.
        return f"/objects/{key}"

    def exists(self, key: str) -> bool:
        return key in self._objects

    def clear(self) -> None:
        self._objects.clear()

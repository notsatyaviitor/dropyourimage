"""Typed errors that map onto `ErrorCode`.

The API layer converts these into `ErrorInfo`. The reason the taxonomy exists rather than a
single exception type: the UI must distinguish "the vendor throttled us" from "your file is
unsupported" from "we have a bug". One generic failure toast makes a demo look broken when it is
merely rate-limited.

Messages must be safe to display. Never interpolate a settings value, an API key, a signed URL,
or a filesystem path into one.
"""

from __future__ import annotations

from app.models import ErrorCode, ErrorInfo


class PipelineError(Exception):
    """Base class. Carries the code and whether a retry could plausibly help."""

    code: ErrorCode = ErrorCode.INTERNAL_ERROR
    retryable: bool = False

    def __init__(self, message: str | None = None) -> None:
        super().__init__(message or self.__class__.__doc__ or self.code.value)
        self.message = message or "An internal error occurred."

    def to_info(self) -> ErrorInfo:
        return ErrorInfo(code=self.code, message=self.message, retryable=self.retryable)


# --- vendor-side ------------------------------------------------------------


class VendorRateLimited(PipelineError):
    """The segmentation vendor is throttling us. Retrying later will work.

    `retry_after_seconds` carries the vendor's own `Retry-After` header when it sent one. Honouring
    it beats guessing: remove.bg's published limit scales down with megapixels (500 images/min at
    low resolution, ~10/min at 50 MP), so a fixed backoff either wastes time or hammers a vendor
    that already told us exactly how long to wait.
    """

    code = ErrorCode.VENDOR_RATE_LIMITED
    retryable = True

    def __init__(self, message: str | None = None, retry_after_seconds: float | None = None) -> None:
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


class VendorTimeout(PipelineError):
    """The segmentation vendor did not respond in time."""

    code = ErrorCode.VENDOR_TIMEOUT
    retryable = True


class VendorUnauthorized(PipelineError):
    """The vendor rejected our credentials, or no key is configured for this engine."""

    code = ErrorCode.VENDOR_UNAUTHORIZED
    retryable = False


class VendorOutOfCredits(PipelineError):
    """The vendor accepted the key but the account has no credit left.

    Split from `VendorUnauthorized` because the user action is completely different — top up the
    account versus fix the key — and because collapsing the two costs real debugging time: a 402 was
    once reported as "no segmentation engine was reachable", which reads like a network fault.
    """

    code = ErrorCode.VENDOR_OUT_OF_CREDITS
    retryable = False


class VendorPayloadTooLarge(PipelineError):
    """The vendor refused the upload as too large (HTTP 413).

    **Not retryable, and that is the whole point of it existing.** Falling through to the generic
    `VendorError` marked it retryable, so the UI offered "may succeed on retry" for a request that
    will send byte-identical content every time — it burns the retry budget and the demo's clock to
    arrive at the same 413. Same reasoning as `NoForegroundFound`.

    Measured: a 130 MB BMP sent to Photoroom as-is. The fix is upstream — `pipeline` now sizes the
    vendor payload before sending — so reaching this error means the budget is still too generous
    for that vendor, which is a configuration answer (`VENDOR_MAX_UPLOAD_BYTES`), not a retry.
    """

    code = ErrorCode.VENDOR_PAYLOAD_TOO_LARGE
    retryable = False


class NoForegroundFound(PipelineError):
    """The engine could not identify anything to cut out.

    A property of the image, not a fault: it is what remove.bg returns (`unknown_foreground`) for a
    crop with no clear subject — seen on a 402x81 strip of a TV cabinet, where there is no
    figure/ground separation to find. Deterministic, so **not retryable**: the same bytes fail
    identically every time, and retrying only spends the vendor's rate budget and the demo's time.
    """

    code = ErrorCode.NO_FOREGROUND_FOUND
    retryable = False


class VendorError(PipelineError):
    """The segmentation vendor returned an error."""

    code = ErrorCode.VENDOR_ERROR
    retryable = True


# --- input-side -------------------------------------------------------------


class UnsupportedFile(PipelineError):
    """This file is not an image we can process."""

    code = ErrorCode.UNSUPPORTED_FILE


class FileTooLarge(PipelineError):
    """This file exceeds the configured size limit."""

    code = ErrorCode.FILE_TOO_LARGE


class MaliciousArchive(PipelineError):
    """The uploaded archive tripped a safety guard and was rejected."""

    code = ErrorCode.MALICIOUS_ARCHIVE


class ImageDecodeFailed(PipelineError):
    """The image could not be decoded."""

    code = ErrorCode.IMAGE_DECODE_FAILED


class OutputTooLarge(PipelineError):
    """The requested output canvas exceeds this deployment's pixel budget."""

    code = ErrorCode.OUTPUT_TOO_LARGE


class BatchTooLarge(PipelineError):
    """The job as a whole exceeds the image-count or upload-size cap.

    Distinct from `FileTooLarge` and `MaliciousArchive` because the fix is different and the blame
    is different: nothing here is malformed or hostile, there is simply too much of it. A user who
    sees "malicious archive" for a legitimate 600-image upload learns the wrong thing.
    """

    code = ErrorCode.BATCH_TOO_LARGE


# --- capacity ---------------------------------------------------------------


class BulkRequiresWorker(PipelineError):
    """Too many images to process inline; this deployment has no queue configured.

    Memory mode runs a job synchronously inside the POST handler (see `app/api/routes.py`), which
    is fine for the handful of images a first run or a test uses and impossible for hundreds: the
    request would block for minutes to tens of minutes and time out with a half-finished job
    behind it. Refusing up front, with the fix named, beats a timeout that looks like a crash.
    """

    code = ErrorCode.BULK_REQUIRES_WORKER


class BudgetExceeded(PipelineError):
    """The job reached its vendor-spend ceiling and stopped.

    Not retryable in the sense the flag means — retrying the same job spends the same money again.
    Whatever finished before the ceiling is kept and downloadable; only the remainder is skipped.
    """

    code = ErrorCode.BUDGET_EXCEEDED


class JobCancelled(PipelineError):
    """The operator cancelled the job; the remaining images were not processed."""

    code = ErrorCode.JOB_CANCELLED


# --- feature availability ---------------------------------------------------


class PsdUnavailable(PipelineError):
    """Layered PSD output is not available in this deployment."""

    code = ErrorCode.PSD_UNAVAILABLE

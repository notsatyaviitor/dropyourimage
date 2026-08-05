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
    """The segmentation vendor is throttling us. Retrying later will work."""

    code = ErrorCode.VENDOR_RATE_LIMITED
    retryable = True


class VendorTimeout(PipelineError):
    """The segmentation vendor did not respond in time."""

    code = ErrorCode.VENDOR_TIMEOUT
    retryable = True


class VendorUnauthorized(PipelineError):
    """The vendor rejected our credentials, or no key is configured for this engine."""

    code = ErrorCode.VENDOR_UNAUTHORIZED
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


# --- feature availability ---------------------------------------------------


class PsdUnavailable(PipelineError):
    """Layered PSD output is not available in this deployment."""

    code = ErrorCode.PSD_UNAVAILABLE

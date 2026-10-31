"""Custom exceptions for the Matricula Toolkit."""


class MatriculaError(Exception):
    """Base exception for all Matricula Toolkit errors."""


class DownloadError(MatriculaError):
    """Raised when a page download fails."""


class NetworkError(MatriculaError):
    """Raised when a network request fails.

    Attributes:
        status_code: HTTP status code if the failure was an HTTP response,
            otherwise ``None``.
        retry_after: Value of the ``Retry-After`` header in seconds if
            present, otherwise ``None``.
    """

    def __init__(
        self,
        message: str,
        status_code: int | None = None,
        retry_after: int | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retry_after = retry_after


class ImageNotFoundError(MatriculaError):
    """Raised when no high-resolution image URL is found on a page."""


class BrowserError(MatriculaError):
    """Raised when the browser cannot be started or used."""


class ConfigurationError(MatriculaError):
    """Raised when configuration is invalid or missing."""


class ImageValidationError(MatriculaError):
    """Raised when an image file fails validation (corrupt, unsupported, etc.)."""


class ProxyError(MatriculaError):
    """Raised when the local SSRF proxy cannot operate."""

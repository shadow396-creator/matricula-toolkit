"""Configuration management for the Matricula Toolkit."""

from __future__ import annotations

import os
from dataclasses import dataclass

from matricula_toolkit.exceptions import ConfigurationError
from matricula_toolkit.security import SecurityError, validate_url


@dataclass
class MatriculaConfig:
    """Configuration for downloading Matricula pages."""

    base_url: str
    book: str = "01519"
    out_dir: str = "./downloads"
    wait_seconds: int = 5
    timeout_ms: int = 60000
    headless: bool = True
    viewport_width: int = 1920
    viewport_height: int = 1080
    max_parallel: int = 1
    max_file_size: int = 0
    max_pages: int = 0
    # OCR load protection thresholds (0 = disabled)
    ocr_max_cpu_percent: float = 85.0
    ocr_max_ram_percent: float = 85.0
    ocr_max_temperature_c: float | None = 80.0
    ocr_timeout_seconds: int = 300
    # Download retry configuration (0 = disabled)
    max_retries: int = 3
    retry_backoff_base: float = 1.0
    retry_backoff_max: float = 30.0

    @classmethod
    def from_env(cls) -> MatriculaConfig:
        """Create configuration from environment variables."""
        return cls(
            base_url=os.getenv("MATRICULA_BASE_URL", ""),
            book=os.getenv("MATRICULA_BOOK", "01519"),
            out_dir=os.getenv("MATRICULA_OUT_DIR", "./downloads"),
            wait_seconds=int(os.getenv("MATRICULA_WAIT_SECONDS", "5")),
            timeout_ms=int(os.getenv("MATRICULA_TIMEOUT_MS", "60000")),
            headless=os.getenv("MATRICULA_HEADLESS", "true").lower() == "true",
            viewport_width=int(os.getenv("MATRICULA_VIEWPORT_WIDTH", "1920")),
            viewport_height=int(os.getenv("MATRICULA_VIEWPORT_HEIGHT", "1080")),
            max_parallel=int(os.getenv("MATRICULA_MAX_PARALLEL", "1")),
            max_file_size=int(os.getenv("MATRICULA_MAX_FILE_SIZE", "0")),
            max_pages=int(os.getenv("MATRICULA_MAX_PAGES", "0")),
            ocr_max_cpu_percent=float(
                os.getenv("MATRICULA_OCR_MAX_CPU_PERCENT", "85.0")
            ),
            ocr_max_ram_percent=float(
                os.getenv("MATRICULA_OCR_MAX_RAM_PERCENT", "85.0")
            ),
            ocr_max_temperature_c=float(os.getenv("MATRICULA_OCR_MAX_TEMP_C", "80.0")),
            ocr_timeout_seconds=int(os.getenv("MATRICULA_OCR_TIMEOUT_SECONDS", "300")),
            max_retries=int(os.getenv("MATRICULA_MAX_RETRIES", "3")),
            retry_backoff_base=float(os.getenv("MATRICULA_RETRY_BACKOFF_BASE", "1.0")),
            retry_backoff_max=float(os.getenv("MATRICULA_RETRY_BACKOFF_MAX", "30.0")),
        )

    @classmethod
    def from_dict(cls, data: dict) -> MatriculaConfig:
        """Create configuration from a dictionary."""
        return cls(**data)

    def validate(self) -> None:
        """Validate the configuration. Raises ConfigurationError if invalid."""
        if not self.base_url:
            raise ConfigurationError("MATRICULA_BASE_URL is required")

        try:
            validate_url(self.base_url)
        except SecurityError as exc:
            raise ConfigurationError(str(exc)) from exc

        if not self.book:
            raise ConfigurationError("MATRICULA_BOOK is required")
        if self.wait_seconds < 1:
            raise ConfigurationError("MATRICULA_WAIT_SECONDS must be >= 1")
        if self.timeout_ms < 1000:
            raise ConfigurationError("MATRICULA_TIMEOUT_MS must be >= 1000")
        if self.max_parallel < 1:
            raise ConfigurationError("MATRICULA_MAX_PARALLEL must be >= 1")
        if self.ocr_max_cpu_percent < 0 or self.ocr_max_cpu_percent > 100:
            raise ConfigurationError(
                "MATRICULA_OCR_MAX_CPU_PERCENT must be between 0 and 100"
            )
        if self.ocr_max_ram_percent < 0 or self.ocr_max_ram_percent > 100:
            raise ConfigurationError(
                "MATRICULA_OCR_MAX_RAM_PERCENT must be between 0 and 100"
            )
        if self.ocr_max_temperature_c is not None and self.ocr_max_temperature_c < 0:
            raise ConfigurationError("MATRICULA_OCR_MAX_TEMP_C must be >= 0 or unset")
        if self.ocr_timeout_seconds < 1:
            raise ConfigurationError("MATRICULA_OCR_TIMEOUT_SECONDS must be >= 1")
        if self.max_retries < 0:
            raise ConfigurationError("MATRICULA_MAX_RETRIES must be >= 0")
        if self.retry_backoff_base < 0:
            raise ConfigurationError("MATRICULA_RETRY_BACKOFF_BASE must be >= 0")
        if self.retry_backoff_max < 0:
            raise ConfigurationError("MATRICULA_RETRY_BACKOFF_MAX must be >= 0")

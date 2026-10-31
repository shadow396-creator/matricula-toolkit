"""Matricula Toolkit - CLI and MCP server for downloading Matricula church book scans."""

__version__ = "1.0.0"

from matricula_toolkit.downloader import MatriculaDownloader, PageResult
from matricula_toolkit.exceptions import DownloadError, MatriculaError, NetworkError

__all__ = [
    "DownloadError",
    "MatriculaDownloader",
    "MatriculaError",
    "NetworkError",
    "PageResult",
    "__version__",
]

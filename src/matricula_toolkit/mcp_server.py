"""MCP server for the Matricula Toolkit.

Provides tools for inspecting books, pages, downloading, verifying,
and diagnosing Matricula church book scans.

Security: All user-supplied URLs and file paths are validated before use
to prevent SSRF (Server-Side Request Forgery) and path traversal attacks.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from matricula_toolkit.config import MatriculaConfig
from matricula_toolkit.downloader import MatriculaDownloader
from matricula_toolkit.exceptions import MatriculaError
from matricula_toolkit.security import SecurityError, validate_url


def validate_base_url(base_url: str) -> str | None:
    """Validate a user-supplied base URL.

    Returns an error message string if invalid, or None if valid.
    Only HTTPS URLs on public hosts are allowed.
    """
    if not base_url:
        return "base_url is required"

    try:
        validate_url(base_url)
    except SecurityError as exc:
        return str(exc)

    return None


def validate_image_path(file_path: str, allowed_dir: Path | None = None) -> str | None:
    """Validate a file path for image verification.

    Returns an error message string if invalid, or None if valid.
    Rejects path-traversal components and paths outside *allowed_dir*.
    Relative paths are resolved against *allowed_dir*.
    """
    if not file_path:
        return "path is required"

    path = Path(file_path)

    # Reject path-traversal components in the raw string
    if ".." in path.parts:
        return "path must not contain parent-directory references (..)"

    if allowed_dir is not None:
        allowed_dir = allowed_dir.resolve()

        # Resolve relative paths against the allowed directory
        if not path.is_absolute():
            path = allowed_dir / path

        try:
            resolved = path.resolve()
        except (OSError, RuntimeError):
            return "path cannot be resolved"

        try:
            resolved.relative_to(allowed_dir)
        except ValueError:
            return "path must be within the configured output directory"

    return None


class MatriculaMCPServer:
    """MCP server implementation for Matricula operations."""

    def __init__(self, config: MatriculaConfig | None = None):
        self.config = config or MatriculaConfig.from_env()

    # ------------------------------------------------------------------
    # Tool: inspect_book
    # ------------------------------------------------------------------
    async def inspect_book(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """Inspect a Matricula book: report page count estimate and structure."""
        base_url = arguments.get("base_url") or self.config.base_url
        book = arguments.get("book") or self.config.book

        if not base_url:
            return {"error": "base_url is required"}

        url_error = validate_base_url(base_url)
        if url_error:
            return {"error": url_error}

        config = MatriculaConfig(
            base_url=base_url,
            book=book,
            out_dir=self.config.out_dir,
        )

        try:
            async with MatriculaDownloader(config) as downloader:
                # Try downloading page 1 to verify the book is accessible
                result = await downloader.download_page(1)

            if result.success:
                return {
                    "book": book,
                    "base_url": base_url,
                    "accessible": True,
                    "sample_page": 1,
                    "sample_file": str(result.file_path) if result.file_path else None,
                    "sample_size_bytes": result.file_size,
                }
            else:
                return {
                    "book": book,
                    "base_url": base_url,
                    "accessible": False,
                    "error": result.error,
                }
        except MatriculaError as exc:
            return {
                "book": book,
                "base_url": base_url,
                "accessible": False,
                "error": str(exc),
            }

    # ------------------------------------------------------------------
    # Tool: inspect_page
    # ------------------------------------------------------------------
    async def inspect_page(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """Inspect a single page: download and report metadata."""
        page = arguments.get("page")
        base_url = arguments.get("base_url") or self.config.base_url
        book = arguments.get("book") or self.config.book

        if page is None:
            return {"error": "page is required"}
        if not base_url:
            return {"error": "base_url is required"}

        url_error = validate_base_url(base_url)
        if url_error:
            return {"error": url_error}

        config = MatriculaConfig(
            base_url=base_url,
            book=book,
            out_dir=self.config.out_dir,
        )

        try:
            async with MatriculaDownloader(config) as downloader:
                result = await downloader.download_page(page)

            if result.success:
                return {
                    "page": page,
                    "success": True,
                    "file_path": str(result.file_path) if result.file_path else None,
                    "file_size_bytes": result.file_size,
                    "image_url": result.image_url,
                    "duration_seconds": result.duration_seconds,
                }
            else:
                return {
                    "page": page,
                    "success": False,
                    "error": result.error,
                }
        except MatriculaError as exc:
            return {"page": page, "success": False, "error": str(exc)}

    # ------------------------------------------------------------------
    # Tool: download_page
    # ------------------------------------------------------------------
    async def download_page(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """Download a single page."""
        return await self.inspect_page(arguments)

    # ------------------------------------------------------------------
    # Tool: download_range
    # ------------------------------------------------------------------
    async def download_range(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """Download a range of pages."""
        start = arguments.get("start")
        end = arguments.get("end")
        base_url = arguments.get("base_url") or self.config.base_url
        book = arguments.get("book") or self.config.book

        if start is None or end is None:
            return {"error": "start and end are required"}
        try:
            start = int(start)
            end = int(end)
        except (TypeError, ValueError):
            return {"error": "start and end must be integers"}
        if start < 1 or end < 1:
            return {"error": "start and end must be positive integers"}
        if end < start:
            return {"error": "end must be >= start"}
        if not base_url:
            return {"error": "base_url is required"}

        url_error = validate_base_url(base_url)
        if url_error:
            return {"error": url_error}

        config = MatriculaConfig(
            base_url=base_url,
            book=book,
            out_dir=self.config.out_dir,
        )

        results: list[dict[str, Any]] = []
        success_count = 0
        fail_count = 0

        async with MatriculaDownloader(config) as downloader:
            page_results = await downloader.download_range(start, end)

        for r in page_results:
            entry: dict[str, Any] = {
                "page": r.page_number,
                "success": r.success,
            }
            if r.success:
                entry["file_path"] = str(r.file_path) if r.file_path else None
                entry["file_size_bytes"] = r.file_size
                success_count += 1
            else:
                entry["error"] = r.error
                fail_count += 1
            results.append(entry)

        return {
            "start": start,
            "end": end,
            "total": len(results),
            "success": success_count,
            "failed": fail_count,
            "results": results,
        }

    # ------------------------------------------------------------------
    # Tool: verify_image
    # ------------------------------------------------------------------
    async def verify_image(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """Verify that a downloaded image is a valid JPEG."""
        file_path = arguments.get("path")
        if not file_path:
            return {"error": "path is required"}

        path_error = validate_image_path(
            file_path, allowed_dir=Path(self.config.out_dir)
        )
        if path_error:
            return {"error": path_error}

        path = Path(file_path)

        async with MatriculaDownloader(self.config) as downloader:
            valid = await downloader.verify_image(path)

        return {
            "path": str(path),
            "valid": valid,
        }

    # ------------------------------------------------------------------
    # Tool: diagnose_error
    # ------------------------------------------------------------------
    async def diagnose_error(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """Diagnose a page URL: attempt download and report detailed error."""
        page = arguments.get("page")
        base_url = arguments.get("base_url")
        if page is None:
            return {"error": "page is required"}
        if not base_url:
            return {"error": "base_url is required"}

        url_error = validate_base_url(base_url)
        if url_error:
            return {"error": url_error}

        config = MatriculaConfig(
            base_url=base_url,
            book=self.config.book,
            out_dir=self.config.out_dir,
            headless=True,
        )

        try:
            async with MatriculaDownloader(config) as downloader:
                result = await downloader.download_page(page)

            return {
                "page": page,
                "base_url": base_url,
                "success": result.success,
                "file_path": str(result.file_path) if result.file_path else None,
                "file_size_bytes": result.file_size,
                "image_url": result.image_url,
                "error": result.error,
                "duration_seconds": result.duration_seconds,
            }
        except MatriculaError as exc:
            return {
                "page": page,
                "base_url": base_url,
                "success": False,
                "error": str(exc),
                "error_type": type(exc).__name__,
            }

    # ------------------------------------------------------------------
    # Tool: prepare_ocr
    # ------------------------------------------------------------------
    async def prepare_ocr(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """Prepare a directory of images for OCR: list files and sizes."""
        directory = arguments.get("directory") or self.config.out_dir
        book = arguments.get("book") or self.config.book

        path_error = validate_image_path(
            directory, allowed_dir=Path(self.config.out_dir).parent
        )
        if path_error:
            return {"error": path_error}

        path = Path(directory)
        if not path.is_dir():
            return {"error": f"Directory not found: {directory}"}

        pattern = f"matricula_{book}_pg*.jpg"
        images = sorted(path.glob(pattern))

        total_size = sum(img.stat().st_size for img in images)

        return {
            "directory": str(path),
            "book": book,
            "pattern": pattern,
            "image_count": len(images),
            "total_size_bytes": total_size,
            "total_size_mb": round(total_size / (1024 * 1024), 2),
            "files": [str(img) for img in images],
        }

    # ------------------------------------------------------------------
    # MCP protocol: list tools
    # ------------------------------------------------------------------
    def list_tools(self) -> list[dict[str, Any]]:
        """Return the list of available MCP tools."""
        return [
            {
                "name": "inspect_book",
                "description": "Inspect a Matricula book: verify accessibility and get sample page info",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "base_url": {
                            "type": "string",
                            "description": "Matricula viewer base URL",
                        },
                        "book": {"type": "string", "description": "Book identifier"},
                    },
                    "required": ["base_url"],
                },
            },
            {
                "name": "inspect_page",
                "description": "Download and inspect a single page",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "page": {"type": "integer", "description": "Page number"},
                        "base_url": {
                            "type": "string",
                            "description": "Matricula viewer base URL",
                        },
                        "book": {"type": "string", "description": "Book identifier"},
                    },
                    "required": ["page", "base_url"],
                },
            },
            {
                "name": "download_page",
                "description": "Download a single page",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "page": {"type": "integer", "description": "Page number"},
                        "base_url": {
                            "type": "string",
                            "description": "Matricula viewer base URL",
                        },
                        "book": {"type": "string", "description": "Book identifier"},
                    },
                    "required": ["page", "base_url"],
                },
            },
            {
                "name": "download_range",
                "description": "Download a range of pages",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "start": {
                            "type": "integer",
                            "description": "Start page (inclusive)",
                        },
                        "end": {
                            "type": "integer",
                            "description": "End page (inclusive)",
                        },
                        "base_url": {
                            "type": "string",
                            "description": "Matricula viewer base URL",
                        },
                        "book": {"type": "string", "description": "Book identifier"},
                    },
                    "required": ["start", "end", "base_url"],
                },
            },
            {
                "name": "verify_image",
                "description": "Verify that a downloaded image is a valid JPEG",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "Path to image file"},
                    },
                    "required": ["path"],
                },
            },
            {
                "name": "diagnose_error",
                "description": "Diagnose a page URL: attempt download and report detailed error",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "page": {"type": "integer", "description": "Page number"},
                        "base_url": {
                            "type": "string",
                            "description": "Matricula viewer base URL",
                        },
                    },
                    "required": ["page", "base_url"],
                },
            },
            {
                "name": "prepare_ocr",
                "description": "Prepare a directory of images for OCR: list files and sizes",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "directory": {
                            "type": "string",
                            "description": "Directory containing images",
                        },
                        "book": {"type": "string", "description": "Book identifier"},
                    },
                    "required": [],
                },
            },
        ]

    # ------------------------------------------------------------------
    # MCP protocol: call tool
    # ------------------------------------------------------------------
    async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Call an MCP tool by name."""
        handlers = {
            "inspect_book": self.inspect_book,
            "inspect_page": self.inspect_page,
            "download_page": self.download_page,
            "download_range": self.download_range,
            "verify_image": self.verify_image,
            "diagnose_error": self.diagnose_error,
            "prepare_ocr": self.prepare_ocr,
        }

        if name not in handlers:
            return {"error": f"Unknown tool: {name}"}

        try:
            return await handlers[name](arguments)
        except MatriculaError as exc:
            return {"error": str(exc), "error_type": type(exc).__name__}
        except Exception as exc:  # noqa: BLE001
            return {"error": str(exc), "error_type": type(exc).__name__}


def main() -> None:
    """Entry point for the MCP server (stdio transport).

    Loads environment variables from a ``.env`` file in the current working
    directory (if present) before starting, so that ``MATRICULA_*`` variables
    defined there are picked up automatically.
    """
    load_dotenv()

    server = MatriculaMCPServer()

    async def run():
        while True:
            try:
                line = await asyncio.get_running_loop().run_in_executor(
                    None, sys.stdin.readline
                )
                if not line:
                    break

                line = line.strip()
                if not line:
                    continue

                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    response = {
                        "jsonrpc": "2.0",
                        "error": {"code": -32700, "message": "Parse error"},
                        "id": None,
                    }
                    print(json.dumps(response), flush=True)
                    continue

                msg_id = message.get("id")
                method = message.get("method")
                params = message.get("params", {})

                if method == "initialize":
                    response = {
                        "jsonrpc": "2.0",
                        "id": msg_id,
                        "result": {
                            "protocolVersion": "2024-11-05",
                            "capabilities": {"tools": {}},
                            "serverInfo": {
                                "name": "matricula-toolkit",
                                "version": "0.1.0",
                            },
                        },
                    }
                elif method == "tools/list":
                    response = {
                        "jsonrpc": "2.0",
                        "id": msg_id,
                        "result": {"tools": server.list_tools()},
                    }
                elif method == "tools/call":
                    tool_name = params.get("name", "")
                    tool_args = params.get("arguments", {})
                    result = await server.call_tool(tool_name, tool_args)
                    response = {
                        "jsonrpc": "2.0",
                        "id": msg_id,
                        "result": {
                            "content": [
                                {"type": "text", "text": json.dumps(result, indent=2)}
                            ]
                        },
                    }
                else:
                    response = {
                        "jsonrpc": "2.0",
                        "id": msg_id,
                        "error": {
                            "code": -32601,
                            "message": f"Method not found: {method}",
                        },
                    }

                print(json.dumps(response), flush=True)

            except KeyboardInterrupt:
                break
            except Exception as exc:  # noqa: BLE001
                print(
                    json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "error": {"code": -32603, "message": str(exc)},
                            "id": None,
                        }
                    ),
                    flush=True,
                )

    asyncio.run(run())


if __name__ == "__main__":
    main()

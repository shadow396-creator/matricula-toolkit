"""Command-line interface for the Matricula Toolkit."""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

from matricula_toolkit.config import MatriculaConfig
from matricula_toolkit.downloader import MatriculaDownloader, PageResult
from matricula_toolkit.exceptions import MatriculaError


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="matricula-download",
        description="Download Matricula church book scans",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    # download-page command
    page_parser = subparsers.add_parser("page", help="Download a single page")
    page_parser.add_argument("page", type=int, help="Page number to download")
    page_parser.add_argument(
        "--url", help="Matricula viewer base URL (overrides MATRICULA_BASE_URL)"
    )
    page_parser.add_argument(
        "--book", help="Book identifier for filename (overrides MATRICULA_BOOK)"
    )
    page_parser.add_argument(
        "--out", help="Output directory (overrides MATRICULA_OUT_DIR)"
    )
    page_parser.add_argument(
        "--wait", type=int, default=5, help="Wait seconds per page (default: 5)"
    )
    page_parser.add_argument(
        "--headless",
        action="store_true",
        default=True,
        dest="headless",
        help="Run browser headless",
    )
    page_parser.add_argument(
        "--headed",
        action="store_false",
        dest="headless",
        help="Run browser with UI",
    )

    # download-range command
    range_parser = subparsers.add_parser("range", help="Download a range of pages")
    range_parser.add_argument("start", type=int, help="Start page number (inclusive)")
    range_parser.add_argument("end", type=int, help="End page number (inclusive)")
    range_parser.add_argument("--url", help="Matricula viewer base URL")
    range_parser.add_argument("--book", help="Book identifier for filename")
    range_parser.add_argument("--out", help="Output directory")
    range_parser.add_argument(
        "--wait", type=int, default=5, help="Wait seconds per page (default: 5)"
    )
    range_parser.add_argument(
        "--headless",
        action="store_true",
        default=True,
        dest="headless",
        help="Run browser headless",
    )
    range_parser.add_argument(
        "--headed",
        action="store_false",
        dest="headless",
        help="Run browser with UI",
    )
    range_parser.add_argument(
        "--max-pages",
        type=int,
        default=0,
        help="Maximum pages to download (0 = unlimited)",
    )

    # verify command
    verify_parser = subparsers.add_parser("verify", help="Verify downloaded images")
    verify_parser.add_argument("path", help="Path to image file or directory")
    verify_parser.add_argument(
        "--book", default="01519", help="Book identifier (default: 01519)"
    )

    # diagnose command
    diag_parser = subparsers.add_parser("diagnose", help="Diagnose a page URL")
    diag_parser.add_argument("page", type=int, help="Page number to diagnose")
    diag_parser.add_argument("--url", required=True, help="Matricula viewer base URL")

    return parser


def _format_result(result: PageResult) -> str:
    """Format a PageResult for console output."""
    if result.success:
        size_kb = (result.file_size or 0) / 1024
        dur = f"{result.duration_seconds:.1f}s" if result.duration_seconds else "?"
        return (
            f"  [OK] Page {result.page_number}: {result.file_path.name} "  # type: ignore[union-attr]
            f"({size_kb:.1f} KB, {dur})"
        )
    else:
        return f"  [FAIL] Page {result.page_number}: {result.error}"


async def _run_download_page(args: argparse.Namespace) -> int:
    config = MatriculaConfig.from_env()
    if args.url:
        config.base_url = args.url
    if args.book:
        config.book = args.book
    if args.out:
        config.out_dir = args.out
    config.wait_seconds = args.wait
    config.headless = args.headless

    config.validate()

    async with MatriculaDownloader(config) as downloader:
        result = await downloader.download_page(args.page)

    print(_format_result(result))
    return 0 if result.success else 1


async def _run_download_range(args: argparse.Namespace) -> int:
    config = MatriculaConfig.from_env()
    if args.url:
        config.base_url = args.url
    if args.book:
        config.book = args.book
    if args.out:
        config.out_dir = args.out
    config.wait_seconds = args.wait
    config.headless = args.headless
    config.max_pages = args.max_pages

    config.validate()

    start_time = time.monotonic()
    success_count = 0
    fail_count = 0
    skip_count = 0

    def progress(result: PageResult) -> None:
        nonlocal success_count, fail_count, skip_count
        if result.success:
            if result.file_path and result.file_path.exists():
                # Check if it was skipped (already existed)
                pass
            success_count += 1
        else:
            fail_count += 1
        print(_format_result(result), flush=True)

    async with MatriculaDownloader(config) as downloader:
        results = await downloader.download_range(
            args.start, args.end, progress_callback=progress
        )

    duration = time.monotonic() - start_time
    print()
    print("=== Summary ===")
    print(f"Total:   {len(results)}")
    print(f"Success: {success_count}")
    print(f"Failed:  {fail_count}")
    print(f"Duration: {duration:.1f}s")

    return 0 if fail_count == 0 else 1


async def _run_verify(args: argparse.Namespace) -> int:
    from matricula_toolkit.downloader import MatriculaDownloader

    path = Path(args.path)

    async with MatriculaDownloader(MatriculaConfig.from_env()) as downloader:
        if path.is_file():
            valid = await downloader.verify_image(path)
            status = "VALID" if valid else "INVALID"
            print(f"{path}: {status}")
            return 0 if valid else 1
        elif path.is_dir():
            images = sorted(path.glob("matricula_*.jpg"))
            if not images:
                print(f"No matricula_*.jpg files found in {path}")
                return 1
            valid_count = 0
            invalid_count = 0
            for img in images:
                valid = await downloader.verify_image(img)
                status = "VALID" if valid else "INVALID"
                print(f"  {img.name}: {status}")
                if valid:
                    valid_count += 1
                else:
                    invalid_count += 1
            print(f"\nValid: {valid_count}, Invalid: {invalid_count}")
            return 0 if invalid_count == 0 else 1
        else:
            print(f"Path not found: {path}")
            return 1


async def _run_diagnose(args: argparse.Namespace) -> int:
    config = MatriculaConfig.from_env()
    config.base_url = args.url
    config.validate()

    async with MatriculaDownloader(config) as downloader:
        result = await downloader.download_page(args.page)

    if result.success:
        print(f"Page {args.page}: OK")
        print(f"  File: {result.file_path}")
        print(f"  Size: {(result.file_size or 0) / 1024:.1f} KB")
        print(f"  Image URL: {result.image_url}")
    else:
        print(f"Page {args.page}: FAILED")
        print(f"  Error: {result.error}")

    return 0 if result.success else 1


def main(argv: list[str] | None = None) -> int:
    """Main entry point for the CLI.

    Loads environment variables from a ``.env`` file in the current working
    directory (if present) before parsing arguments, so that
    ``MATRICULA_*`` variables defined there are picked up automatically.
    """
    load_dotenv()

    parser = _build_parser()

    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        # argparse calls sys.exit on errors (missing command, invalid choice, etc.)
        return exc.code if isinstance(exc.code, int) else 1

    try:
        if args.command == "page":
            return asyncio.run(_run_download_page(args))
        elif args.command == "range":
            return asyncio.run(_run_download_range(args))
        elif args.command == "verify":
            return asyncio.run(_run_verify(args))
        elif args.command == "diagnose":
            return asyncio.run(_run_diagnose(args))
        else:
            parser.print_help()
            return 1
    except MatriculaError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nInterrupted by user", file=sys.stderr)
        return 130
    except Exception as exc:  # noqa: BLE001
        print(f"Unexpected error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

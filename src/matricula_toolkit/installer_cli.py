"""Command-line entry point for the Matricula Toolkit installer."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from matricula_toolkit.installer import (
    ComponentStatus,
    detect_components,
    detect_system,
    run_installer,
)


def _format_components(components: list[ComponentStatus]) -> str:
    lines = []
    for comp in components:
        status = "OK" if comp.installed else "MISSING"
        optional = " (optional)" if comp.optional else ""
        version = f" v{comp.version}" if comp.version else ""
        path = f" at {comp.path}" if comp.path else ""
        lines.append(f"  [{status}] {comp.name}{version}{optional}{path}")
    return "\n".join(lines)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="matricula-install",
        description="Matricula Toolkit installation assistant",
    )
    parser.add_argument(
        "--no-venv",
        action="store_true",
        help="Skip creating a virtual environment",
    )
    parser.add_argument(
        "--venv-path",
        type=Path,
        default=None,
        help="Path for the virtual environment (default: .venv in project dir)",
    )
    parser.add_argument(
        "--no-kraken",
        action="store_true",
        help="Skip Kraken OCR backend installation",
    )
    parser.add_argument(
        "--no-models",
        action="store_true",
        help="Skip Kraken model download",
    )
    parser.add_argument(
        "--non-interactive",
        action="store_true",
        help="Run without prompting (skip steps requiring confirmation)",
    )
    parser.add_argument(
        "--check-only",
        action="store_true",
        help="Only detect system and components, do not install",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Main entry point for the installer CLI."""
    parser = _build_parser()
    args = parser.parse_args(argv)

    # Detect system and components
    system_info = detect_system()
    components = detect_components()

    print("=== Matricula Toolkit - System Check ===")
    print(f"OS: {system_info.os} {system_info.os_version}")
    print(f"Distribution: {system_info.distribution or 'unknown'}")
    print(f"Architecture: {system_info.architecture}")
    print(f"Python: {system_info.python_version} ({system_info.python_executable})")
    print(f"RAM: {system_info.ram_total_mb} MB")
    print(f"Disk free: {system_info.disk_free_mb} MB")
    print(f"GPU: {system_info.gpu_vendor or 'none detected'}")
    print(f"Container: {system_info.is_container}")
    print()
    print("=== Component Check ===")
    print(_format_components(components))
    print()

    if args.check_only:
        missing = [c.name for c in components if not c.installed and not c.optional]
        if missing:
            print(f"Missing required components: {', '.join(missing)}")
            return 1
        print("All required components are installed.")
        return 0

    # Run installer
    result = run_installer(
        interactive=not args.non_interactive,
        create_venv=not args.no_venv,
        venv_path=args.venv_path,
        install_kraken=not args.no_kraken,
        install_models=not args.no_models,
    )

    print()
    print("=== Installation Log ===")
    for line in result.log:
        print(line)

    print()
    if result.errors:
        print("=== Errors ===")
        for err in result.errors:
            print(f"  ERROR: {err}")
        return 1

    if result.warnings:
        print("=== Warnings ===")
        for warn in result.warnings:
            print(f"  WARNING: {warn}")

    if result.success:
        print("\nInstallation completed successfully.")
        return 0
    print("\nInstallation completed with errors.")
    return 1


if __name__ == "__main__":
    sys.exit(main())

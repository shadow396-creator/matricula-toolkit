"""Desktop workbench for the Matricula Toolkit."""

try:
    from .app import SECTIONS, MainWindow, main
except ImportError:  # pragma: no cover - Qt is optional at runtime
    from .app import SECTIONS, main

    MainWindow = None  # type: ignore

__all__ = ["SECTIONS", "MainWindow", "main"]

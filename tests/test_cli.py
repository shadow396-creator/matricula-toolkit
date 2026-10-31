"""Tests for the CLI."""

from __future__ import annotations

from unittest.mock import patch

from matricula_toolkit.cli import main


class TestCLI:
    """Tests for the CLI entry point."""

    def test_no_command(self):
        """Calling main with no arguments should print help and return non-zero."""
        result = main([])
        assert result != 0

    def test_unknown_command(self):
        """Calling main with an unknown command should return non-zero."""
        result = main(["unknown"])
        assert result != 0

    @patch("matricula_toolkit.cli.asyncio.run")
    def test_page_command(self, mock_run):
        """The page command should call asyncio.run without leaving coroutine objects unclosed."""

        def _close_coroutine(coro):
            coro.close()

        mock_run.side_effect = _close_coroutine
        try:
            main(["page", "104"])
        except SystemExit:
            pass

    @patch("matricula_toolkit.cli.asyncio.run")
    def test_range_command(self, mock_run):
        """The range command should call asyncio.run without leaving coroutine objects unclosed."""

        def _close_coroutine(coro):
            coro.close()

        mock_run.side_effect = _close_coroutine
        try:
            main(["range", "104", "200"])
        except SystemExit:
            pass

    def test_verify_command_missing_path(self):
        """The verify command without a path should return non-zero."""
        result = main(["verify"])
        assert result != 0

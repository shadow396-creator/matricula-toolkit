"""Mock-based tests for the Matricula Toolkit installer.

All tests use mocks to avoid installing system packages, downloading models,
or creating virtual environments on the test host.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

from matricula_toolkit.installer import (
    ComponentStatus,
    Installer,
    InstallPlan,
    InstallResult,
    SystemInfo,
    _detect_container,
    _detect_gpu,
    _detect_linux_distribution,
    _detect_package_manager,
    _module_available,
    _read_disk_free_mb,
    _read_ram_total_mb,
    _run_command,
    detect_components,
    detect_system,
    run_installer,
)

# ---------------------------------------------------------------------------
# System detection
# ---------------------------------------------------------------------------


class TestDetectSystem:
    """Tests for detect_system()."""

    def test_detect_system_returns_system_info(self):
        info = detect_system()
        assert isinstance(info, SystemInfo)
        assert info.os in ("Linux", "Darwin", "Windows")
        assert info.architecture != ""
        assert info.python_version != ""
        assert info.python_executable != ""

    def test_detect_system_ram_non_negative(self):
        info = detect_system()
        assert info.ram_total_mb >= 0

    def test_detect_system_disk_non_negative(self):
        info = detect_system()
        assert info.disk_free_mb >= 0


class TestReadRamTotalMb:
    """Tests for _read_ram_total_mb()."""

    def test_read_ram_total_mb(self):
        ram = _read_ram_total_mb()
        assert ram >= 0


class TestReadDiskFreeMb:
    """Tests for _read_disk_free_mb()."""

    def test_read_disk_free_mb(self):
        free = _read_disk_free_mb(Path.cwd())
        assert free >= 0


class TestDetectLinuxDistribution:
    """Tests for _detect_linux_distribution()."""

    def test_detect_linux_distribution_returns_string(self):
        distro = _detect_linux_distribution()
        assert isinstance(distro, str)


class TestDetectContainer:
    """Tests for _detect_container()."""

    def test_detect_container_returns_bool(self):
        assert isinstance(_detect_container(), bool)


class TestDetectGpu:
    """Tests for _detect_gpu()."""

    def test_detect_gpu_returns_tuple(self):
        available, vendor = _detect_gpu()
        assert isinstance(available, bool)
        assert vendor is None or isinstance(vendor, str)


# ---------------------------------------------------------------------------
# Component detection
# ---------------------------------------------------------------------------


class TestDetectComponents:
    """Tests for detect_components()."""

    def test_detect_components_returns_list(self):
        components = detect_components()
        assert isinstance(components, list)
        assert len(components) > 0

    def test_detect_components_has_python(self):
        components = detect_components()
        python_comp = next((c for c in components if c.name == "python"), None)
        assert python_comp is not None
        assert python_comp.installed is True

    def test_detect_components_all_have_name_and_installed(self):
        components = detect_components()
        for comp in components:
            assert isinstance(comp, ComponentStatus)
            assert comp.name != ""
            assert isinstance(comp.installed, bool)


class TestModuleAvailable:
    """Tests for _module_available()."""

    def test_module_available_os(self):
        assert (
            _module_available(
                sys.executable if "sys" in dir() else __import__("sys").executable, "os"
            )
            is True
        )

    def test_module_available_nonexistent(self):
        import sys

        assert _module_available(sys.executable, "nonexistent_module_xyz") is False


# ---------------------------------------------------------------------------
# Run command
# ---------------------------------------------------------------------------


class TestRunCommand:
    """Tests for _run_command()."""

    def test_run_command_success(self):
        rc, out, _err = _run_command(
            [sys.executable, "-c", "print('hello')"], capture=True
        )
        assert rc == 0
        assert "hello" in out

    def test_run_command_not_found(self):
        rc, out, err = _run_command(["nonexistent_command_xyz"], capture=True)
        assert rc == -1
        assert "not found" in err.lower() or "not found" in out.lower()

    def test_run_command_timeout(self):
        rc, _out, err = _run_command(
            [sys.executable, "-c", "import time; time.sleep(10)"],
            capture=True,
            timeout=1,
        )
        assert rc == -1
        assert "timed out" in err.lower()


# ---------------------------------------------------------------------------
# Package manager detection
# ---------------------------------------------------------------------------


class TestDetectPackageManager:
    """Tests for _detect_package_manager()."""

    def test_detect_package_manager_returns_dict_or_none(self):
        from matricula_toolkit.installer import SystemInfo

        info = SystemInfo(os="Linux", distribution="Ubuntu 22.04")
        result = _detect_package_manager(info)
        assert result is None or isinstance(result, dict)
        if result:
            assert "name" in result
            assert "install_cmd" in result


# ---------------------------------------------------------------------------
# Install plan
# ---------------------------------------------------------------------------


class TestComputeInstallPlan:
    """Tests for compute_install_plan()."""

    def test_compute_install_plan_returns_plan(self):
        from matricula_toolkit.installer import compute_install_plan

        info = SystemInfo(os="Linux", distribution="Ubuntu 22.04")
        components = [
            ComponentStatus(name="python", installed=True),
            ComponentStatus(name="playwright", installed=False),
            ComponentStatus(name="Pillow", installed=True),
            ComponentStatus(name="python-dotenv", installed=True),
            ComponentStatus(name="kraken", installed=False),
            ComponentStatus(name="kraken-python", installed=False),
        ]
        plan = compute_install_plan(info, components)
        assert isinstance(plan, InstallPlan)
        assert "playwright" in plan.python_packages
        assert "kraken" in plan.system_packages or "kraken" in plan.python_packages

    def test_compute_install_plan_skip_installed(self):
        from matricula_toolkit.installer import compute_install_plan

        info = SystemInfo(os="Linux", distribution="Ubuntu 22.04")
        components = [
            ComponentStatus(name="python", installed=True),
            ComponentStatus(name="playwright", installed=True),
            ComponentStatus(name="Pillow", installed=True),
            ComponentStatus(name="python-dotenv", installed=True),
            ComponentStatus(name="kraken", installed=True),
            ComponentStatus(name="kraken-python", installed=True),
        ]
        plan = compute_install_plan(info, components)
        assert "playwright" not in plan.python_packages
        assert "kraken" not in plan.system_packages
        assert "kraken" not in plan.python_packages


# ---------------------------------------------------------------------------
# Installer class
# ---------------------------------------------------------------------------


class TestInstallerCreateVenv:
    """Tests for Installer._create_venv()."""

    def test_create_venv_skips_existing(self, tmp_path):
        info = SystemInfo(os="Linux")
        plan = InstallPlan(create_venv=True, venv_path=tmp_path / ".venv")
        # Create a fake venv with python executable
        venv_dir = tmp_path / ".venv"
        bin_dir = venv_dir / "bin"
        bin_dir.mkdir(parents=True, exist_ok=True)
        (bin_dir / "python").touch()

        installer = Installer(plan, info)
        installer._create_venv(interactive=False)
        assert (
            "already exists" in "".join(installer.result.log)
            or installer.result.success
        )


class TestInstallerVerify:
    """Tests for Installer._verify_installation()."""

    def test_verify_installation(self, tmp_path):
        info = SystemInfo(os="Linux")
        plan = InstallPlan(create_venv=True, venv_path=tmp_path / ".venv")
        installer = Installer(plan, info)
        installer._verify_installation()
        # Should not crash; may have errors/warnings but that's fine
        assert isinstance(installer.result, InstallResult)


# ---------------------------------------------------------------------------
# run_installer convenience function
# ---------------------------------------------------------------------------


class TestRunInstaller:
    """Tests for run_installer()."""

    @patch("matricula_toolkit.installer.Installer.run")
    def test_run_installer_returns_result(self, mock_run):
        mock_result = InstallResult(success=True, log=["test"])
        mock_run.return_value = mock_result

        result = run_installer(interactive=False)
        assert result == mock_result
        mock_run.assert_called_once_with(interactive=False)

    @patch("matricula_toolkit.installer.Installer.run")
    def test_run_installer_non_interactive(self, mock_run):
        mock_result = InstallResult(success=True)
        mock_run.return_value = mock_result

        run_installer(interactive=False, create_venv=False, install_kraken=False)
        mock_run.assert_called_once_with(interactive=False)


# ---------------------------------------------------------------------------
# System package installation (CI / non-interactive / update_cmd)
# ---------------------------------------------------------------------------


class TestInstallSystemPackages:
    """Tests for Installer._install_system_packages()."""

    def _make_installer(self, packages: list[str] | None = None) -> Installer:
        info = SystemInfo(os="Linux")
        plan = InstallPlan(
            create_venv=False,
            system_packages=packages or ["chromium-browser"],
        )
        return Installer(plan, info)

    @patch("matricula_toolkit.installer._detect_package_manager")
    @patch("matricula_toolkit.installer._run_command")
    def test_skips_when_not_interactive(
        self, mock_run: MagicMock, mock_pkg: MagicMock
    ) -> None:
        """No sudo/apt-get when interactive=False."""
        mock_pkg.return_value = {
            "name": "apt-get",
            "install_cmd": ["sudo", "apt-get", "install", "-y"],
            "update_cmd": ["sudo", "apt-get", "update"],
        }
        installer = self._make_installer()
        installer._install_system_packages(interactive=False)
        mock_run.assert_not_called()
        assert any("skipping" in entry.lower() for entry in installer.result.log)

    @patch("matricula_toolkit.installer._detect_package_manager")
    @patch("matricula_toolkit.installer._run_command")
    def test_skips_when_ci_env_set(
        self, mock_run: MagicMock, mock_pkg: MagicMock
    ) -> None:
        """No sudo/apt-get when CI environment variable is set."""
        mock_pkg.return_value = {
            "name": "apt-get",
            "install_cmd": ["sudo", "apt-get", "install", "-y"],
            "update_cmd": ["sudo", "apt-get", "update"],
        }
        installer = self._make_installer()
        with patch.dict("os.environ", {"CI": "true"}):
            installer._install_system_packages(interactive=True)
        mock_run.assert_not_called()
        assert any("skipping" in entry.lower() for entry in installer.result.log)

    @patch("matricula_toolkit.installer._detect_package_manager")
    @patch("matricula_toolkit.installer._run_command")
    def test_skips_when_noninteractive_env_set(
        self, mock_run: MagicMock, mock_pkg: MagicMock
    ) -> None:
        """No sudo/apt-get when NONINTERACTIVE environment variable is set."""
        mock_pkg.return_value = {
            "name": "apt-get",
            "install_cmd": ["sudo", "apt-get", "install", "-y"],
            "update_cmd": ["sudo", "apt-get", "update"],
        }
        installer = self._make_installer()
        with patch.dict("os.environ", {"NONINTERACTIVE": "1"}):
            installer._install_system_packages(interactive=True)
        mock_run.assert_not_called()
        assert any("skipping" in entry.lower() for entry in installer.result.log)

    @patch("matricula_toolkit.installer._detect_package_manager")
    @patch("matricula_toolkit.installer._run_command")
    def test_runs_update_cmd_before_install(
        self, mock_run: MagicMock, mock_pkg: MagicMock
    ) -> None:
        """update_cmd (apt-get update) runs before install_cmd in interactive mode."""
        mock_pkg.return_value = {
            "name": "apt-get",
            "install_cmd": ["sudo", "apt-get", "install", "-y"],
            "update_cmd": ["sudo", "apt-get", "update"],
        }
        mock_run.return_value = (0, "", "")
        installer = self._make_installer()
        # CI/NONINTERACTIVE must be unset so the installer does not skip
        with patch.dict("os.environ", {}, clear=False):
            for key in ("CI", "NONINTERACTIVE"):
                os.environ.pop(key, None)
            installer._install_system_packages(interactive=True)
        # update_cmd must be called before install_cmd
        call_args = [call[0][0] for call in mock_run.call_args_list]
        assert ["sudo", "apt-get", "update"] in call_args
        assert ["sudo", "apt-get", "install", "-y", "chromium-browser"] in call_args
        update_idx = call_args.index(["sudo", "apt-get", "update"])
        install_idx = call_args.index(
            ["sudo", "apt-get", "install", "-y", "chromium-browser"]
        )
        assert update_idx < install_idx

    @patch("matricula_toolkit.installer._detect_package_manager")
    @patch("matricula_toolkit.installer._run_command")
    def test_no_update_cmd_for_dnf(
        self, mock_run: MagicMock, mock_pkg: MagicMock
    ) -> None:
        """Package managers without update_cmd (e.g. dnf) skip the update step."""
        mock_pkg.return_value = {
            "name": "dnf",
            "install_cmd": ["sudo", "dnf", "install", "-y"],
        }
        mock_run.return_value = (0, "", "")
        installer = self._make_installer(["some-package"])
        # CI/NONINTERACTIVE must be unset so the installer does not skip
        with patch.dict("os.environ", {}, clear=False):
            for key in ("CI", "NONINTERACTIVE"):
                os.environ.pop(key, None)
            installer._install_system_packages(interactive=True)
        call_args = [call[0][0] for call in mock_run.call_args_list]
        assert not any("update" in str(cmd) for cmd in call_args)
        assert ["sudo", "dnf", "install", "-y", "some-package"] in call_args

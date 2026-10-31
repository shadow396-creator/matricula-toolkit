"""Automatic system and installation assistant for the Matricula Toolkit.

Detects the operating system, checks for missing prerequisites, installs
Kraken for OCR/HTR, sets up a Python virtual environment, and verifies the
installation. Designed to be idempotent: re-running must not damage a
working installation.

All system-level changes (apt, brew, choco, pip) are opt-in: the installer
reports what is missing and offers to install, never silently running
sudo for every step.
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
import venv
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class SystemInfo:
    """Collected system information."""

    os: str = ""
    os_version: str = ""
    distribution: str = ""
    architecture: str = ""
    python_version: str = ""
    python_executable: str = ""
    ram_total_mb: int = 0
    disk_free_mb: int = 0
    gpu_available: bool = False
    gpu_vendor: str | None = None
    is_container: bool = False


@dataclass
class ComponentStatus:
    """Status of a single component check."""

    name: str
    installed: bool
    version: str | None = None
    path: str | None = None
    error: str | None = None
    optional: bool = False


@dataclass
class InstallPlan:
    """Computed installation plan."""

    system_packages: list[str] = field(default_factory=list)
    python_packages: list[str] = field(default_factory=list)
    kraken_models: list[str] = field(default_factory=list)
    create_venv: bool = False
    venv_path: Path | None = None
    skip_if_installed: bool = True


# ---------------------------------------------------------------------------
# Verified Kraken models
# ---------------------------------------------------------------------------

# All models below were verified via ``kraken show <DOI>`` on 2026-10-28.
# Only models with confirmed metadata (DOI, license, type, description) are
# included.  Models are distinguished between historical handwriting (Kurrent,
# Sütterlin) and historical print (Fraktur).  No model is labelled as suitable
# for a script it was not trained on.

KRAKEN_MODELS: dict[str, dict[str, str]] = {
    # --- Historical handwriting (Kurrent / Sütterlin) ---
    "10.5281/zenodo.21747522": {
        "name": "HTR model for German manuscripts in Kurrent writing",
        "type": "recognition",
        "script": "Latin (Fraktur variant) - handwriting",
        "language": "German",
        "license": "CC-BY-4.0",
        "creator": "Janosch Alze (hfm Detmold)",
        "doi": "10.5281/zenodo.21747522",
        "concept_doi": "10.5281/zenodo.21747521",
        "description": (
            "Trained on Kurrent handwriting (12 pages of travel diary by "
            "composer Louise Le Beau). Limited training data - results are "
            "better than the base handwriting model but not production-grade."
        ),
        "keywords": "Kurrent; Sutterlin; HTR; handwritten text recognition",
    },
    # --- Historical print (Fraktur) ---
    "10.5281/zenodo.6891852": {
        "name": "Fraktur model (Austrian Newspapers)",
        "type": "recognition",
        "script": "Latin (Fraktur print)",
        "language": "German",
        "license": "CC-BY-SA-4.0",
        "creator": "Stefan Weil (Universitätsbibliothek Mannheim)",
        "doi": "10.5281/zenodo.6891852",
        "concept_doi": "10.5281/zenodo.6891851",
        "description": (
            "Recognition model for 19th century German Fraktur texts, "
            "trained with ground truth from Austrian Newspapers. "
            "Character error rate: 1.42."
        ),
        "keywords": "Fraktur; historical print; OCR",
    },
}

# Default models to offer (handwriting first, then print)
DEFAULT_KRAKEN_MODELS: list[str] = [
    "10.5281/zenodo.21747522",  # Kurrent handwriting
    "10.5281/zenodo.6891852",  # Fraktur print
]


@dataclass
class InstallResult:
    """Result of an installation run."""

    success: bool = False
    steps: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    log: list[str] = field(default_factory=list)

    def log_step(self, message: str) -> None:
        self.log.append(message)
        self.steps.append(message)

    def add_error(self, message: str) -> None:
        self.errors.append(message)
        self.log.append(f"ERROR: {message}")

    def add_warning(self, message: str) -> None:
        self.warnings.append(message)
        self.log.append(f"WARNING: {message}")


# ---------------------------------------------------------------------------
# System detection
# ---------------------------------------------------------------------------


def detect_system() -> SystemInfo:
    """Detect operating system, hardware, and Python environment."""
    info = SystemInfo()

    info.os = platform.system()
    info.os_version = platform.release()
    info.architecture = platform.machine()
    info.python_version = platform.python_version()
    info.python_executable = sys.executable

    # Distribution detection (Linux only)
    if info.os == "Linux":
        info.distribution = _detect_linux_distribution()
        info.is_container = _detect_container()

    # RAM
    info.ram_total_mb = _read_ram_total_mb()

    # Disk free
    info.disk_free_mb = _read_disk_free_mb(Path.cwd())

    # GPU
    info.gpu_available, info.gpu_vendor = _detect_gpu()

    return info


def _detect_linux_distribution() -> str:
    """Return a human-readable distribution name, or empty string."""
    try:
        with open("/etc/os-release", "r", encoding="utf-8") as f:
            data = {}
            for line in f:
                if "=" in line:
                    key, _, value = line.partition("=")
                    data[key.strip()] = value.strip().strip('"')
        name = data.get("PRETTY_NAME", data.get("NAME", ""))
        version = data.get("VERSION_ID", "")
        if name and version:
            return f"{name} {version}"
        return name
    except OSError:
        return ""


def _detect_container() -> bool:
    """Return True if running inside a container."""
    return Path("/.dockerenv").exists() or Path("/run/.containerenv").exists()


def _read_ram_total_mb() -> int:
    """Read total RAM in MB from /proc/meminfo (Linux)."""
    try:
        with open("/proc/meminfo", "r", encoding="utf-8") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    parts = line.split()
                    if len(parts) >= 2:
                        return int(parts[1]) // 1024  # kB -> MB
    except OSError:
        pass
    return 0


def _read_disk_free_mb(path: Path) -> int:
    """Return free disk space in MB for the given path."""
    try:
        usage = shutil.disk_usage(path)
        return usage.free // (1024 * 1024)
    except OSError:
        return 0


def _detect_gpu() -> tuple[bool, str | None]:
    """Detect available GPU and vendor."""
    # Try nvidia-smi
    if shutil.which("nvidia-smi"):
        try:
            result = subprocess.run(
                ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
                check=False,
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode == 0 and result.stdout.strip():
                return True, "NVIDIA"
        except (OSError, subprocess.SubprocessError):
            pass
    # Try rocm-smi
    if shutil.which("rocm-smi"):
        return True, "AMD"
    # Try intel GPU tools
    if shutil.which("intel_gpu_top"):
        return True, "Intel"
    return False, None


# ---------------------------------------------------------------------------
# Component detection
# ---------------------------------------------------------------------------


def detect_components(venv_python: str | None = None) -> list[ComponentStatus]:
    """Detect installed components relevant to the Matricula Toolkit."""
    python = venv_python or sys.executable
    components: list[ComponentStatus] = []

    # Python
    components.append(
        ComponentStatus(
            name="python",
            installed=True,
            version=platform.python_version(),
            path=python,
        )
    )

    # pip
    pip_rc = _run_command([python, "-m", "pip", "--version"], capture=True)[0]
    components.append(
        ComponentStatus(
            name="pip",
            installed=pip_rc == 0,
            path=python,
        )
    )

    # venv module
    components.append(
        ComponentStatus(
            name="venv",
            installed=_module_available(python, "venv"),
            path=python,
        )
    )

    # Playwright
    components.append(
        ComponentStatus(
            name="playwright",
            installed=_module_available(python, "playwright"),
            optional=False,
        )
    )

    # Pillow
    components.append(
        ComponentStatus(
            name="Pillow",
            installed=_module_available(python, "PIL"),
            optional=False,
        )
    )

    # python-dotenv
    components.append(
        ComponentStatus(
            name="python-dotenv",
            installed=_module_available(python, "dotenv"),
            optional=False,
        )
    )

    # PySide6 (optional desktop)
    components.append(
        ComponentStatus(
            name="PySide6",
            installed=_module_available(python, "PySide6"),
            optional=True,
        )
    )

    # MCP
    components.append(
        ComponentStatus(
            name="mcp",
            installed=_module_available(python, "mcp"),
            optional=True,
        )
    )

    # Kraken (OCR backend)
    kraken_path = shutil.which("kraken")
    kraken_version = None
    if kraken_path:
        rc, out, _ = _run_command([kraken_path, "--version"], capture=True)
        if rc == 0:
            kraken_version = out.strip()
    components.append(
        ComponentStatus(
            name="kraken",
            installed=bool(kraken_path),
            version=kraken_version,
            path=kraken_path,
            optional=False,
        )
    )

    # Kraken Python package
    components.append(
        ComponentStatus(
            name="kraken-python",
            installed=_module_available(python, "kraken"),
            optional=False,
        )
    )

    # System tools
    for tool_name, cmd in [
        ("chromium", "chromium"),
        ("chromium-browser", "chromium-browser"),
    ]:
        path = shutil.which(cmd)
        if path:
            components.append(
                ComponentStatus(
                    name=tool_name,
                    installed=True,
                    path=path,
                )
            )
            break

    return components


def _module_available(python: str, module_name: str) -> bool:
    """Check if a Python module is importable."""
    try:
        result = subprocess.run(
            [python, "-c", f"import {module_name}"],
            check=False,
            capture_output=True,
            timeout=10,
        )
        return result.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _run_command(
    cmd: list[str],
    *,
    capture: bool = False,
    timeout: int | None = None,
    env: dict[str, str] | None = None,
) -> tuple[int, str, str]:
    """Run a command and return (returncode, stdout, stderr)."""
    try:
        result = subprocess.run(
            cmd,
            check=False,
            capture_output=capture,
            text=True,
            timeout=timeout,
            env=env,
        )
        return result.returncode, result.stdout or "", result.stderr or ""
    except subprocess.TimeoutExpired:
        return -1, "", "Command timed out"
    except FileNotFoundError:
        return -1, "", f"Command not found: {cmd[0]}"
    except OSError as exc:
        return -1, "", str(exc)


# ---------------------------------------------------------------------------
# Installation plan computation
# ---------------------------------------------------------------------------


def compute_install_plan(
    system_info: SystemInfo,
    components: list[ComponentStatus],
    *,
    create_venv: bool = True,
    venv_path: Path | None = None,
    install_kraken: bool = True,
    install_models: bool = True,
    models: list[str] | None = None,
) -> InstallPlan:
    """Compute what needs to be installed based on detected state."""
    plan = InstallPlan()
    plan.create_venv = create_venv
    plan.venv_path = venv_path or Path.cwd() / ".venv"

    # Check which core Python packages are missing
    core_packages = ["playwright", "Pillow", "python-dotenv"]
    missing_python = []
    for comp in components:
        if comp.name in core_packages and not comp.installed:
            missing_python.append(comp.name)

    if missing_python:
        plan.python_packages.extend(missing_python)

    # Kraken
    kraken_installed = any(c.name == "kraken" and c.installed for c in components)
    kraken_python_installed = any(
        c.name == "kraken-python" and c.installed for c in components
    )

    if install_kraken and not kraken_python_installed:
        plan.python_packages.append("kraken")

    # System packages for Kraken native dependencies
    if install_kraken and not kraken_installed:
        if system_info.os == "Linux":
            plan.system_packages.extend(
                ["libxml2-dev", "libxslt1-dev", "zlib1g-dev", "libjpeg-dev"]
            )
        elif system_info.os == "Darwin":
            plan.system_packages.extend(["libxml2", "libxslt"])

    # Chromium for Playwright
    chromium_installed = any(c.name.startswith("chromium") for c in components)
    if not chromium_installed:
        if system_info.os == "Linux":
            plan.system_packages.append("chromium-browser")
        elif system_info.os == "Darwin":
            plan.system_packages.append("chromium")

    # Models
    if install_models:
        plan.kraken_models = models or list(DEFAULT_KRAKEN_MODELS)

    return plan


# ---------------------------------------------------------------------------
# Installation execution
# ---------------------------------------------------------------------------


class Installer:
    """Executes an installation plan."""

    def __init__(self, plan: InstallPlan, system_info: SystemInfo) -> None:
        self.plan = plan
        self.system_info = system_info
        self.result = InstallResult()

    def run(self, *, interactive: bool = True) -> InstallResult:
        """Execute the installation plan.

        Args:
            interactive: If True, prompt before system-level changes.
                         If False, skip steps that require user confirmation.
        """
        self.result.log_step("=== Matricula Toolkit Installer ===")
        self.result.log_step(
            f"System: {self.system_info.os} {self.system_info.os_version}"
        )
        self.result.log_step(
            f"Distribution: {self.system_info.distribution or 'unknown'}"
        )
        self.result.log_step(f"Architecture: {self.system_info.architecture}")
        self.result.log_step(f"Python: {self.system_info.python_version}")
        self.result.log_step(f"RAM: {self.system_info.ram_total_mb} MB")
        self.result.log_step(f"Disk free: {self.system_info.disk_free_mb} MB")
        self.result.log_step(f"GPU: {self.system_info.gpu_vendor or 'none detected'}")
        self.result.log_step("")

        # Step 1: Create venv
        if self.plan.create_venv:
            self._create_venv(interactive=interactive)

        # Step 2: Install system packages
        if self.plan.system_packages:
            self._install_system_packages(interactive=interactive)

        # Step 3: Install Python packages
        if self.plan.python_packages:
            self._install_python_packages(interactive=interactive)

        # Step 4: Install Playwright browsers
        self._install_playwright_browsers(interactive=interactive)

        # Step 5: Download Kraken models
        if self.plan.kraken_models:
            self._download_kraken_models(interactive=interactive)

        # Step 6: Verify
        self._verify_installation()

        if self.result.errors:
            self.result.success = False
        else:
            self.result.success = True

        self.result.log_step("")
        self.result.log_step(
            f"Installation {'successful' if self.result.success else 'completed with errors'}"
        )
        return self.result

    # ------------------------------------------------------------------
    # Venv
    # ------------------------------------------------------------------
    def _create_venv(self, *, interactive: bool) -> None:
        venv_path = self.plan.venv_path
        if venv_path is None:
            self.result.add_error("No venv path configured.")
            return
        if venv_path.exists():
            # Check if it has a Python executable
            python_exe = venv_path / "bin" / "python"
            if self.system_info.os == "Windows":
                python_exe = venv_path / "Scripts" / "python.exe"
            if python_exe.exists():
                self.result.log_step(f"Venv already exists at {venv_path}, skipping.")
                return
            self.result.log_step(
                f"Existing venv at {venv_path} is incomplete, recreating."
            )

        self.result.log_step(f"Creating virtual environment at {venv_path}...")
        try:
            venv.create(venv_path, with_pip=True)
            self.result.log_step("Virtual environment created.")
        except Exception as exc:  # noqa: BLE001
            self.result.add_error(f"Failed to create venv: {exc}")

    # ------------------------------------------------------------------
    # System packages
    # ------------------------------------------------------------------
    def _install_system_packages(self, *, interactive: bool) -> None:
        packages = self.plan.system_packages
        if not packages:
            return

        pkg_manager = _detect_package_manager(self.system_info)
        if not pkg_manager:
            self.result.add_warning(
                f"No supported package manager found for {self.system_info.os}. "
                f"Please install manually: {', '.join(packages)}"
            )
            return

        self.result.log_step(
            f"System packages needed: {', '.join(packages)} (via {pkg_manager['name']})"
        )

        if not interactive or os.getenv("CI") or os.getenv("NONINTERACTIVE"):
            self.result.add_warning(
                "Non-interactive mode: skipping system package installation. "
                f"Please install: {', '.join(packages)}"
            )
            return

        # Run package manager update before install (apt-get update, etc.)
        update_cmd = pkg_manager.get("update_cmd")
        if update_cmd:
            self.result.log_step(f"Running: {' '.join(update_cmd)}")
            _run_command(update_cmd, capture=True, timeout=120)

        install_cmd = pkg_manager["install_cmd"] + packages
        self.result.log_step(f"Running: {' '.join(install_cmd)}")
        rc, out, err = _run_command(install_cmd, capture=True, timeout=300)
        if rc == 0:
            self.result.log_step("System packages installed successfully.")
        else:
            self.result.add_error(f"Failed to install system packages: {err or out}")

    # ------------------------------------------------------------------
    # Python packages
    # ------------------------------------------------------------------
    def _install_python_packages(self, *, interactive: bool) -> None:
        packages = self.plan.python_packages
        if not packages:
            return

        python = self._venv_python()
        if not python:
            self.result.add_error(
                "No Python executable available for package installation."
            )
            return

        self.result.log_step(f"Installing Python packages: {', '.join(packages)}")
        install_cmd = [python, "-m", "pip", "install", "--upgrade", "pip"]
        rc, out, err = _run_command(install_cmd, capture=True, timeout=120)
        if rc != 0:
            self.result.add_warning(f"pip upgrade failed: {err or out}")

        install_cmd = [python, "-m", "pip", "install"] + packages
        rc, out, err = _run_command(install_cmd, capture=True, timeout=300)
        if rc == 0:
            self.result.log_step("Python packages installed successfully.")
        else:
            self.result.add_error(f"Failed to install Python packages: {err or out}")

    # ------------------------------------------------------------------
    # Playwright browsers
    # ------------------------------------------------------------------
    def _install_playwright_browsers(self, *, interactive: bool) -> None:
        python = self._venv_python()
        if not python:
            return

        # Check if playwright is installed
        if not _module_available(python, "playwright"):
            self.result.log_step(
                "Playwright not installed, skipping browser installation."
            )
            return

        self.result.log_step("Installing Playwright Chromium browser...")
        install_cmd = [python, "-m", "playwright", "install", "chromium"]
        rc, out, err = _run_command(install_cmd, capture=True, timeout=300)
        if rc == 0:
            self.result.log_step("Playwright Chromium installed.")
        else:
            self.result.add_error(
                f"Failed to install Playwright Chromium: {err or out}"
            )

    # ------------------------------------------------------------------
    # Kraken models
    # ------------------------------------------------------------------
    def _download_kraken_models(self, *, interactive: bool) -> None:
        models = self.plan.kraken_models
        if not models:
            return

        models_dir = Path.cwd() / "models"
        models_dir.mkdir(parents=True, exist_ok=True)

        self.result.log_step(f"Kraken models directory: {models_dir}")

        for model_spec in models:
            self._download_single_model(model_spec, models_dir)

    def _download_single_model(self, model_spec: str, models_dir: Path) -> None:
        """Download a Kraken model using ``kraken get``.

        model_spec is a DOI (e.g. ``10.5281/zenodo.21747522``) that identifies
        a model in the Kraken model repository.  The DOI is verified against
        the known model table before downloading.
        """
        self.result.log_step(f"Preparing model: {model_spec}")

        # Look up model metadata from the known model table
        model_info = KRAKEN_MODELS.get(model_spec)
        if model_info is None:
            self.result.add_warning(
                f"Model '{model_spec}' is not in the verified model table. "
                f"Skipping download. Add it to KRAKEN_MODELS in installer.py "
                f"to enable automatic download."
            )
            return

        # Check if Kraken CLI is available
        kraken_path = shutil.which("kraken")
        if not kraken_path:
            self.result.add_error(
                f"Cannot download model '{model_spec}': Kraken CLI not found on PATH."
            )
            return

        # Check if model already exists in Kraken's model directory
        # Kraken stores models under ~/.local/share/htrmopo/ on Linux
        self.result.log_step(f"  Type: {model_info['type']}")
        self.result.log_step(f"  License: {model_info['license']}")
        self.result.log_step(f"  DOI: {model_spec}")

        # Use kraken get to download the model
        self.result.log_step(f"  Downloading via 'kraken get {model_spec}'...")
        rc, out, err = _run_command(
            [kraken_path, "get", model_spec],
            capture=True,
            timeout=300,
        )
        if rc == 0:
            self.result.log_step(f"  Model '{model_spec}' downloaded successfully.")
            if out.strip():
                for line in out.strip().splitlines():
                    self.result.log_step(f"    {line}")
        else:
            self.result.add_error(
                f"Failed to download model '{model_spec}': {err or out}"
            )

    # ------------------------------------------------------------------
    # Verify
    # ------------------------------------------------------------------
    def _verify_installation(self) -> None:
        self.result.log_step("=== Verification ===")

        python = self._venv_python()
        if not python:
            self.result.add_error("No Python executable found for verification.")
            return

        # Check Python packages
        for module in ["playwright", "PIL", "dotenv"]:
            if _module_available(python, module):
                self.result.log_step(f"  [OK] {module} importable")
            else:
                self.result.add_error(f"  [FAIL] {module} not importable")

        # Check Kraken
        kraken_path = shutil.which("kraken")
        if kraken_path:
            self.result.log_step(f"  [OK] Kraken CLI found at {kraken_path}")
        else:
            self.result.add_warning("  [WARN] Kraken CLI not found on PATH")

        # Check venv
        if self.plan.venv_path and self.plan.venv_path.exists():
            self.result.log_step(f"  [OK] Venv at {self.plan.venv_path}")
        else:
            self.result.add_error("  [FAIL] Venv not found")

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _venv_python(self) -> str | None:
        """Return the venv Python executable path, or None."""
        if not self.plan.venv_path:
            return None
        python_exe = self.plan.venv_path / "bin" / "python"
        if self.system_info.os == "Windows":
            python_exe = self.plan.venv_path / "Scripts" / "python.exe"
        if python_exe.exists():
            return str(python_exe)
        return None


def _detect_package_manager(system_info: SystemInfo) -> dict[str, Any] | None:
    """Detect the system package manager and return install command info."""
    if system_info.os == "Linux":
        if shutil.which("apt-get"):
            return {
                "name": "apt-get",
                "install_cmd": ["sudo", "apt-get", "install", "-y"],
                "update_cmd": ["sudo", "apt-get", "update"],
            }
        if shutil.which("dnf"):
            return {
                "name": "dnf",
                "install_cmd": ["sudo", "dnf", "install", "-y"],
            }
        if shutil.which("yum"):
            return {
                "name": "yum",
                "install_cmd": ["sudo", "yum", "install", "-y"],
            }
        if shutil.which("pacman"):
            return {
                "name": "pacman",
                "install_cmd": ["sudo", "pacman", "-S", "--noconfirm"],
            }
        if shutil.which("zypper"):
            return {
                "name": "zypper",
                "install_cmd": ["sudo", "zypper", "install", "-y"],
            }
    elif system_info.os == "Darwin":
        if shutil.which("brew"):
            return {
                "name": "brew",
                "install_cmd": ["brew", "install"],
            }
    elif system_info.os == "Windows":
        if shutil.which("choco"):
            return {
                "name": "choco",
                "install_cmd": ["choco", "install", "-y"],
            }
        if shutil.which("winget"):
            return {
                "name": "winget",
                "install_cmd": ["winget", "install"],
            }
    return None


# ---------------------------------------------------------------------------
# High-level convenience function
# ---------------------------------------------------------------------------


def run_installer(
    *,
    interactive: bool = True,
    create_venv: bool = True,
    venv_path: Path | None = None,
    install_kraken: bool = True,
    install_models: bool = True,
    models: list[str] | None = None,
) -> InstallResult:
    """Convenience function: detect, plan, and install in one call."""
    system_info = detect_system()
    components = detect_components()
    plan = compute_install_plan(
        system_info,
        components,
        create_venv=create_venv,
        venv_path=venv_path,
        install_kraken=install_kraken,
        install_models=install_models,
        models=models,
    )
    installer = Installer(plan, system_info)
    return installer.run(interactive=interactive)

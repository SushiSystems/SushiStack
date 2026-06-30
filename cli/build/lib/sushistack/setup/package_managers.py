"""Package-manager abstractions.

Each concrete manager wraps one underlying tool (apt, winget, vcpkg, dnf, …).
Steps depend only on :class:`IPackageManager`, so adding a platform or tool
means adding a class here — no step changes required (Open-Closed + Dependency
Inversion).

Linux managers expose :meth:`translate_apt` so the step can convert apt-format
package names from ``dependencies.toml`` into distro-native names without
widening the manifest schema.
"""

from __future__ import annotations

import fnmatch
import json
import os
import shutil
import subprocess
import tempfile
import urllib.request
import zipfile
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Callable

from .. import console
from ..config import Config, deps_dir
from .probe import binary_works as _binary_works


# --------------------------------------------------------------------------- #
# Internal helpers
# --------------------------------------------------------------------------- #

def _run(cmd: list[str], dry_run: bool, *, check: bool = False) -> int:
    console.command(subprocess.list2cmdline(cmd))
    if dry_run:
        console.info("(dry-run) not executed")
        return 0
    process = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace",
    )
    if process.stdout:
        for line in iter(process.stdout.readline, ""):
            console.console.print(line.rstrip("\n"), markup=False, highlight=False)
    process.wait()
    if check and process.returncode != 0:
        console.error(f"Command failed ({process.returncode}).")
    return process.returncode


def _is_root() -> bool:
    try:
        return os.geteuid() == 0  # type: ignore[attr-defined]
    except AttributeError:
        return False


def _tools_dir() -> Path:
    """Directory for portable tools (cmake, ninja) under the deps folder."""
    return deps_dir() / "tools"


def _gh_latest_asset(repo: str, asset_glob: str) -> str:
    """Return the download URL for the first release asset matching *asset_glob*."""
    url = f"https://api.github.com/repos/{repo}/releases/latest"
    req = urllib.request.Request(url, headers={"User-Agent": "sushiruntime-installer"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read())
    for asset in data.get("assets", []):
        if fnmatch.fnmatch(asset["name"], asset_glob):
            return asset["browser_download_url"]
    raise RuntimeError(f"No asset matching '{asset_glob}' in {repo} latest release.")


def _gh_tagged_asset(repo: str, tag: str, asset_glob: str) -> str:
    """Return the download URL for an asset of a specific release *tag*."""
    url = f"https://api.github.com/repos/{repo}/releases/tags/{tag}"
    req = urllib.request.Request(url, headers={"User-Agent": "sushiruntime-installer"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read())
    for asset in data.get("assets", []):
        if fnmatch.fnmatch(asset["name"], asset_glob):
            return asset["browser_download_url"]
    raise RuntimeError(f"No asset matching '{asset_glob}' in {repo} {tag}.")


def _download(url: str, dest: Path) -> None:
    console.info(f"Downloading {dest.name} ...")
    req = urllib.request.Request(url, headers={"User-Agent": "sushiruntime-installer"})
    with urllib.request.urlopen(req, timeout=300) as resp, open(dest, "wb") as fh:
        while chunk := resp.read(1 << 16):
            fh.write(chunk)


def _add_to_user_path_windows(directory: str) -> None:
    """Persistently append *directory* to the current user's PATH registry key."""
    try:
        import winreg
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, r"Environment",
            0, winreg.KEY_READ | winreg.KEY_WRITE,
        )
        try:
            current, _ = winreg.QueryValueEx(key, "Path")
        except FileNotFoundError:
            current = ""
        if directory.lower() not in current.lower():
            new_path = f"{current};{directory}" if current else directory
            winreg.SetValueEx(key, "Path", 0, winreg.REG_EXPAND_SZ, new_path)
        winreg.CloseKey(key)
        os.environ["PATH"] = os.environ.get("PATH", "") + os.pathsep + directory
    except Exception:
        pass  # Non-fatal; session PATH is updated by the PowerShell wrapper.


def refresh_windows_path() -> None:
    """Reload PATH from the registry into this process (Windows only, else no-op).

    Installers (winget, MSI, our direct downloads) update the Machine/User PATH
    in the registry, but an already-running process keeps the PATH it inherited.
    Without this refresh a tool installed moments earlier in the same `sr setup`
    run (e.g. CMake under Program Files) stays invisible to ``shutil.which``,
    which causes needless reinstalls and false 'missing' reports — and a failed
    reinstall then aborts the whole pipeline.
    """
    if os.name != "nt":
        return
    try:
        import winreg
        parts: list[str] = []
        for root, sub in (
            (winreg.HKEY_LOCAL_MACHINE,
             r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"),
            (winreg.HKEY_CURRENT_USER, r"Environment"),
        ):
            try:
                key = winreg.OpenKey(root, sub)
                try:
                    val, _ = winreg.QueryValueEx(key, "Path")
                    parts.append(os.path.expandvars(str(val)))
                finally:
                    winreg.CloseKey(key)
            except FileNotFoundError:
                pass
        parts.append(os.environ.get("PATH", ""))  # keep this process's own additions
        seen: set[str] = set()
        out: list[str] = []
        for entry in os.pathsep.join(parts).split(os.pathsep):
            key_l = entry.lower()
            if entry and key_l not in seen:
                seen.add(key_l)
                out.append(entry)
        os.environ["PATH"] = os.pathsep.join(out)
    except Exception:
        pass


def _cmake_portable_bin() -> Path:
    """bin/ of the portable CMake the direct-download manager extracts."""
    return _tools_dir() / "cmake" / "bin"


def _cmake_on_system() -> str:
    """Resolve a usable cmake after a refresh: PATH, Program Files, portable dir."""
    refresh_windows_path()
    found = shutil.which("cmake")
    if found:
        return found
    candidates = [
        r"C:\Program Files\CMake\bin\cmake.exe",
        r"C:\Program Files (x86)\CMake\bin\cmake.exe",
        str(_cmake_portable_bin() / "cmake.exe"),
    ]
    for base in candidates:
        if Path(base).is_file():
            return base
    return ""


# Direct-download install functions (Windows only) --------------------------- #

def _install_cmake_direct() -> bool:
    # Use the portable .zip, not the .msi: the CMake MSI requires administrator
    # rights (it writes to Program Files and the system PATH), but the default
    # non-oneAPI install runs as a normal user. Extracting the zip into the tools
    # dir and adding it to the user PATH needs no elevation — mirroring ninja.
    if _cmake_on_system():
        console.info("cmake: already present, skipping.")
        return True
    try:
        url = _gh_latest_asset("Kitware/CMake", "*windows-x86_64.zip")
        with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as f:
            zip_dest = Path(f.name)
        _download(url, zip_dest)
        target = _tools_dir() / "cmake"
        console.info(f"Extracting CMake to {target} ...")
        with tempfile.TemporaryDirectory() as staging:
            stage = Path(staging)
            with zipfile.ZipFile(zip_dest) as zf:
                zf.extractall(stage)
            entries = list(stage.iterdir())
            srcroot = entries[0] if len(entries) == 1 and entries[0].is_dir() else stage
            if target.exists():
                shutil.rmtree(target, ignore_errors=True)
            shutil.move(str(srcroot), str(target))
        zip_dest.unlink(missing_ok=True)
        _add_to_user_path_windows(str(_cmake_portable_bin()))
        if (_cmake_portable_bin() / "cmake.exe").is_file():
            return True
        console.error("CMake not found after extracting the portable archive.")
        return False
    except Exception as exc:
        console.error(f"CMake direct install failed: {exc}")
        return False


def _install_ninja_direct() -> bool:
    try:
        url = _gh_latest_asset("ninja-build/ninja", "ninja-win.zip")
        with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as f:
            dest = Path(f.name)
        _download(url, dest)
        tools = _tools_dir()
        tools.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(dest) as zf:
            zf.extract("ninja.exe", tools)
        dest.unlink(missing_ok=True)
        _add_to_user_path_windows(str(tools))
        return (tools / "ninja.exe").is_file()
    except Exception as exc:
        console.error(f"Ninja direct install failed: {exc}")
        return False


def _install_doxygen_direct() -> bool:
    target = _tools_dir() / "doxygen"
    exe = target / "doxygen.exe"
    if exe.is_file():
        console.info("doxygen: already present in deps/tools, skipping.")
        return True
    try:
        url = _gh_latest_asset("doxygen/doxygen", "doxygen-*.windows.x64.bin.zip")
        with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as f:
            zip_dest = Path(f.name)
        _download(url, zip_dest)
        console.info(f"Extracting Doxygen to {target} ...")
        with tempfile.TemporaryDirectory() as staging:
            stage = Path(staging)
            with zipfile.ZipFile(zip_dest) as zf:
                zf.extractall(stage)
            entries = list(stage.iterdir())
            srcroot = entries[0] if len(entries) == 1 and entries[0].is_dir() else stage
            if target.exists():
                shutil.rmtree(target, ignore_errors=True)
            shutil.move(str(srcroot), str(target))
        zip_dest.unlink(missing_ok=True)
        _add_to_user_path_windows(str(target))
        if exe.is_file():
            return True
        console.error("doxygen.exe not found after extracting the portable archive.")
        return False
    except Exception as exc:
        console.error(f"Doxygen direct install failed: {exc}")
        return False


def _install_git_direct() -> bool:
    try:
        url = _gh_latest_asset("git-for-windows/git", "*64-bit.exe")
        with tempfile.NamedTemporaryFile(suffix=".exe", delete=False) as f:
            dest = Path(f.name)
        _download(url, dest)
        console.info("Installing Git silently ...")
        rc = subprocess.run(
            [str(dest), "/VERYSILENT", "/NORESTART", "/NOCANCEL", "/SP-",
             "/CLOSEAPPLICATIONS", "/RESTARTAPPLICATIONS",
             "/COMPONENTS=icons,ext\\reg\\shellhere,assoc,assoc_sh"],
            timeout=300,
        ).returncode
        dest.unlink(missing_ok=True)
        return rc == 0
    except Exception as exc:
        console.error(f"Git direct install failed: {exc}")
        return False


# --------------------------------------------------------------------------- #
# Apt → native package name translation tables
# --------------------------------------------------------------------------- #

_APT_TO_DNF: dict[str, list[str]] = {
    "build-essential":     ["gcc", "gcc-c++", "make"],
    "ninja-build":         ["ninja-build"],
    "libhwloc-dev":        ["hwloc-devel"],
    "libgtest-dev":        ["gtest-devel"],
    "ocl-icd-opencl-dev":  ["ocl-icd"],
    "ocl-icd-libopencl1":  [],
    "pkg-config":          ["pkgconf"],
}

_APT_TO_PACMAN: dict[str, list[str]] = {
    "build-essential":     ["base-devel"],
    "ninja-build":         ["ninja"],
    "libhwloc-dev":        ["hwloc"],
    "libgtest-dev":        ["gtest"],
    "ocl-icd-opencl-dev":  ["ocl-icd"],
    "ocl-icd-libopencl1":  [],
    "pkg-config":          ["pkgconf"],
}

_APT_TO_ZYPPER: dict[str, list[str]] = {
    "build-essential":     ["gcc", "gcc-c++", "make"],
    "ninja-build":         ["ninja"],
    "libhwloc-dev":        ["hwloc-devel"],
    "libgtest-dev":        ["gtest"],
    "ocl-icd-opencl-dev":  ["ocl-icd"],
    "ocl-icd-libopencl1":  [],
    "pkg-config":          ["pkgconf"],
}


def _translate(apt_pkgs: list[str], table: dict[str, list[str]]) -> list[str]:
    out: list[str] = []
    for pkg in apt_pkgs:
        out.extend(table.get(pkg, [pkg]))
    return out


# --------------------------------------------------------------------------- #
# Abstract base
# --------------------------------------------------------------------------- #

class IPackageManager(ABC):
    """Installs packages of one kind and reports availability."""

    name: str = "pkg"

    @abstractmethod
    def available(self) -> bool:
        """Is the underlying tool present on this machine?"""

    @abstractmethod
    def is_installed(self, pkg: str) -> bool:
        """Best-effort check whether *pkg* is already installed."""

    @abstractmethod
    def install(self, pkgs: list[str], dry_run: bool) -> bool:
        """Install the given packages. Return True on success."""

    def translate_apt(self, apt_pkgs: list[str]) -> list[str]:
        """Convert apt-format package names to this manager's native names.

        The default implementation is an identity pass (apt names work as-is).
        Non-apt managers override this.
        """
        return apt_pkgs

    def remove(self, pkgs: list[str], dry_run: bool) -> bool:
        """Remove the given packages. Return True on success (best-effort)."""
        console.warn(f"{self.name}: remove not implemented, skipping.")
        return True


# --------------------------------------------------------------------------- #
# Linux managers
# --------------------------------------------------------------------------- #

class AptManager(IPackageManager):
    """Debian/Ubuntu apt."""

    name = "apt"

    def available(self) -> bool:
        return shutil.which("apt-get") is not None

    def is_installed(self, pkg: str) -> bool:
        try:
            result = subprocess.run(
                ["dpkg-query", "-W", "-f=${Status}", pkg],
                capture_output=True, text=True,
            )
        except OSError:
            return False
        return result.returncode == 0 and "install ok installed" in result.stdout

    def install(self, pkgs: list[str], dry_run: bool) -> bool:
        if not pkgs:
            return True
        sudo = [] if _is_root() else ["sudo"]
        if _run([*sudo, "apt-get", "update"], dry_run) != 0 and not dry_run:
            console.warn("apt-get update failed; continuing anyway.")
        rc = _run([*sudo, "apt-get", "install", "-y", *pkgs], dry_run, check=True)
        return rc == 0

    def remove(self, pkgs: list[str], dry_run: bool) -> bool:
        if not pkgs:
            return True
        sudo = [] if _is_root() else ["sudo"]
        rc = _run([*sudo, "apt-get", "remove", "-y", *pkgs], dry_run, check=True)
        return rc == 0


class DnfManager(IPackageManager):
    """Fedora / RHEL dnf."""

    name = "dnf"

    def available(self) -> bool:
        return shutil.which("dnf") is not None

    def is_installed(self, pkg: str) -> bool:
        try:
            result = subprocess.run(
                ["rpm", "-q", pkg], capture_output=True, text=True,
            )
            return result.returncode == 0
        except OSError:
            return False

    def translate_apt(self, apt_pkgs: list[str]) -> list[str]:
        return _translate(apt_pkgs, _APT_TO_DNF)

    def install(self, pkgs: list[str], dry_run: bool) -> bool:
        if not pkgs:
            return True
        sudo = [] if _is_root() else ["sudo"]
        rc = _run([*sudo, "dnf", "install", "-y", *pkgs], dry_run, check=True)
        return rc == 0

    def remove(self, pkgs: list[str], dry_run: bool) -> bool:
        if not pkgs:
            return True
        sudo = [] if _is_root() else ["sudo"]
        rc = _run([*sudo, "dnf", "remove", "-y", *pkgs], dry_run, check=True)
        return rc == 0


class YumManager(IPackageManager):
    """Legacy RHEL/CentOS yum."""

    name = "yum"

    def available(self) -> bool:
        return shutil.which("yum") is not None and shutil.which("dnf") is None

    def is_installed(self, pkg: str) -> bool:
        try:
            result = subprocess.run(
                ["rpm", "-q", pkg], capture_output=True, text=True,
            )
            return result.returncode == 0
        except OSError:
            return False

    def translate_apt(self, apt_pkgs: list[str]) -> list[str]:
        return _translate(apt_pkgs, _APT_TO_DNF)

    def install(self, pkgs: list[str], dry_run: bool) -> bool:
        if not pkgs:
            return True
        sudo = [] if _is_root() else ["sudo"]
        rc = _run([*sudo, "yum", "install", "-y", *pkgs], dry_run, check=True)
        return rc == 0

    def remove(self, pkgs: list[str], dry_run: bool) -> bool:
        if not pkgs:
            return True
        sudo = [] if _is_root() else ["sudo"]
        rc = _run([*sudo, "yum", "remove", "-y", *pkgs], dry_run, check=True)
        return rc == 0


class PacmanManager(IPackageManager):
    """Arch Linux pacman."""

    name = "pacman"

    def available(self) -> bool:
        return shutil.which("pacman") is not None

    def is_installed(self, pkg: str) -> bool:
        try:
            result = subprocess.run(
                ["pacman", "-Q", pkg], capture_output=True, text=True,
            )
            return result.returncode == 0
        except OSError:
            return False

    def translate_apt(self, apt_pkgs: list[str]) -> list[str]:
        return _translate(apt_pkgs, _APT_TO_PACMAN)

    def install(self, pkgs: list[str], dry_run: bool) -> bool:
        if not pkgs:
            return True
        sudo = [] if _is_root() else ["sudo"]
        rc = _run([*sudo, "pacman", "-Sy", "--noconfirm", *pkgs], dry_run, check=True)
        return rc == 0

    def remove(self, pkgs: list[str], dry_run: bool) -> bool:
        if not pkgs:
            return True
        sudo = [] if _is_root() else ["sudo"]
        rc = _run([*sudo, "pacman", "-R", "--noconfirm", *pkgs], dry_run, check=True)
        return rc == 0


class ZypperManager(IPackageManager):
    """openSUSE zypper."""

    name = "zypper"

    def available(self) -> bool:
        return shutil.which("zypper") is not None

    def is_installed(self, pkg: str) -> bool:
        try:
            result = subprocess.run(
                ["rpm", "-q", pkg], capture_output=True, text=True,
            )
            return result.returncode == 0
        except OSError:
            return False

    def translate_apt(self, apt_pkgs: list[str]) -> list[str]:
        return _translate(apt_pkgs, _APT_TO_ZYPPER)

    def install(self, pkgs: list[str], dry_run: bool) -> bool:
        if not pkgs:
            return True
        sudo = [] if _is_root() else ["sudo"]
        rc = _run([*sudo, "zypper", "install", "-y", *pkgs], dry_run, check=True)
        return rc == 0

    def remove(self, pkgs: list[str], dry_run: bool) -> bool:
        if not pkgs:
            return True
        sudo = [] if _is_root() else ["sudo"]
        rc = _run([*sudo, "zypper", "remove", "-y", *pkgs], dry_run, check=True)
        return rc == 0


# --------------------------------------------------------------------------- #
# Windows managers
# --------------------------------------------------------------------------- #

class WingetManager(IPackageManager):
    """Windows winget for system toolchain packages."""

    name = "winget"

    def available(self) -> bool:
        return shutil.which("winget") is not None

    def is_installed(self, pkg: str) -> bool:
        try:
            result = subprocess.run(
                ["winget", "list", "--id", pkg, "-e"],
                capture_output=True, text=True,
            )
        except OSError:
            return False
        return result.returncode == 0 and pkg.lower() in result.stdout.lower()

    def install(self, pkgs: list[str], dry_run: bool) -> bool:
        ok = True
        for pkg in pkgs:
            rc = _run(
                ["winget", "install", "--id", pkg, "-e",
                 "--accept-package-agreements", "--accept-source-agreements",
                 "--silent"],
                dry_run, check=True,
            )
            ok = ok and rc == 0
        return ok

    def remove(self, pkgs: list[str], dry_run: bool) -> bool:
        ok = True
        for pkg in pkgs:
            rc = _run(
                ["winget", "uninstall", "--id", pkg, "-e", "--silent"],
                dry_run,
            )
            ok = ok and rc == 0
        return ok


# Maps winget package IDs to the command-line tool they provide.
WINGET_ID_TO_CMD: dict[str, str] = {
    "Kitware.CMake":              "cmake",
    "Ninja-build.Ninja":          "ninja",
    "Git.Git":                    "git",
    "DimitriVanHeesch.Doxygen":   "doxygen",
}

# Maps winget IDs to direct-download installer functions.
_DIRECT_RECIPES: dict[str, Callable[[], bool]] = {
    "Kitware.CMake":             _install_cmake_direct,
    "Ninja-build.Ninja":         _install_ninja_direct,
    "Git.Git":                   _install_git_direct,
    "DimitriVanHeesch.Doxygen":  _install_doxygen_direct,
}


class DirectDownloadWindowsManager(IPackageManager):
    """Installs cmake, ninja, and git via direct downloads when winget is absent.

    Fetches the latest release from GitHub (or python.org for Python) and runs
    a silent installer, so no Microsoft Store or App Installer is required.
    """

    name = "direct-download"

    def available(self) -> bool:
        return True

    def is_installed(self, pkg: str) -> bool:
        cmd = WINGET_ID_TO_CMD.get(pkg)
        path = shutil.which(cmd) if cmd else None
        return _binary_works(path) if path else False

    def install(self, pkgs: list[str], dry_run: bool) -> bool:
        refresh_windows_path()  # see a tool installed earlier this run
        ok = True
        for pkg in pkgs:
            cmd = WINGET_ID_TO_CMD.get(pkg)
            if cmd and (w := shutil.which(cmd)) and _binary_works(w):
                console.info(f"{cmd}: already on PATH, skipping.")
                continue
            recipe = _DIRECT_RECIPES.get(pkg)
            if recipe is None:
                console.warn(f"direct-download: no recipe for '{pkg}', skipping.")
                continue
            if dry_run:
                console.info(f"(dry-run) would download and install {pkg}.")
                continue
            ok = recipe() and ok
        return ok


class VcpkgManager(IPackageManager):
    """Windows vcpkg for C++ library ports (hwloc, gtest, pkgconf, …)."""

    name = "vcpkg"

    def __init__(self, cfg: Config) -> None:
        self._cfg = cfg
        self._triplet = cfg.vcpkg_triplet or "x64-windows"

    def _root(self) -> Path:
        root = self._cfg.expand(self._cfg.vcpkg_root) if self._cfg.vcpkg_root else ""
        if root:
            return Path(root)
        # Self-contained default: vcpkg lives under the deps folder so the whole
        # install is one deletable directory.
        return deps_dir() / "vcpkg"

    def _exe(self) -> Path:
        return self._root() / "vcpkg.exe"

    def available(self) -> bool:
        return self._exe().is_file() or shutil.which("vcpkg") is not None

    def ensure_bootstrapped(self, dry_run: bool) -> bool:
        root = self._root()
        if self._exe().is_file():
            return True
        console.info(f"vcpkg not found; bootstrapping into {root} ...")
        if not (root / ".git").is_dir():
            if root.exists():
                import shutil as _shutil
                console.info(f"Removing stale directory {root} before cloning ...")
                if not dry_run:
                    _shutil.rmtree(root)
            rc = _run(["git", "clone", "https://github.com/microsoft/vcpkg.git",
                       str(root)], dry_run, check=True)
            if rc != 0 and not dry_run:
                return False
        bootstrap = root / "bootstrap-vcpkg.bat"
        rc = _run(["cmd", "/c", str(bootstrap), "-disableMetrics"], dry_run, check=True)
        return rc == 0 or dry_run

    def is_installed(self, pkg: str) -> bool:
        if not self.available():
            return False
        exe = self._exe() if self._exe().is_file() else Path("vcpkg")
        try:
            result = subprocess.run(
                [str(exe), "list", "--triplet", self._triplet],
                capture_output=True, text=True,
            )
        except OSError:
            return False
        return result.returncode == 0 and pkg.lower() in result.stdout.lower()

    def install(self, pkgs: list[str], dry_run: bool) -> bool:
        if not pkgs:
            return True
        if not self.ensure_bootstrapped(dry_run):
            console.error("vcpkg bootstrap failed.")
            return False
        exe = self._exe()
        ports = [f"{p}:{self._triplet}" for p in pkgs]
        rc = _run([str(exe), "install", *ports], dry_run, check=True)
        return rc == 0

    def remove(self, pkgs: list[str], dry_run: bool) -> bool:
        if not pkgs:
            return True
        exe = self._exe()
        if not exe.is_file():
            console.warn("vcpkg not found; skipping port removal.")
            return True
        ports = [f"{p}:{self._triplet}" for p in pkgs]
        rc = _run([str(exe), "remove", *ports], dry_run, check=True)
        return rc == 0

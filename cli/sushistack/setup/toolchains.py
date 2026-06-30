"""SYCL toolchain installers.

These mirror the Dockerfile's toolchain provisioning for a host install:

* ``install_intel_llvm`` downloads a pre-built intel/llvm nightly SYCL bundle
  (clang++ -fsycl) — the lean, vendor-neutral primary toolchain (Dockerfile
  lines 36-45).
* ``install_adaptivecpp`` builds AdaptiveCpp (acpp) from source, the secondary
  fully open-source toolchain (Dockerfile lines 52-76).

Intel oneAPI (icx/icpx) is handled separately in ``steps.py`` because it is
opt-in (``--oneapi``) and heavy.

Each installer returns the resolved path the CLI config needs (the bundle root
for intel-llvm, the ``acpp`` executable for adaptivecpp) or ``None`` on failure,
so the caller can record it in ``config.local.toml`` and degrade gracefully —
the intel-llvm path is enough to build, so an acpp build failure is non-fatal.
"""

from __future__ import annotations

import glob as _glob
import os
import shutil
import subprocess
import tarfile
import tempfile
from pathlib import Path

from .. import console
from ..config import Config, deps_dir
from .package_managers import (
    IPackageManager,
    _download,
    _gh_latest_asset,
    _gh_tagged_asset,
    _run,
)


def _run_quiet(cmd: list[str], dry_run: bool) -> bool:
    """Run *cmd* silently; on failure print the last 20 lines of output."""
    console.command(subprocess.list2cmdline(cmd))
    if dry_run:
        console.info("(dry-run) not executed")
        return True
    result = subprocess.run(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace",
    )
    if result.returncode != 0:
        lines = (result.stdout or "").splitlines()
        for line in lines[-20:]:
            console.console.print(line, markup=False, highlight=False)
    return result.returncode == 0

# AdaptiveCpp release pinned to match the Dockerfile (ADAPTIVECPP_VERSION).
ACPP_VERSION = "v24.10.0"
# clang/llvm major version used to build acpp (Dockerfile ACPP_LLVM).
ACPP_LLVM = "17"
# Full LLVM release vendored on Windows when no LLVM dev install exists. Matches
# ACPP_LLVM. The official clang+llvm Windows tarball ships lib/cmake/llvm.
LLVM_WINDOWS_VERSION = "17.0.6"
# Seconds to wait for consent before downloading the heavy LLVM (default: no).
_LLVM_CONSENT_TIMEOUT = 30


def _confirm_timeout(message: str, timeout: int = _LLVM_CONSENT_TIMEOUT,
                     default: bool = False) -> bool:
    """Ask a yes/no question, returning *default* if unanswered within *timeout*.

    Prints *message* then a standardized ``[y/n]`` hint (the brackets are escaped
    so Rich does not eat them as markup). Used to gate the heavy (~2-3 GB) LLVM
    download behind explicit consent. A non-interactive stdin (piped install, CI)
    yields *default* immediately, so an unattended run never blocks.

    Must be called *outside* any Rich progress/live context — a prompt rendered
    under a spinner is not answerable.
    """
    import threading

    console.console.print(message)
    hint = r"\[y/n]" if default is False else r"\[Y/n]"
    console.console.print(
        f"[bold]Your choice {hint}[/bold] (auto-"
        f"{'yes' if default else 'no'} in {timeout}s): ", end="")
    result = [default]

    def _read() -> None:
        try:
            answer = input().strip().lower()
            result[0] = answer in ("y", "yes", "e", "evet")
        except (EOFError, OSError):
            pass  # non-interactive: keep the default

    thread = threading.Thread(target=_read, daemon=True)
    thread.start()
    thread.join(timeout)
    if thread.is_alive():
        console.console.print()  # finish the prompt line
        console.info(f"No answer in {timeout}s — defaulting to "
                     f"{'yes' if default else 'no'}.")
    return result[0]


def _find_windows_sdk_rc_dir() -> str:
    """Return the directory containing rc.exe from the Windows 10/11 SDK, or ''."""
    for pat in [
        r"C:/Program Files (x86)/Windows Kits/10/bin/*/x64/rc.exe",
        r"C:/Program Files/Windows Kits/10/bin/*/x64/rc.exe",
    ]:
        hits = sorted(_glob.glob(pat))
        if hits:
            return str(Path(hits[-1]).parent)
    return ""


def _find_windows_llvm() -> tuple[str, str] | None:
    """Return (LLVM_DIR, clang_prefix) for an existing Windows LLVM, else None."""
    bases = [
        deps_dir() / "tools" / "llvm",
        Path(r"C:/Program Files/LLVM"),
        Path(r"C:/Program Files (x86)/LLVM"),
    ]
    for base in bases:
        cm = base / "lib" / "cmake" / "llvm"
        if cm.is_dir():
            return (str(cm), str(base))
    return None


def toolchains_dir() -> Path:
    """Base directory for SYCL toolchains installed by ``sr setup``."""
    return deps_dir() / "toolchains"


# --------------------------------------------------------------------------- #
# intel/llvm nightly bundle
# --------------------------------------------------------------------------- #

def install_intel_llvm(cfg: Config, dry_run: bool) -> str | None:
    """Download + extract the intel/llvm SYCL bundle. Return its root dir.

    The clang++ inside is the intel-llvm toolchain compiler. Already-present
    installs are reused (idempotent), so re-running setup is cheap.
    """
    root = toolchains_dir() / "llvm-sycl"
    clang = root / "bin" / ("clang++.exe" if cfg.is_windows else "clang++")
    if clang.is_file():
        console.info(f"intel/llvm bundle already present: {root}")
        return str(root)

    asset = "sycl_windows.tar.gz" if cfg.is_windows else "sycl_linux.tar.gz"
    if dry_run:
        console.info(f"(dry-run) would download intel/llvm '{asset}' to {root}")
        return str(root)

    try:
        url = _gh_latest_asset("intel/llvm", asset)
    except Exception as exc:
        console.error(f"Could not resolve intel/llvm release asset: {exc}")
        return None

    with tempfile.TemporaryDirectory() as tmp:
        archive = Path(tmp) / asset
        try:
            console.info("Downloading intel/llvm SYCL bundle (~300-400 MB) ...")
            _download(url, archive)
            _extract_tar_gz(archive, root)
        except Exception as exc:
            console.error(f"intel/llvm bundle install failed: {exc}")
            shutil.rmtree(root, ignore_errors=True)
            return None

    if clang.is_file():
        console.success(f"intel/llvm bundle installed: {root}")
        return str(root)
    console.error(f"intel/llvm bundle extracted but {clang.name} is missing.")
    return None


def _extract_tar_gz(archive: Path, dest: Path) -> None:
    """Extract *archive* into *dest*, collapsing a single top-level wrapper dir.

    The intel/llvm tarballs wrap everything in one directory (the Dockerfile
    strips it with ``--strip-components=1``); some assets do not. Extracting to a
    temp dir first lets us detect either layout and end up with bin/ and lib/ at
    the root of *dest*.
    """
    dest.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as staging:
        stage = Path(staging)
        with tarfile.open(archive, "r:gz") as tf:
            try:
                tf.extractall(stage, filter="data")  # py3.12+: path-traversal safe
            except TypeError:
                tf.extractall(stage)  # older Python: no filter kwarg
        entries = list(stage.iterdir())
        srcroot = entries[0] if len(entries) == 1 and entries[0].is_dir() else stage
        for item in srcroot.iterdir():
            target = dest / item.name
            if target.exists():
                shutil.rmtree(target) if target.is_dir() else target.unlink()
            shutil.move(str(item), str(target))


def _extract_tarball(archive: Path, dest: Path) -> None:
    """Extract any tar archive (gz/xz/...) into *dest*, collapsing a top wrapper.

    Like ``_extract_tar_gz`` but autodetects the compression (``r:*``), so it
    handles the LLVM ``clang+llvm-*.tar.xz`` releases as well.
    """
    dest.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as staging:
        stage = Path(staging)
        with tarfile.open(archive, "r:*") as tf:
            try:
                tf.extractall(stage, filter="data")
            except TypeError:
                tf.extractall(stage)
        entries = list(stage.iterdir())
        srcroot = entries[0] if len(entries) == 1 and entries[0].is_dir() else stage
        for item in srcroot.iterdir():
            target = dest / item.name
            if target.exists():
                shutil.rmtree(target) if target.is_dir() else target.unlink()
            shutil.move(str(item), str(target))


def _vendor_llvm_windows() -> tuple[str, str] | None:
    """Download the official clang+llvm Windows tarball into deps/tools/llvm.

    Returns (LLVM_DIR, clang_prefix) for the vendored install, or None on
    failure. Keeps acpp's heavy LLVM dependency inside the one deps folder
    instead of relying on winget/Program Files.
    """
    dest = deps_dir() / "tools" / "llvm"
    tag = f"llvmorg-{LLVM_WINDOWS_VERSION}"
    asset = f"LLVM-{LLVM_WINDOWS_VERSION}-win64.exe"
    try:
        url = _gh_tagged_asset("llvm/llvm-project", tag, asset)
    except Exception as exc:
        console.error(f"Could not resolve LLVM {LLVM_WINDOWS_VERSION} asset: {exc}")
        return None
    with tempfile.TemporaryDirectory() as tmp:
        archive = Path(tmp) / asset
        try:
            console.info(f"Downloading LLVM {LLVM_WINDOWS_VERSION} (~1 GB) into {dest} ...")
            _download(url, archive)
            console.info("Installing LLVM silently (this takes a minute) ...")
            dest.mkdir(parents=True, exist_ok=True)
            # NSIS installer requires elevation (WinError 740). We use PowerShell to 
            # trigger the UAC prompt via -Verb RunAs so it can install silently.
            ps_cmd = (
                f"Start-Process -FilePath '{archive}' "
                f"-ArgumentList '/S /D={dest.absolute()}' -Wait -Verb RunAs"
            )
            subprocess.run(["powershell", "-NoProfile", "-Command", ps_cmd], check=True)
        except Exception as exc:
            console.error(f"LLVM vendor failed: {exc}")
            shutil.rmtree(dest, ignore_errors=True)
            return None
    found = _find_windows_llvm()
    if found:
        console.success(f"LLVM {LLVM_WINDOWS_VERSION} vendored into {dest}")
    else:
        console.error("LLVM extracted but lib/cmake/llvm is missing.")
    return found


# --------------------------------------------------------------------------- #
# AdaptiveCpp (acpp) from source
# --------------------------------------------------------------------------- #

def install_adaptivecpp(cfg: Config, mgr: IPackageManager | None,
                        vcpkg: IPackageManager | None, dry_run: bool,
                        assume_yes: bool = False) -> str | None:
    """Build AdaptiveCpp from source. Return the ``acpp`` executable path.

    Best-effort: a failure is reported but left non-fatal by the caller, since
    the intel-llvm toolchain already provides a working SYCL compiler. On Windows
    a missing LLVM dev install triggers a timed consent prompt before the heavy
    (~2-3 GB) LLVM download; ``assume_yes`` skips that prompt (used by the
    explicit ``sr setup acpp`` command).
    """
    prefix = toolchains_dir() / "adaptivecpp"
    acpp = prefix / "bin" / ("acpp.bat" if cfg.is_windows else "acpp")
    acpp_exe = prefix / "bin" / "acpp"
    for cand in (acpp, acpp_exe):
        if cand.is_file():
            console.info(f"AdaptiveCpp already present: {prefix}")
            return str(cand)

    # cmake/git may be off PATH (e.g. scoop), so honour the configured paths.
    cmake = cfg.expand(cfg.cmake_exe) if cfg.cmake_exe else (shutil.which("cmake") or "")
    git = shutil.which("git") or ""
    if not git or not cmake:
        console.warn("AdaptiveCpp build needs git and cmake; skipping.")
        return None

    if dry_run:
        console.info(f"(dry-run) would build AdaptiveCpp {ACPP_VERSION} -> {prefix}")
        return str(acpp_exe)

    llvm_dir, clang_prefix = (
        _acpp_deps_windows(vcpkg, assume_yes=assume_yes)
        if cfg.is_windows else _acpp_deps_linux(mgr)
    )
    if llvm_dir is None:
        _explain_acpp_skip(cfg)
        return None

    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "acpp"
        build = src / "build"
        clone = _run(
            [git, "clone", "--depth", "1", "--branch", ACPP_VERSION,
             "https://github.com/AdaptiveCpp/AdaptiveCpp.git", str(src)],
            dry_run, check=True,
        )
        if clone != 0:
            console.warn("AdaptiveCpp clone failed; skipping.")
            return None

        cfg_cmd = [
            cmake, "-S", str(src), "-B", str(build), "-G", "Ninja",
            "-DCMAKE_BUILD_TYPE=Release",
            f"-DCMAKE_INSTALL_PREFIX={prefix}",
            f"-DLLVM_DIR={llvm_dir}",
            "-DCMAKE_POLICY_VERSION_MINIMUM=3.5",
        ]
        if clang_prefix:
            cfg_cmd.append(f"-DCLANG_INSTALL_PREFIX={clang_prefix}")
            if cfg.is_windows:
                vcpkg_tc = deps_dir() / "vcpkg" / "scripts" / "buildsystems" / "vcpkg.cmake"
                if vcpkg_tc.is_file():
                    cfg_cmd.append(f"-DCMAKE_TOOLCHAIN_FILE={vcpkg_tc}")
        if cfg.ninja_exe:
            cfg_cmd.append(f"-DCMAKE_MAKE_PROGRAM={cfg.expand(cfg.ninja_exe)}")

        # On Windows, clang-cl needs rc.exe (Windows SDK Resource Compiler) on
        # PATH for the manifest-embed step. vcvars is not sourced here, so we
        # locate the SDK bin dir and inject it into the subprocess environment.
        if cfg.is_windows:
            rc_dir = _find_windows_sdk_rc_dir()
            if rc_dir and rc_dir.lower() not in os.environ.get("PATH", "").lower():
                os.environ["PATH"] = rc_dir + os.pathsep + os.environ.get("PATH", "")

        configure_ok = _run_quiet(cfg_cmd, dry_run)
        if not configure_ok:
            console.warn("AdaptiveCpp configure failed; skipping. "
                         "intel-llvm is installed and selected.")
            return None
        if _run([cmake, "--build", str(build), "--target", "install"],
                dry_run, check=True) != 0:
            console.warn("AdaptiveCpp build failed; skipping. "
                         "intel-llvm is installed and selected.")
            return None

    result = next((c for c in (acpp, acpp_exe) if c.is_file()), None)
    if result:
        console.success(f"AdaptiveCpp installed: {prefix}")
        return str(result)
    console.warn("AdaptiveCpp build completed but acpp binary not found.")
    return None


def _explain_acpp_skip(cfg: Config) -> None:
    """Explain clearly why the acpp build was skipped and how to enable it.

    AdaptiveCpp has no official Windows binary, so `sr setup` builds it from
    source — which needs an LLVM development install (with lib/cmake/llvm). When
    that is unavailable the build is skipped (non-fatal: intel-llvm still works).
    The user asked for an actionable message instead of a bare warning.
    """
    console.warn("AdaptiveCpp (acpp) was skipped — it builds from source and its "
                 "LLVM development dependency is not available.")
    if cfg.is_windows:
        console.info("Install it anytime with [bold cyan]sr setup acpp[/bold cyan] — "
                     f"that downloads LLVM {LLVM_WINDOWS_VERSION} (~2-3 GB) into the "
                     "deps folder and builds acpp, no prompt.")
        console.info("Note: the intel/llvm bundle cannot supply this LLVM — it ships "
                     "a compiler, not the LLVM cmake/dev files acpp needs.")
    else:
        console.info("To enable acpp on Linux, install the LLVM dev packages, e.g. "
                     "(Debian/Ubuntu): clang-17 llvm-17-dev libclang-17-dev "
                     "libboost-context-dev libboost-fiber-dev, then re-run "
                     "`sr setup --profile normal`.")
    console.info("This is non-fatal: intel-llvm is installed and selected, so you "
                 "can build and run now. acpp is the secondary toolchain.")


def _acpp_deps_linux(mgr: IPackageManager | None) -> tuple[str | None, str | None]:
    """Install acpp build deps via the distro manager. Return (LLVM_DIR, clang_prefix).

    Mirrors the Dockerfile: clang-17/llvm-17-dev + boost-context/fiber. Only the
    apt path matches the exact package names; other distros use generic clang/llvm
    + boost so we let cmake's find_package locate LLVM (LLVM_DIR left to default).
    """
    if mgr is None:
        return (None, None)
    if mgr.name == "apt":
        pkgs = [
            f"clang-{ACPP_LLVM}", f"llvm-{ACPP_LLVM}-dev",
            f"libclang-{ACPP_LLVM}-dev", f"lld-{ACPP_LLVM}",
            "libboost-context-dev", "libboost-fiber-dev",
        ]
        mgr.install(pkgs, dry_run=False)
        llvm_dir = f"/usr/lib/llvm-{ACPP_LLVM}/lib/cmake/llvm"
        clang_prefix = f"/usr/lib/llvm-{ACPP_LLVM}"
        return (llvm_dir if Path(llvm_dir).is_dir() else None, clang_prefix)
    # Non-apt: install generic clang/llvm-dev/boost and let find_package resolve.
    mgr.install(mgr.translate_apt(
        ["clang", "llvm", "libboost-context-dev", "libboost-fiber-dev"]),
        dry_run=False)
    return ("", None)  # empty LLVM_DIR => cmake searches default locations


def _acpp_deps_windows(vcpkg: IPackageManager | None,
                       assume_yes: bool = False) -> tuple[str | None, str | None]:
    """Install acpp build deps on Windows. Return (LLVM_DIR, clang_prefix).

    boost-context/fiber come from vcpkg. acpp also needs an LLVM dev install
    (lib/cmake/llvm); if one is already present it is reused. Otherwise acquiring
    it means a ~2-3 GB download, so the user is asked first (30 s, default no).
    On consent LLVM is vendored into the deps folder (winget is used when present
    but is not required). Without consent we return (None, None) and the caller
    explains how to enable acpp later.
    """
    if vcpkg is not None:
        vcpkg.install(["boost-context", "boost-fiber"], dry_run=False)

    existing = _find_windows_llvm()
    if existing:
        return existing

    # Consent is gathered up front by the caller (before the progress spinner),
    # never here — prompting mid-pipeline is unanswerable. Without consent, skip.
    if not assume_yes:
        return (None, None)

    # Consent given. Prefer winget if available (installs to Program Files),
    # else vendor the official tarball into the deps folder.
    if shutil.which("winget"):
        console.info("Installing LLVM via winget ...")
        subprocess.run(
            ["winget", "install", "--id", "LLVM.LLVM", "-e",
             "--accept-package-agreements", "--accept-source-agreements",
             "--silent"],
            check=False,
        )
        existing = _find_windows_llvm()
        if existing:
            return existing

    vendored = _vendor_llvm_windows()
    return vendored if vendored else (None, None)

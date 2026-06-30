"""Concrete pipeline steps.

Each step has a single responsibility and depends only on abstractions:
``DetectStep`` inventories the machine, ``InstallDepsStep`` installs missing
manifest packages through injected package managers, ``ConfigureStep`` writes
``config.local.toml`` from probed tool paths, ``VerifyStep`` builds and
smoke-tests through the project service, and ``UninstallStep`` tears down
everything the installer placed on the system.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from rich.table import Table

from .. import console
from ..config import config_dir
from . import probe
from .probe import binary_works
from .dependency_source import Dependency, IDependencySource
from .package_managers import (
    IPackageManager,
    WINGET_ID_TO_CMD,
    _tools_dir,
    refresh_windows_path,
)
from .pipeline import InstallContext, Step, StepResult
from . import toolchains

# System toolchain installed through the toolchain manager (not via the manifest
# since these tools must exist before vcpkg/pip can run).
_LINUX_TOOLCHAIN_APT = ["build-essential", "cmake", "ninja-build", "git"]


def _check_cmd_ok(cmd: list[str]) -> bool:
    """True if *cmd* runs and exits 0 (its tool is present and the check passes)."""
    try:
        return subprocess.run(cmd, capture_output=True).returncode == 0
    except (OSError, FileNotFoundError):
        return False


def _dep_installed(dep: Dependency, mgr: IPackageManager, platform: str) -> bool:
    if dep.check_cmd and _check_cmd_ok(dep.check_cmd):
        return True
    pkgs = dep.packages_for(platform)
    if not pkgs:
        return False
    return all(mgr.is_installed(p) for p in pkgs)


def _first_available(managers: list[IPackageManager]) -> IPackageManager | None:
    for mgr in managers:
        if mgr.available():
            return mgr
    return None


class DetectStep(Step):
    """Inventory tools and dependencies; fill ``ctx.detected``."""

    name = "detect"

    def __init__(self, source: IDependencySource,
                 managers: list[IPackageManager] | None = None) -> None:
        self._source = source
        self._managers = managers or []

    def _dep_manager(self, plat: str) -> IPackageManager | None:
        """The manager that knows whether a manifest dep is installed."""
        if plat == "windows":
            return next((m for m in self._managers if m.name == "vcpkg"), None)
        linux = ("apt", "dnf", "yum", "pacman", "zypper")
        return next((m for m in self._managers
                     if m.name in linux and m.available()), None)

    def _add_toolchain_rows(self, ctx: InstallContext, table: Table) -> None:
        """Add an installed/missing row per SYCL toolchain (and CUDA)."""
        from ..config import deps_dir
        win = ctx.cfg.platform == "windows"
        base = deps_dir() / "toolchains"

        clang = base / "llvm-sycl" / "bin" / ("clang++.exe" if win else "clang++")
        intel_ok = clang.is_file() and probe.binary_works(str(clang))

        acpp_path = ""
        for n in ("acpp.bat", "acpp"):
            cand = base / "adaptivecpp" / "bin" / n
            if cand.is_file():
                acpp_path = str(cand)
                break
        acpp_ok = bool(acpp_path) and probe.binary_works(acpp_path)

        # oneAPI installs system-wide (off the deps tree). Trust the same probe
        # the active-compiler row uses so a glob-discovered icx-cl/icpx counts.
        active, _ = probe.find_sycl_compiler(ctx.cfg)
        oneapi_ok = (active in ("icx-cl", "icpx")
                     or any(probe.binary_works(c) for c in ("icx-cl", "icpx", "icx")))

        rows = [
            ("intel-llvm",  intel_ok, "intel/llvm SYCL toolchain (clang++ -fsycl)"),
            ("adaptivecpp", acpp_ok,  "AdaptiveCpp (acpp)"),
            ("oneapi",      oneapi_ok, "Intel oneAPI DPC++ (icx/icpx)"),
        ]
        if ctx.gpu:
            rows.append(("cuda", probe.binary_works("nvcc"), "NVIDIA CUDA toolkit (nvcc)"))
        for name, present, detail in rows:
            ctx.detected[name] = present
            table.add_row(name, _mark(present), detail)

    def run(self, ctx: InstallContext) -> StepResult:
        plat = ctx.cfg.platform
        refresh_windows_path()  # reflect tools the bootstrap installer just added
        table = Table(show_header=True, header_style="bold magenta",
                      title="Environment inventory")
        table.add_column("Component")
        table.add_column("Status")
        table.add_column("Detail", style="dim")

        # Some tools the build uses live off PATH (the deps folder, or a vcpkg
        # port like pkgconf). Fall back to the configured path so the row shows
        # what the build actually uses instead of a misleading MISSING.
        cfg = ctx.cfg
        configured = {
            "cmake":     cfg.expand(cfg.cmake_exe)    if cfg.cmake_exe    else "",
            "ninja":     cfg.expand(cfg.ninja_exe)    if cfg.ninja_exe    else "",
            "pkg-config": cfg.expand(cfg.pkgconf_exe) if cfg.pkgconf_exe  else "",
            "doxygen":   cfg.expand(cfg.doxygen_exe)  if cfg.doxygen_exe  else "",
        }
        for tool in ("python3" if plat != "windows" else "python",
                     "git", "cmake", "ninja", "pkg-config", "doxygen"):
            path = shutil.which(tool) or ""
            if not path and configured.get(tool) and Path(configured[tool]).is_file():
                path = configured[tool]
            ctx.detected[tool] = bool(path)
            table.add_row(tool, _mark(bool(path)), path)

        compiler, where = probe.find_sycl_compiler(ctx.cfg)
        if compiler is None:
            # The intel/llvm bundle and acpp live off PATH; consult the paths
            # `ss install` records so a post-install re-run reports them correctly.
            compiler, where = probe.find_configured_toolchain(ctx.cfg)
        ctx.detected["sycl_compiler"] = compiler is not None
        table.add_row("SYCL compiler (active)", _mark(compiler is not None),
                      f"{compiler or '-'} {where or ''}".strip())

        # Per-toolchain status. These are SushiRuntime's SYCL toolchains, installed
        # by the toolchain installer (not apt/vcpkg), so they are reported here
        # rather than in the manifest-dependency loop below — which only covers
        # apt/vcpkg packages. This is the "what is installed" view for the heavy
        # components `ss install --customize` lets you pick.
        self._add_toolchain_rows(ctx, table)

        # Use the same check `install-deps` uses (check_cmd, then the package
        # manager) so detect and install agree — a lib installed via vcpkg/apt
        # is reported present even when its check_cmd tool (e.g. pkg-config) is
        # not on PATH.
        dep_mgr = self._dep_manager(plat)
        for dep in self._source.selected(plat, ctx.gpu):
            if dep_mgr is not None:
                present = _dep_installed(dep, dep_mgr, plat)
            else:
                present = bool(dep.check_cmd) and _check_cmd_ok(dep.check_cmd)
            ctx.detected[dep.name] = present
            pkgs = ", ".join(dep.packages_for(plat))
            table.add_row(dep.name, _mark(present),
                          f"{dep.description} ({pkgs})")

        gpu_present = shutil.which("nvidia-smi") is not None
        ctx.detected["nvidia_gpu"] = gpu_present
        table.add_row("NVIDIA GPU", _mark(gpu_present),
                      "use --gpu to install CUDA" if gpu_present else "")

        console.console.print(table)

        from ..config import deps_dir
        console.info(f"Vendored dependencies go in one folder: {deps_dir()}")
        console.info("Remove the whole install by deleting that folder "
                     "(`ss remove --all` does it for you).")
        if plat == "windows":
            console.info("System prerequisites kept outside that folder: the C++ "
                         "host compiler (Visual Studio Build Tools + Windows SDK), "
                         "git, and — with --gpu — the CUDA toolkit.")
        else:
            console.info("System prerequisites kept outside that folder: the host "
                         "compiler (gcc) plus the -dev packages (hwloc, gtest, "
                         "opencl), git, and — with --gpu — the CUDA toolkit.")
        return StepResult.OK


class InstallDepsStep(Step):
    """Install missing manifest dependencies and system toolchain."""

    name = "install-deps"

    def __init__(self, source: IDependencySource,
                 managers: list[IPackageManager]) -> None:
        self._source = source
        self._managers = managers

    def _manager(self, name: str) -> IPackageManager | None:
        for m in self._managers:
            if m.name == name:
                return m
        return None

    def run(self, ctx: InstallContext) -> StepResult:
        if ctx.cfg.platform == "windows":
            return self._run_windows(ctx)
        return self._run_linux(ctx)

    # -- SYCL toolchains (shared) --------------------------------------------- #

    def _install_toolchains(self, ctx: InstallContext,
                            mgr: IPackageManager | None,
                            vcpkg: IPackageManager | None) -> None:
        """Install the SYCL toolchains the active profile selected.

        Which toolchains run is gated by ``ctx.install_intel_llvm`` /
        ``ctx.install_acpp`` (set from PROFILE_SPECS). Installed paths are recorded
        on ``ctx.resolved_paths`` so ConfigureStep can write them. Failures are
        non-fatal when another toolchain remains, but the sole toolchain of a
        profile failing leaves nothing to build with, so that case warns loudly.
        """
        if ctx.install_intel_llvm:
            llvm = toolchains.install_intel_llvm(ctx.cfg, ctx.dry_run)
            if llvm:
                ctx.resolved_paths["llvm_root"] = llvm
            else:
                console.warn("intel/llvm bundle not installed; the intel-llvm "
                             "toolchain will be unavailable.")

        if ctx.install_acpp:
            acpp = toolchains.install_adaptivecpp(
                ctx.cfg, mgr, vcpkg, ctx.dry_run,
                assume_yes=ctx.assume_acpp_llvm)
            if acpp:
                ctx.resolved_paths["acpp_exe"] = acpp
            elif not ctx.install_intel_llvm:
                console.warn("AdaptiveCpp is the only toolchain in this profile but "
                             "it did not install; the project will not build. "
                             "Re-run with `--profile normal` for the intel-llvm "
                             "fallback.")

    # -- Linux ---------------------------------------------------------------- #

    def _run_linux(self, ctx: InstallContext) -> StepResult:
        linux_managers = ["apt", "dnf", "yum", "pacman", "zypper"]
        mgr = next(
            (self._manager(n) for n in linux_managers
             if self._manager(n) and self._manager(n).available()),  # type: ignore[union-attr]
            None,
        )
        if mgr is None:
            msg = ("No supported package manager found (apt, dnf, yum, pacman, zypper). "
                   "Install python3, pip, git, cmake, and ninja manually, then re-run.")
            if ctx.dry_run:
                console.warn(f"(dry-run) {msg}")
                return StepResult.SKIPPED
            console.error(msg)
            return StepResult.FAILED

        console.info(f"Using package manager: {mgr.name}")

        # Translate the generic apt toolchain list to native package names.
        pkgs: list[str] = list(mgr.translate_apt(_LINUX_TOOLCHAIN_APT))
        for dep in self._source.selected("linux", ctx.gpu):
            if _dep_installed(dep, mgr, "linux"):
                console.info(f"{dep.name}: already installed, skipping.")
                continue
            pkgs.extend(mgr.translate_apt(dep.linux_apt))

        pkgs = _dedup(pkgs)
        if not pkgs:
            console.info("All packages already present.")
            return StepResult.SKIPPED

        console.info(f"Installing via {mgr.name}: {', '.join(pkgs)}")
        ok = mgr.install(pkgs, ctx.dry_run)
        ctx.installed.extend(pkgs)

        self._install_toolchains(ctx, mgr=mgr, vcpkg=None)

        # Intel oneAPI DPC++ — opt-in (--oneapi). Mirrors the Dockerfile's
        # WITH_ONEAPI apt route; needs the Intel oneAPI apt repo configured.
        if ctx.oneapi and mgr.name == "apt":
            console.info("oneAPI: installing intel-oneapi-compiler-dpcpp-cpp "
                         "(requires the Intel oneAPI apt repository).")
            mgr.install(["intel-oneapi-compiler-dpcpp-cpp"], ctx.dry_run)
        elif ctx.oneapi:
            console.warn(f"--oneapi on {mgr.name} is not automated; install the "
                         "Intel oneAPI DPC++ compiler manually.")
        return StepResult.OK if ok else StepResult.FAILED

    # -- Windows -------------------------------------------------------------- #

    def _run_windows(self, ctx: InstallContext) -> StepResult:
        refresh_windows_path()  # see cmake/git the bootstrap script just installed
        winget = self._manager("winget")
        direct = self._manager("direct-download")
        vcpkg  = self._manager("vcpkg")

        tool_ok = True

        # cmake, ninja, and doxygen: always install portably into the deps folder
        # (never system-wide via winget), so the whole install stays one deletable
        # directory. The direct-download manager extracts them under deps/tools
        # and skips anything already on PATH.
        portable = ["Kitware.CMake", "Ninja-build.Ninja", "DimitriVanHeesch.Doxygen"]
        missing_portable = [
            pkg for pkg in portable if not shutil.which(WINGET_ID_TO_CMD.get(pkg, ""))
        ]
        if missing_portable and direct:
            console.info("Installing CMake, Ninja, and Doxygen portably into the deps folder ...")
            tool_ok = direct.install(missing_portable, ctx.dry_run)
        elif not missing_portable:
            console.info("cmake + ninja + doxygen already present, skipping.")

        # git is a bootstrap prerequisite (needed to clone the repo and acpp), so
        # it stays a system tool: reuse an existing one, else install via winget.
        if not shutil.which("git"):
            if winget and winget.available():
                console.info("Installing git via winget ...")
                tool_ok = winget.install(["Git.Git"], ctx.dry_run) and tool_ok
            elif direct:
                tool_ok = direct.install(["Git.Git"], ctx.dry_run) and tool_ok
            else:
                console.warn("git not found and no installer available; install it manually.")

        # C++ library ports via vcpkg.
        ports: list[str] = []
        for dep in self._source.selected("windows", ctx.gpu):
            if vcpkg and _dep_installed(dep, vcpkg, "windows"):
                console.info(f"{dep.name}: already installed, skipping.")
                continue
            ports.extend(dep.windows_vcpkg)
        ports = _dedup(ports)

        lib_ok = True
        if ports:
            if vcpkg is None:
                console.error("vcpkg manager missing; cannot install C++ libs.")
                return StepResult.FAILED
            console.info(f"Installing via vcpkg: {', '.join(ports)}")
            lib_ok = vcpkg.install(ports, ctx.dry_run)
            ctx.installed.extend(ports)

        # VS Build Tools — only attempt if winget is present.
        if winget and winget.available():
            if not winget.is_installed("Microsoft.VisualStudio.2022.BuildTools"):
                if ctx.dry_run:
                    console.info("(dry-run) skipping VS Build Tools install.")
                else:
                    vs_cmd = [
                        "winget", "install",
                        "--id", "Microsoft.VisualStudio.2022.BuildTools", "-e",
                        "--accept-package-agreements", "--accept-source-agreements",
                        "--override",
                        "--add Microsoft.VisualStudio.Workload.VCTools "
                        "--includeRecommended --quiet --wait --norestart",
                    ]
                    with console.console.status(
                        "[bold cyan]Installing Visual Studio Build Tools "
                        "(C++ workloads) — this may take 10–20 minutes.",
                        spinner="bouncingBar",
                    ):
                        rc = subprocess.run(vs_cmd).returncode
                    if rc != 0:
                        console.warn("Visual Studio Build Tools install failed or was cancelled.")
                        tool_ok = False
                    else:
                        console.success("Visual Studio Build Tools installed.")

        # Lean SYCL toolchains (intel-llvm bundle + AdaptiveCpp), like the
        # Dockerfile's default. oneAPI is installed below only with --oneapi.
        self._install_toolchains(ctx, mgr=None, vcpkg=vcpkg)

        # Intel oneAPI — opt-in (--oneapi), and only when icx-cl is not present.
        if ctx.oneapi and not ctx.detected.get("sycl_compiler", False):
            if ctx.dry_run:
                console.info("(dry-run) skipping Intel oneAPI install.")
            else:
                oneapi_url = (
                    "https://registrationcenter-download.intel.com/akdlm/IRC_NAS/"
                    "bae85ab1-cfcd-4251-8d42-a0c27949ea33/"
                    "intel-oneapi-toolkit-2026.0.0.193_offline.exe"
                )
                installer = Path.home() / "intel-oneapi-toolkit-offline.exe"
                if not installer.is_file():
                    console.info("Downloading Intel oneAPI Installer (~4 GB) from Intel servers ...")
                    dl_rc = subprocess.run(
                        ["curl", "-L", "-o", str(installer), oneapi_url]
                    ).returncode
                    if dl_rc != 0:
                        console.error("Failed to download Intel oneAPI installer.")
                        tool_ok = False
                if installer.is_file() and tool_ok:
                    oneapi_cmd = [
                        str(installer), "-s", "-a", "--silent", "--eula", "accept",
                        "-p=NEED_VS2022_INTEGRATION=1",
                    ]
                    with console.console.status(
                        "[bold cyan]Installing Intel oneAPI Toolkit silently "
                        "— this may take 10–20 minutes.",
                        spinner="bouncingBar",
                    ):
                        try:
                            rc = subprocess.run(oneapi_cmd).returncode
                        except OSError as exc:
                            if getattr(exc, "winerror", None) == 740:
                                console.info(
                                    "Intel oneAPI requires administrator privileges. "
                                    "A UAC prompt will appear — approve it to continue."
                                )
                                try:
                                    ps_cmd = (
                                        f"$p = Start-Process -FilePath '{str(installer)}'"
                                        f" -ArgumentList '-s','-a','--silent','--eula','accept'"
                                        f",'-p=NEED_VS2022_INTEGRATION=1'"
                                        f" -Verb RunAs -Wait -PassThru; exit $p.ExitCode"
                                    )
                                    rc = subprocess.run(
                                        ["powershell", "-Command", ps_cmd],
                                        timeout=1200,
                                    ).returncode
                                except Exception as exc2:
                                    console.error(f"Elevated oneAPI launch failed: {exc2}")
                                    tool_ok = False
                                    rc = 1
                            else:
                                console.warn(f"Intel oneAPI installer failed to launch: {exc}")
                                tool_ok = False
                                rc = 1
                    if rc != 0 and tool_ok:
                        console.warn("Intel oneAPI Toolkit installation failed.")
                        tool_ok = False
                    elif rc == 0:
                        console.success("Intel oneAPI Toolkit installed.")

        return StepResult.OK if (tool_ok and lib_ok) else StepResult.FAILED


class ConfigureStep(Step):
    """Probe installed tools and write ``config.local.toml``."""

    name = "configure"

    def run(self, ctx: InstallContext) -> StepResult:
        from ..config import set_toolchain

        refresh_windows_path()  # probe needs to see freshly-installed tools
        values = probe.resolve_local_config(ctx.cfg, gpu=ctx.gpu)
        ctx.resolved_paths = values

        target = config_dir() / "config.local.toml"

        if ctx.dry_run:
            if ctx.active_toolchain:
                console.info(f"(dry-run) would set active toolchain to "
                             f"'{ctx.active_toolchain}'.")
            if values:
                console.info(f"(dry-run) would write {target}:")
                console.console.print(
                    probe.render_local_config(ctx.cfg.platform, values), markup=False)
            return StepResult.OK

        if not values and not ctx.active_toolchain:
            console.info("No machine-specific paths to write; defaults suffice.")
            return StepResult.SKIPPED

        if values:
            content = probe.render_local_config(ctx.cfg.platform, values)
            if target.is_file():
                backup = target.with_suffix(".toml.bak")
                shutil.copyfile(target, backup)
                console.info(f"Backed up existing config to {backup.name}")
            target.write_text(content, encoding="utf-8")
            console.success(f"Wrote {target}")

        # Pin the profile's toolchain last: set_toolchain rewrites the file while
        # preserving the [tool.<platform>] table just written above.
        if ctx.active_toolchain:
            set_toolchain(ctx.active_toolchain)
            console.success(f"Active SYCL toolchain set to '{ctx.active_toolchain}'.")
        return StepResult.OK


class VerifyStep(Step):
    """Build and smoke-test through the existing project service."""

    name = "verify"

    def run(self, ctx: InstallContext) -> StepResult:
        if ctx.dry_run:
            console.info("(dry-run) skipping build/verify.")
            return StepResult.SKIPPED

        from ..services import project as project_svc
        from ..services.project import BuildType, Suite

        no_cuda = not ctx.gpu and ctx.cfg.platform != "windows"
        rc = project_svc.build(BuildType.release, distributed=False,
                               no_cuda=no_cuda, clean=False)
        if rc != 0:
            console.error("Build failed during verification.")
            return StepResult.FAILED

        rc = project_svc.test(Suite.functional, distributed=False,
                              filter=None, asan=False, repeat=0)
        if rc != 0:
            console.warn("Functional smoke test reported failures.")
            return StepResult.FAILED

        console.success("Build + smoke test passed. Project is ready.")
        return StepResult.OK


class UninstallStep(Step):
    """Remove packages and files that the installer placed on this system.

    With ``ctx.everything`` set, also removes toolchain binaries (cmake, git,
    ninja) that were downloaded by the direct-download manager. This is a
    destructive operation and cannot be undone automatically.
    """

    name = "uninstall"

    def __init__(self, source: IDependencySource,
                 managers: list[IPackageManager]) -> None:
        self._source = source
        self._managers = managers

    def _manager(self, name: str) -> IPackageManager | None:
        for m in self._managers:
            if m.name == name:
                return m
        return None

    def run(self, ctx: InstallContext) -> StepResult:
        if ctx.cfg.platform == "windows":
            return self._run_windows(ctx)
        return self._run_linux(ctx)

    def _run_linux(self, ctx: InstallContext) -> StepResult:
        linux_names = ["apt", "dnf", "yum", "pacman", "zypper"]
        mgr = next(
            (self._manager(n) for n in linux_names
             if self._manager(n) and self._manager(n).available()),  # type: ignore[union-attr]
            None,
        )
        pkgs: list[str] = []
        for dep in self._source.selected("linux", ctx.gpu):
            pkgs.extend(mgr.translate_apt(dep.linux_apt) if mgr else dep.linux_apt)
        pkgs = _dedup(pkgs)

        if mgr and pkgs:
            console.info(f"Removing via {mgr.name}: {', '.join(pkgs)}")
            mgr.remove(pkgs, ctx.dry_run)

        if ctx.everything:
            self._remove_installed_toolchains(ctx)
        self._remove_config(ctx)
        return StepResult.OK

    def _run_windows(self, ctx: InstallContext) -> StepResult:
        vcpkg = self._manager("vcpkg")

        ports: list[str] = []
        for dep in self._source.selected("windows", ctx.gpu):
            ports.extend(dep.windows_vcpkg)
        ports = _dedup(ports)

        if vcpkg and ports:
            console.info(f"Removing vcpkg ports: {', '.join(ports)}")
            vcpkg.remove(ports, ctx.dry_run)

        # Remove the ninja binary and portable cmake we extracted to the tools dir.
        tools = _tools_dir()
        ninja_exe = tools / "ninja.exe"
        if ninja_exe.is_file():
            if ctx.dry_run:
                console.info(f"(dry-run) would remove {ninja_exe}")
            else:
                ninja_exe.unlink()
                console.info(f"Removed {ninja_exe}")
        cmake_dir = tools / "cmake"
        if cmake_dir.is_dir():
            if ctx.dry_run:
                console.info(f"(dry-run) would remove {cmake_dir}")
            else:
                shutil.rmtree(cmake_dir, ignore_errors=True)
                console.info(f"Removed {cmake_dir}")
        doxygen_dir = tools / "doxygen"
        if doxygen_dir.is_dir():
            if ctx.dry_run:
                console.info(f"(dry-run) would remove {doxygen_dir}")
            else:
                shutil.rmtree(doxygen_dir, ignore_errors=True)
                console.info(f"Removed {doxygen_dir}")
        if tools.is_dir() and not any(tools.iterdir()) and not ctx.dry_run:
            tools.rmdir()

        if ctx.everything:
            # Wipe the whole shared dependency tree (portable cmake/ninja, the
            # SYCL toolchains, and vcpkg all live there). We deliberately do NOT
            # touch the system git/cmake the bootstrap installer may have placed —
            # those are the user's, not part of the dependencies/ tree.
            self._remove_installed_toolchains(ctx)

        self._remove_config(ctx)
        return StepResult.OK

    def _remove_installed_toolchains(self, ctx: InstallContext) -> None:
        """Delete the entire vendored deps folder (the one-folder install).

        Everything `sr setup` downloads — the intel/llvm bundle (~1 GB),
        AdaptiveCpp, portable cmake/ninja, and the vcpkg tree — lives under one
        directory, so ``--everything`` reclaims it all in a single rmtree. Only
        done with ``--everything`` since it is the heaviest, least-reversible part.
        """
        from ..config import deps_dir
        dep_dir = deps_dir()
        if not dep_dir.is_dir():
            return
        if ctx.dry_run:
            console.info(f"(dry-run) would remove the whole deps folder at {dep_dir}")
            return
        shutil.rmtree(dep_dir, ignore_errors=True)
        console.success(f"Removed the vendored deps folder at {dep_dir}")

    def _remove_config(self, ctx: InstallContext) -> None:
        target = config_dir() / "config.local.toml"
        if target.is_file():
            if ctx.dry_run:
                console.info(f"(dry-run) would remove {target}")
            else:
                target.unlink()
                console.success(f"Removed {target}")


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _mark(ok: bool) -> str:
    return "[green]OK[/green]" if ok else "[red]MISSING[/red]"


def _dedup(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out

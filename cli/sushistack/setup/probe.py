"""Tool-path probing and ``config.local.toml`` rendering.

Kept separate from the steps so ``ConfigureStep`` stays a thin orchestrator: this
module knows *where tools live*, the step knows *when to write them out*.

The values produced here mirror the fields the CLI's ``Config`` already reads
(see ``config.py``), so the generated ``config.local.toml`` plugs straight into
the existing layered-config + env-snapshot machinery with no other changes.
"""

from __future__ import annotations

import glob
import shutil
import subprocess
from pathlib import Path

from ..config import Config, deps_dir

# Common Windows install roots probed when a tool is not already on PATH.
_VS_VCVARS_GLOBS = [
    r"C:/Program Files/Microsoft Visual Studio/2022/*/VC/Auxiliary/Build/vcvars64.bat",
    r"C:/Program Files (x86)/Microsoft Visual Studio/2022/*/VC/Auxiliary/Build/vcvars64.bat",
]
_ONEAPI_ROOTS = [
    r"C:/Program Files (x86)/Intel/oneAPI",
    r"C:/Program Files/Intel/oneAPI",
]
_ICX_GLOBS = [
    r"C:/Program Files (x86)/Intel/oneAPI/compiler/*/bin/icx-cl.exe",
    r"C:/Program Files/Intel/oneAPI/compiler/*/bin/icx-cl.exe",
]
# Linux well-known install locations. Neither the apt CUDA toolkit nor oneAPI
# add themselves to PATH: nvcc lands under /usr/local/cuda*, and icpx/icx under
# /opt/intel/oneapi/compiler/*/bin (normally exposed only after `setvars.sh`).
# The probe checks these directly so a fresh `ss install` reports them present.
_NVCC_GLOBS_LINUX = [
    "/usr/local/cuda/bin/nvcc",
    "/usr/local/cuda-*/bin/nvcc",
]
_ICX_GLOBS_LINUX = [
    "/opt/intel/oneapi/compiler/*/bin/icpx",
    "/opt/intel/oneapi/compiler/*/bin/icx",
]


def _first_glob(patterns: list[str]) -> str:
    for pat in patterns:
        hits = sorted(glob.glob(pat))
        if hits:
            return hits[-1]  # latest version when sorted
    return ""


def _first_existing(paths: list[str]) -> str:
    for p in paths:
        if Path(p).exists():
            return p
    return ""


def _tools_dir() -> Path:
    """Where `sr setup` extracts portable tools (cmake, ninja): deps/tools."""
    return deps_dir() / "tools"


def _toolchains_dir() -> Path:
    """Where `sr setup` installs the intel-llvm bundle and AdaptiveCpp.

    Mirrors ``toolchains.toolchains_dir`` without importing it (that module pulls
    in package_managers, which imports this one — an import cycle).
    """
    return deps_dir() / "toolchains"


def _discover_installed_toolchains(cfg: Config, values: dict[str, str]) -> None:
    """Record the paths of toolchains installed by `sr setup`, if present."""
    base = _toolchains_dir()
    clang = base / "llvm-sycl" / "bin" / ("clang++.exe" if cfg.platform == "windows" else "clang++")
    if clang.is_file():
        values["llvm_root"] = str(base / "llvm-sycl")
    for name in ("acpp.bat", "acpp"):
        acpp = base / "adaptivecpp" / "bin" / name
        if acpp.is_file():
            values["acpp_exe"] = str(acpp)
            break


def toolchain_status(cfg: Config, gpu: bool) -> list[tuple[str, bool, str]]:
    """Installed/missing status for each SYCL toolchain (and CUDA if *gpu*).

    One row per toolchain: ``(name, present, detail)``. Lives here rather than
    on ``DetectStep`` because it is pure probing — the same kind of "where does
    this tool live and does it run" question the rest of this module answers —
    and keeping it here lets any caller (not just the detect step) ask the same
    question without going through the pipeline.
    """
    win = cfg.platform == "windows"
    base = _toolchains_dir()

    clang = base / "llvm-sycl" / "bin" / ("clang++.exe" if win else "clang++")
    intel_ok = clang.is_file() and binary_works(str(clang))

    acpp_path = ""
    for name in ("acpp.bat", "acpp"):
        cand = base / "adaptivecpp" / "bin" / name
        if cand.is_file():
            acpp_path = str(cand)
            break
    acpp_ok = bool(acpp_path) and binary_works(acpp_path)

    # oneAPI installs system-wide (off the deps tree). Trust the same probe
    # the active-compiler row uses so a glob-discovered icx-cl/icpx counts.
    active, _ = find_sycl_compiler(cfg)
    oneapi_bin = (_first_glob(_ICX_GLOBS) or shutil.which("icx-cl")
                  or shutil.which("icpx") or shutil.which("icx")
                  or (_first_glob(_ICX_GLOBS_LINUX) if not win else ""))
    oneapi_ok = (active in ("icx-cl", "icpx")
                 or (bool(oneapi_bin) and binary_works(oneapi_bin)))

    # Rich table cells are rendered as markup, so a path in square brackets (e.g.
    # from a Windows drive-letter-free relative path someone configured) would be
    # parsed as a tag and crash rendering. Escape it.
    from rich.markup import escape as _rich_escape

    rows = [
        ("intel-llvm",  intel_ok, f"intel/llvm SYCL toolchain (clang++ -fsycl) -> {_rich_escape(clang)}" if intel_ok else "intel/llvm SYCL toolchain (clang++ -fsycl)"),
        ("adaptivecpp", acpp_ok,  f"AdaptiveCpp (acpp) -> {_rich_escape(acpp_path)}" if acpp_ok else "AdaptiveCpp (acpp)"),
        ("oneapi",      oneapi_ok, f"Intel oneAPI DPC++ (icx/icpx) -> {_rich_escape(oneapi_bin)}" if oneapi_ok and oneapi_bin else "Intel oneAPI DPC++ (icx/icpx)"),
    ]
    if gpu:
        nvcc_bin = shutil.which("nvcc") or (_first_glob(_NVCC_GLOBS_LINUX) if not win else "")
        nvcc_ok = bool(nvcc_bin) and binary_works(nvcc_bin)
        detail = (f"NVIDIA CUDA toolkit (nvcc) -> {_rich_escape(nvcc_bin)}" if nvcc_ok
                  else "NVIDIA CUDA toolkit (nvcc)")
        rows.append(("cuda", nvcc_ok, detail))
    return rows


def detect_gpu_vendor() -> str:
    """Best-effort discrete-GPU vendor detection: nvidia | amd | intel | none.

    Order: vendor management tools first (definitive when present), then a
    portable ``lspci`` scan of the display-controller lines so we still classify
    a fresh machine that has no vendor stack installed yet. Returns ``none`` when
    nothing recognisable is found — the caller then provisions only the CPU
    (SPIR/OpenCL) path.
    """
    if shutil.which("nvidia-smi"):
        return "nvidia"
    if shutil.which("rocminfo") or shutil.which("rocm-smi"):
        return "amd"

    try:
        out = subprocess.run(["lspci"], capture_output=True, text=True,
                             timeout=10).stdout.lower()
    except Exception:
        out = ""
    gpu_lines = "\n".join(
        ln for ln in out.splitlines()
        if "vga compatible controller" in ln or "3d controller" in ln
        or "display controller" in ln
    )
    # NVIDIA/AMD discrete parts win over an Intel iGPU on the same line-set, so
    # they are checked first (a laptop often reports both Intel + a discrete GPU).
    if "nvidia" in gpu_lines:
        return "nvidia"
    if ("advanced micro devices" in gpu_lines or "amd/ati" in gpu_lines
            or "radeon" in gpu_lines):
        return "amd"
    if "intel" in gpu_lines:
        return "intel"
    return "none"


def binary_works(cmd: str) -> bool:
    """Return True only if *cmd* is on PATH (or an absolute path) and runs cleanly.

    Tries --version first; falls back to -version for tools that use that flag.
    A missing binary, a crash, or a non-zero exit all return False.
    """
    try:
        if subprocess.run([cmd, "--version"], capture_output=True, timeout=15).returncode == 0:
            return True
        return subprocess.run([cmd, "-version"], capture_output=True, timeout=15).returncode == 0
    except Exception:
        return False


def find_sycl_compiler(cfg: Config) -> tuple[str | None, str]:
    """Return (compiler, location) for the active SYCL toolchain, or (None, '').

    Existence of the binary is not enough — a partial install can leave the exe
    on disk with missing DLLs. The binary is verified by running --version.
    """
    if cfg.platform == "windows":
        icx = cfg.expand(cfg.icx_compiler) if cfg.icx_compiler else ""
        if icx and Path(icx).is_file() and binary_works(icx):
            return ("icx-cl", icx)
        hit = _first_glob(_ICX_GLOBS) or shutil.which("icx-cl") or shutil.which("icx") or ""
        if hit and binary_works(hit):
            return ("icx-cl", hit)
        return (None, "")
    # Linux: prefer oneAPI icpx, then intel/llvm clang++.
    for cc in ("icpx", "clang++"):
        path = shutil.which(cc)
        if path and binary_works(path):
            return (cc, path)
    return (None, "")


def find_configured_toolchain(cfg: Config) -> tuple[str | None, str]:
    """Return (label, path) for a SYCL compiler installed by `sr setup`, or (None, '').

    Unlike :func:`find_sycl_compiler`, this consults the off-PATH binaries the
    installer drops in (the intel/llvm bundle's clang++ and AdaptiveCpp's acpp),
    via the configured paths first and the toolchains dir as a fallback. Used by
    ``DetectStep`` so a re-run reports those toolchains accurately instead of
    falsely showing the compiler as missing.
    """
    win = cfg.platform == "windows"
    base = _toolchains_dir()

    bundle = ""
    if cfg.llvm_root:
        cand = Path(cfg.expand(cfg.llvm_root)) / "bin" / ("clang++.exe" if win else "clang++")
        if cand.is_file():
            bundle = str(cand)
    if not bundle:
        cand = base / "llvm-sycl" / "bin" / ("clang++.exe" if win else "clang++")
        if cand.is_file():
            bundle = str(cand)
    if bundle and binary_works(bundle):
        return ("intel-llvm clang++", bundle)

    acpp = cfg.expand(cfg.acpp_exe) if cfg.acpp_exe else ""
    if not (acpp and Path(acpp).is_file()):
        for name in ("acpp.bat", "acpp"):
            cand = base / "adaptivecpp" / "bin" / name
            if cand.is_file():
                acpp = str(cand)
                break
    if acpp and Path(acpp).is_file() and binary_works(acpp):
        return ("acpp", acpp)

    return (None, "")


def resolve_local_config(cfg: Config, gpu: bool = False) -> dict[str, str]:
    """Probe machine-specific tool paths to write into config.local.toml.

    Returns only the fields that were actually found, so we never write empty
    placeholders that would shadow the committed defaults.
    """
    if cfg.platform == "windows":
        return _resolve_windows(cfg)
    return _resolve_linux(cfg)


def _resolve_windows(cfg: Config) -> dict[str, str]:
    values: dict[str, str] = {}

    vcvars = cfg.expand(cfg.vs_vcvars) if cfg.vs_vcvars else ""
    if not (vcvars and Path(vcvars).is_file()):
        vcvars = _first_glob(_VS_VCVARS_GLOBS)
    if vcvars:
        values["vs_vcvars"] = vcvars

    oneapi = cfg.expand(cfg.oneapi_root) if cfg.oneapi_root else ""
    if not (oneapi and Path(oneapi).is_dir()):
        oneapi = _first_existing(_ONEAPI_ROOTS)
    if oneapi:
        values["oneapi_root"] = oneapi

    icx = _first_glob(_ICX_GLOBS) or shutil.which("icx-cl") or shutil.which("icx") or ""
    if icx:
        values["icx_compiler"] = icx

    portable_bin = _tools_dir() / "cmake" / "bin"
    ninja = shutil.which("ninja") or _first_existing([str(_tools_dir() / "ninja.exe")])
    if ninja:
        values["ninja_exe"] = ninja

    # VS BuildTools omits the CMake component, so pin the discovered cmake/ctest
    # rather than relying on PATH at build time, where the snapshotted env may not
    # expose it. cmake may live outside PATH for a non-interactive shell: a system
    # MSI in Program Files, or the portable zip we extract under deps/tools/cmake.
    _cmake_pf = [str(portable_bin / "cmake.exe"),
                 r"C:/Program Files/CMake/bin/cmake.exe",
                 r"C:/Program Files (x86)/CMake/bin/cmake.exe"]
    _ctest_pf = [str(portable_bin / "ctest.exe"),
                 r"C:/Program Files/CMake/bin/ctest.exe",
                 r"C:/Program Files (x86)/CMake/bin/ctest.exe"]
    cmake = shutil.which("cmake") or _first_existing(_cmake_pf)
    if cmake:
        values["cmake_exe"] = cmake
    ctest = shutil.which("ctest") or _first_existing(_ctest_pf)
    if ctest:
        values["ctest_exe"] = ctest

    pkgconf = shutil.which("pkg-config") or shutil.which("pkgconf")
    vcpkg_root = cfg.expand(cfg.vcpkg_root) if cfg.vcpkg_root else ""
    # Ignore a configured path that points inside a conda/venv tree.
    if vcpkg_root and (".conda" in vcpkg_root or "envs" in vcpkg_root):
        vcpkg_root = ""
    if not (vcpkg_root and Path(vcpkg_root).is_dir()):
        # Self-contained default: the deps vcpkg tree. Written even when absent so
        # VcpkgManager bootstraps there instead of a system-wide location.
        vcpkg_root = str(deps_dir() / "vcpkg")
    if vcpkg_root:
        values["vcpkg_root"] = vcpkg_root
        # pkgconf shipped by vcpkg is the one CMakeLists expects.
        vcpkg_pkgconf = Path(vcpkg_root) / "installed" / (cfg.vcpkg_triplet or "x64-windows") / "tools" / "pkgconf" / "pkgconf.exe"
        if vcpkg_pkgconf.is_file():
            pkgconf = str(vcpkg_pkgconf)
    if pkgconf:
        values["pkgconf_exe"] = pkgconf

    doxy = shutil.which("doxygen") or _first_existing([
        str(_tools_dir() / "doxygen" / "doxygen.exe"),
        r"C:/Program Files/doxygen/bin/doxygen.exe",
        r"C:/Program Files (x86)/doxygen/bin/doxygen.exe",
    ])
    if doxy:
        values["doxygen_exe"] = doxy

    _discover_installed_toolchains(cfg, values)
    return values


def _resolve_linux(cfg: Config) -> dict[str, str]:
    values: dict[str, str] = {}
    compiler, path = find_sycl_compiler(cfg)
    if compiler == "clang++":
        # intel/llvm path: project.py already falls back, but make it explicit.
        values["cxx"] = "clang++"
        values["cc"] = "clang"
    oneapi = cfg.expand(cfg.oneapi_root) if cfg.oneapi_root else ""
    if oneapi and Path(oneapi, "setvars.sh").is_file():
        values["oneapi_root"] = oneapi
    del path
    _discover_installed_toolchains(cfg, values)
    return values


def render_local_config(platform: str, values: dict[str, str]) -> str:
    """Render values into the [tool.<platform>] layout config.py reads."""
    lines = [
        "# Auto-generated by `sr setup`. Machine-specific tool paths.",
        "# Safe to edit; re-running `sr setup configure` backs this up first.",
        "",
        f"[tool.{platform}]",
    ]
    for key in sorted(values):
        val = values[key].replace("\\", "/")
        lines.append(f'{key} = "{val}"')
    lines.append("")
    return "\n".join(lines)

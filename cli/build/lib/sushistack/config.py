"""Layered configuration loading for the SushiStack CLI.

Precedence (lowest to highest):
    built-in defaults -> config.toml -> config.local.toml -> SR_* env vars

The active platform's ``[tool.<platform>]`` table is merged over the common
``[tool]`` table, so a single file describes both Linux and Windows.

SushiStack is the umbrella workspace: the user clones it first, then `ss add`
clones the stack modules (sushiruntime, sushiengine, …) inside it. Everything the
installer downloads lands in ``<workspace>/dependencies`` and is shared by every
module, so the modules never provision their own toolchain or vcpkg tree.
"""

from __future__ import annotations

import os
import platform

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:  # Python 3.10 fallback
    import tomli as tomllib
from dataclasses import dataclass, fields
from pathlib import Path

# Marker file written at the workspace root by `ss init`. Its presence is how any
# `ss`/`sr`/`se` invocation locates the shared workspace from a nested directory.
WORKSPACE_MARKER = ".sushistack"


def workspace_root(start: Path | None = None) -> Path:
    """Locate the SushiStack workspace root.

    The CLI is installed (pip/pipx) outside the workspace, so the package location
    tells us nothing about where the workspace lives — the invocation directory
    does. Resolution order: ``SUSHISTACK_HOME`` env var, then a walk up from CWD
    looking for the ``.sushistack`` marker (or a ``cli/manifests`` tree, which is
    the repo's own signature). Run any `ss` command from anywhere inside the tree.
    """
    env = os.environ.get("SUSHISTACK_HOME")
    if env:
        return Path(env).expanduser().resolve()
    cur = (start or Path.cwd()).resolve()
    for d in (cur, *cur.parents):
        if (d / WORKSPACE_MARKER).is_file() or (d / "cli" / "manifests").is_dir():
            return d
    raise SystemExit(
        "Not inside a SushiStack workspace: no .sushistack marker found in the "
        "current directory or any parent. Run `ss init` first, or set "
        "SUSHISTACK_HOME to the workspace root."
    )


# Back-compat alias: diagnostics and ported code still call find_project_root.
find_project_root = workspace_root


def config_dir(root: Path | None = None) -> Path:
    """Directory holding config.toml / config.local.toml (the workspace's cli/)."""
    root = root or workspace_root()
    return root / "cli"


# Registry of modules linked to existing checkouts outside the workspace tree.
# Kept in its own file so writing it never disturbs the [tool] paths that
# `ss install` writes into config.local.toml.
MODULES_FILE = "modules.local.toml"


def registered_modules() -> dict[str, str]:
    """name -> absolute path for modules linked via ``ss link``.

    A developer's working checkouts often live outside the workspace tree (e.g.
    sibling repos). Linking one records its path here so ``ss`` aggregates its
    ``sushistack.deps.toml`` and tracks it, without cloning a second copy. Read
    from ``<workspace>/cli/modules.local.toml`` ``[modules]``.
    """
    try:
        home = workspace_root()
    except SystemExit:
        return {}
    doc = _read_toml(home / "cli" / MODULES_FILE)
    mods = doc.get("modules", {})
    return {k: str(v) for k, v in mods.items() if isinstance(v, str)}

def deps_dir() -> Path:
    """The single self-contained directory for everything ``ss install`` downloads.

    Everything vendorable — the intel/llvm bundle, AdaptiveCpp, a portable CMake
    and Ninja, and the vcpkg tree with its C++ library ports — lands under here,
    so a user can see exactly what was fetched and reclaim it all by deleting one
    folder (``ss remove --all``). Defaults to ``<workspace>/dependencies`` (git-
    ignored) so every module shares one tree; override with ``SUSHISTACK_DEPS_DIR``
    (``SR_DEPS_DIR`` is still honoured for back-compat). Falls back to a user-local
    path when not inside a workspace.

    System-level prerequisites that cannot live in one folder (the host C++
    compiler — MSVC+SDK on Windows, gcc and a few -dev packages on Linux — and
    CUDA) are intentionally *not* placed here; the installer reports them instead.
    """
    override = os.environ.get("SUSHISTACK_DEPS_DIR") or os.environ.get("SR_DEPS_DIR")
    if override:
        return Path(override)
    try:
        return workspace_root() / "dependencies"
    except SystemExit:
        local = os.environ.get("LOCALAPPDATA", "")
        base = Path(local) if local else Path.home() / ".local"
        return base / "SushiStack" / "dependencies"


# The SYCL toolchains a user can select. Must match SR_SYCL_TOOLCHAIN in
# CMakeLists.txt: intel-llvm (primary), adaptivecpp (secondary), oneapi (supported).
TOOLCHAINS = ("intel-llvm", "adaptivecpp", "oneapi")

# Default compiler pair (cc, cxx) per toolchain. Used when the config does not
# pin an explicit compiler, so `sr toolchain <name>` is enough to switch.
TOOLCHAIN_COMPILERS = {
    # (cc, cxx). acpp is C++-only, so the C slot uses a plain C compiler; the
    # project builds CXX only, so cc is effectively unused but kept valid.
    "adaptivecpp": ("gcc", "acpp"),
    "intel-llvm": ("clang", "clang++"),
    "oneapi": ("icx", "icpx"),
}

# `ss install` provisions EVERYTHING by default — all three SYCL toolchains
# (intel/llvm, AdaptiveCpp, oneAPI) plus CUDA. SYCL is a heavy ecosystem by
# nature, so there is no footprint-vs-breadth profile to choose: a user who is
# missing a toolchain will blame us, not their own narrowing. `ss install
# --customize` is the escape hatch — a picker for users who deliberately want a
# subset. ``active`` is written as the default SR_SYCL_TOOLCHAIN.
DEFAULT_ACTIVE_TOOLCHAIN = "intel-llvm"

# The customizable, weighty components `ss install --customize` lets a user pick.
# key -> (label, InstallContext field it gates). All default ON.
CUSTOMIZABLE_COMPONENTS = (
    ("intel-llvm",  "intel/llvm SYCL toolchain (clang++ -fsycl) — primary", "install_intel_llvm"),
    ("adaptivecpp", "AdaptiveCpp (acpp) — secondary SYCL toolchain",        "install_acpp"),
    ("oneapi",      "Intel oneAPI DPC++ (icx/icpx) — heavy, several GB",    "oneapi"),
    ("cuda",        "NVIDIA CUDA toolkit (SYCL nvptx64 backend)",           "gpu"),
)

# Maps a Config field to the SR_* env var that overrides it.
_ENV_OVERRIDES = {
    "toolchain": "SR_SYCL_TOOLCHAIN",
    "cc": "SR_CC",
    "cxx": "SR_CXX",
    "generator": "SR_CMAKE_GENERATOR",
    "vcpkg_root": "SR_VCPKG_ROOT",
    "oneapi_root": "SR_ONEAPI_ROOT",
    "vs_vcvars": "SR_VCVARS",
    "ninja_exe": "SR_NINJA",
    "cmake_exe": "SR_CMAKE",
    "ctest_exe": "SR_CTEST",
    "icx_compiler": "SR_ICX",
    "llvm_root": "SR_LLVM_ROOT",
    "acpp_exe": "SR_ACPP",
    "pkgconf_exe": "SR_PKGCONF",
    "doxygen_exe": "SR_DOXYGEN",
    "vcpkg_triplet": "SR_VCPKG_TRIPLET",
    "target_bin": "SR_TARGET_BIN",
}


@dataclass
class Config:
    """Resolved, platform-specific tool configuration."""

    # SYCL toolchain selection (intel-llvm | adaptivecpp | oneapi). Persisted by
    # `sr toolchain` and consumed as -DSR_SYCL_TOOLCHAIN at configure time.
    toolchain: str = "intel-llvm"

    # Compilers / build. Empty cc/cxx means "derive from the toolchain".
    cc: str = ""
    cxx: str = ""
    generator: str = "Ninja"
    use_vcpkg: bool = False

    # Tool roots / paths (mostly Windows-specific absolutes)
    vcpkg_root: str = ""
    oneapi_root: str = ""
    vs_vcvars: str = ""
    ninja_exe: str = ""
    # cmake/ctest are resolved from PATH when empty. They are configurable
    # because VS BuildTools does not ship the CMake component, so on Windows
    # cmake commonly lives in a scoop/standalone install that is not on PATH.
    cmake_exe: str = ""
    ctest_exe: str = ""
    icx_compiler: str = ""
    # intel/llvm nightly bundle root (holds bin/clang++) for the intel-llvm
    # toolchain, and the AdaptiveCpp compiler for the adaptivecpp toolchain.
    # On Windows these are how the non-oneAPI toolchains provide a SYCL compiler;
    # they are discovered by `sr setup` and written to config.local.toml.
    llvm_root: str = ""
    acpp_exe: str = ""
    pkgconf_exe: str = ""
    # doxygen is resolved from PATH when empty; configurable because on Windows it
    # commonly installs outside PATH (winget/choco shims or Program Files).
    doxygen_exe: str = ""
    vcpkg_triplet: str = "x64-windows"

    # Run defaults
    target_bin: str = "sr_functional_tests"

    # Derived
    platform: str = ""

    @property
    def is_windows(self) -> bool:
        return self.platform == "windows"

    def expand(self, value: str) -> str:
        """Expand ~ and env vars in a path-like config value."""
        return os.path.expandvars(os.path.expanduser(value)) if value else value

    def resolved_compilers(self) -> tuple[str, str]:
        """Return (cc, cxx), deriving them from the toolchain when not pinned.

        An explicit cc/cxx in the config always wins; otherwise the pair is
        taken from TOOLCHAIN_COMPILERS so selecting a toolchain is sufficient.
        """
        default_cc, default_cxx = TOOLCHAIN_COMPILERS.get(
            self.toolchain, TOOLCHAIN_COMPILERS["intel-llvm"])
        return (self.cc or default_cc, self.cxx or default_cxx)

    def llvm_bin(self) -> str:
        """The intel/llvm bundle's bin directory, or '' when not configured."""
        root = self.expand(self.llvm_root)
        return str(Path(root) / "bin") if root else ""

    def resolved_windows_compiler(self) -> str:
        """Return the C++ compiler to drive the Windows build for the toolchain.

        Windows has no system SYCL compiler, so each toolchain points at its own:
        intel-llvm -> clang++ from the intel/llvm bundle, oneapi -> icx-cl,
        adaptivecpp -> acpp. Falls back to a bare command name when the path is
        not configured so PATH resolution still has a chance.
        """
        if self.toolchain == "oneapi":
            return self.expand(self.icx_compiler) or "icx-cl"
        if self.toolchain == "adaptivecpp":
            return self.expand(self.acpp_exe) or "acpp"
        # intel-llvm (default)
        bin_dir = self.llvm_bin()
        return str(Path(bin_dir) / "clang++.exe") if bin_dir else "clang++"


def _read_toml(path: Path) -> dict:
    if not path.is_file():
        return {}
    with path.open("rb") as fh:
        return tomllib.load(fh)


def _merge_tool_table(doc: dict, plat: str) -> dict:
    """Merge common [tool] with [tool.<platform>] (platform wins)."""
    tool = dict(doc.get("tool", {}))
    merged = {k: v for k, v in tool.items() if not isinstance(v, dict)}
    plat_table = tool.get(plat, {})
    if isinstance(plat_table, dict):
        merged.update(plat_table)
    return merged


def load_config() -> Config:
    """Load and resolve the layered configuration for the current platform."""
    plat = platform.system().lower()  # 'windows' | 'linux' | 'darwin'

    cfg_dir = config_dir()
    values: dict = {}
    for fname in ("config.toml", "config.local.toml"):
        doc = _read_toml(cfg_dir / fname)
        if doc:
            values.update(_merge_tool_table(doc, plat))

    # Env overrides (highest precedence below CLI flags).
    for field_name, env_var in _ENV_OVERRIDES.items():
        if env_var in os.environ:
            values[field_name] = os.environ[env_var]

    # Coerce booleans that may arrive as strings from env.
    if isinstance(values.get("use_vcpkg"), str):
        values["use_vcpkg"] = values["use_vcpkg"].strip().lower() in ("1", "true", "yes", "on")

    known = {f.name for f in fields(Config)}
    cfg = Config(**{k: v for k, v in values.items() if k in known})
    cfg.platform = plat
    # Guard against a stale/typo'd toolchain leaking through from config or env.
    if cfg.toolchain not in TOOLCHAINS:
        cfg.toolchain = "intel-llvm"
    return cfg


def set_toolchain(toolchain: str) -> Path:
    """Persist the selected SYCL toolchain into config.local.toml.

    Writes a top-level ``[tool] toolchain = "..."`` key, preserving any existing
    ``[tool.<platform>]`` tables written by `sr setup configure`. Returns the
    path that was written.
    """
    if toolchain not in TOOLCHAINS:
        raise ValueError(f"Unknown toolchain '{toolchain}'. Choose one of {', '.join(TOOLCHAINS)}.")

    target = config_dir() / "config.local.toml"
    doc = _read_toml(target)
    tool = dict(doc.get("tool", {}))
    tool["toolchain"] = toolchain

    scalars = {k: v for k, v in tool.items() if not isinstance(v, dict)}
    tables = {k: v for k, v in tool.items() if isinstance(v, dict)}

    lines = [
        "# Managed by the SushiRuntime CLI. `sr toolchain` writes the toolchain key;",
        "# `sr setup configure` writes the [tool.<platform>] tool paths.",
        "",
        "[tool]",
    ]
    for key in sorted(scalars):
        val = scalars[key]
        if isinstance(val, bool):
            lines.append(f"{key} = {'true' if val else 'false'}")
        else:
            lines.append(f'{key} = "{val}"')
    for tname in sorted(tables):
        lines.append("")
        lines.append(f"[tool.{tname}]")
        for key in sorted(tables[tname]):
            val = str(tables[tname][key]).replace("\\", "/")
            lines.append(f'{key} = "{val}"')
    lines.append("")

    target.write_text("\n".join(lines), encoding="utf-8")
    return target

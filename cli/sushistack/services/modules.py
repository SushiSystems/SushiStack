"""Module + workspace management for the SushiStack umbrella.

SushiStack is the workspace the user clones first; the stack's modules
(sushiruntime, sushiengine, …) are git checkouts that live *inside* it, cloned by
``ss add``. This service owns that lifecycle — initialising the workspace, cloning
and updating modules, and reporting status — while the dependency engine in
``sushistack.setup`` owns everything under ``dependencies/``.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from .. import console
from ..config import (
    MODULES_FILE,
    WORKSPACE_MARKER,
    config_dir,
    deps_dir,
    registered_modules,
    workspace_root,
)
from ..setup.dependency_source import MODULE_MANIFEST_REL


@dataclass(frozen=True)
class Module:
    """One stack module: its short name, clone URL, and on-disk directory."""

    name: str          # short name used on the CLI: runtime | engine | ai | blas
    repo: str          # git clone URL
    directory: str     # directory name created under the workspace root


# The known stack modules. Names are the exact program names — there is no
# program called "runtime", it is "sushiruntime" — so no one confuses them. The
# directory matches the name so `sr`/`se` find their siblings.
MODULES: dict[str, Module] = {
    "sushiruntime": Module("sushiruntime", "https://github.com/sushisystems/sushiruntime.git", "sushiruntime"),
    "sushiengine":  Module("sushiengine",  "https://github.com/sushisystems/sushiengine.git",  "sushiengine"),
    "sushiai":      Module("sushiai",      "https://github.com/sushisystems/sushiai.git",      "sushiai"),
    "sushiblas":    Module("sushiblas",    "https://github.com/sushisystems/sushiblas.git",    "sushiblas"),
}

# Lines `ss init` ensures are present in the workspace .gitignore: the shared
# dependency tree and every module checkout are build artifacts of the workspace,
# not part of it.
_GITIGNORE_LINES = [
    "# Managed by `ss init`: shared dependencies and cloned modules are not tracked.",
    "/dependencies/",
    *(f"/{m.directory}/" for m in MODULES.values()),
    "/cli/config.local.toml",
    "/cli/modules.local.toml",
]


def module_dest(root: Path, name: str) -> Path:
    """Where module *name* lives: its linked external path, else inside the workspace.

    A module registered via ``ss link`` resolves to that checkout; otherwise it is
    the conventional ``<workspace>/<directory>`` that ``ss add`` clones into.
    """
    linked = registered_modules().get(name)
    if linked:
        return Path(linked)
    return root / MODULES[name].directory


def _write_link(name: str, path: Path) -> None:
    """Record (or update) a module->path entry in modules.local.toml."""
    registry = dict(registered_modules())
    registry[name] = str(path)
    target = config_dir() / MODULES_FILE
    lines = [
        "# Managed by `ss link`: modules pointed at existing checkouts outside the",
        "# workspace tree. `ss` reads these to aggregate their dependency fragments",
        "# and track them alongside cloned modules.",
        "",
        "[modules]",
    ]
    for key in sorted(registry):
        lines.append(f'{key} = "{str(registry[key]).replace(chr(92), "/")}"')
    lines.append("")
    target.write_text("\n".join(lines), encoding="utf-8")


def _run_git(args: list[str], cwd: Path) -> int:
    """Run a git command, streaming its output. Return its exit code."""
    try:
        return subprocess.run(["git", *args], cwd=str(cwd)).returncode
    except FileNotFoundError:
        console.error("git not found on PATH. Install git and try again.")
        return 1


def _resolve_names(names: list[str] | None) -> list[str] | None:
    """Expand a user module list ('all' or names) to concrete module names.

    Returns None on an unknown name (after reporting it), so callers can abort.
    """
    if not names or names == ["all"]:
        return list(MODULES)
    unknown = [n for n in names if n not in MODULES]
    if unknown:
        console.error(f"Unknown module(s): {', '.join(unknown)}. "
                      f"Choose from: {', '.join(MODULES)} (or 'all').")
        return None
    return names


def init() -> int:
    """Turn the current directory into a SushiStack workspace. Return exit code."""
    console.header("SushiStack Init")
    root = Path.cwd().resolve()

    marker = root / WORKSPACE_MARKER
    if marker.is_file():
        console.info(f"Already a SushiStack workspace: {root}")
    else:
        marker.write_text(
            "# SushiStack workspace marker. `ss` locates the workspace by walking\n"
            "# up to this file. Delete it to detach this directory.\n",
            encoding="utf-8",
        )
        console.success(f"Marked workspace root: {root}")

    gitignore = root / ".gitignore"
    existing = gitignore.read_text(encoding="utf-8") if gitignore.is_file() else ""
    missing = [ln for ln in _GITIGNORE_LINES if ln not in existing]
    if missing:
        prefix = "" if existing.endswith("\n") or not existing else "\n"
        with gitignore.open("a", encoding="utf-8") as fh:
            fh.write(prefix + "\n".join(missing) + "\n")
        console.success("Updated .gitignore (dependencies/ and module checkouts).")

    deps_dir().mkdir(parents=True, exist_ok=True)
    console.info(f"Dependencies will install into: {deps_dir()}")
    console.info("Next: `ss install` to provision deps, then `ss add runtime`.")
    return 0


def add(names: list[str] | None) -> int:
    """Clone one or more modules into the workspace. Return exit code."""
    console.header("SushiStack Add")
    resolved = _resolve_names(names)
    if resolved is None:
        return 1
    root = workspace_root()

    linked = registered_modules()
    failed = False
    for name in resolved:
        if name in linked:
            console.info(f"{name}: linked to {linked[name]} (use `ss link` to change); skipping clone.")
            continue
        mod = MODULES[name]
        dest = root / mod.directory
        if (dest / ".git").is_dir():
            console.info(f"{name}: already cloned at {dest}")
            continue
        console.info(f"{name}: cloning {mod.repo} -> {dest}")
        if _run_git(["clone", mod.repo, str(dest)], cwd=root) != 0:
            console.error(f"{name}: clone failed.")
            failed = True
    if failed:
        return 1
    console.success("Modules ready. Build them with their own CLI (`sr`, `se`).")
    return 0


def link(name: str, path: str) -> int:
    """Register an existing checkout as a module, in place (no clone). Return code.

    For developers whose working repos live outside the workspace tree: links the
    module to that path so `ss` aggregates its dependency fragment and tracks it.
    The module's own CLI still resolves the shared deps via SUSHISTACK_HOME.
    """
    console.header("SushiStack Link")
    if name not in MODULES:
        console.error(f"Unknown module '{name}'. Choose from: {', '.join(MODULES)}.")
        return 1
    target = Path(path).expanduser().resolve()
    if not target.is_dir():
        console.error(f"Path does not exist: {target}")
        return 1
    if not (target / ".git").is_dir():
        console.warn(f"{target} is not a git checkout; linking anyway.")
    _write_link(name, target)
    console.success(f"Linked {name} -> {target}")
    fragment = target / MODULE_MANIFEST_REL
    if not fragment.is_file():
        console.info(f"Note: cli/{fragment.name} not found there; this module adds no deps.")
    console.info("Run `ss install` to pick up its dependencies.")
    return 0


def update(names: list[str] | None) -> int:
    """git pull the modules that are present (cloned or linked). Return exit code."""
    console.header("SushiStack Update")
    resolved = _resolve_names(names)
    if resolved is None:
        return 1
    root = workspace_root()

    failed = False
    any_present = False
    for name in resolved:
        dest = module_dest(root, name)
        if not (dest / ".git").is_dir():
            if names and names != ["all"]:
                console.warn(f"{name}: not present (run `ss add {name}` or `ss link {name} <path>`).")
            continue
        any_present = True
        console.info(f"{name}: git pull ({dest})")
        if _run_git(["pull", "--ff-only"], cwd=dest) != 0:
            console.error(f"{name}: update failed.")
            failed = True
    if not any_present:
        console.info("No modules present yet. Add one with `ss add runtime`.")
    return 1 if failed else 0


def status() -> int:
    """Report which modules are present and where dependencies live."""
    from rich.table import Table

    console.header("SushiStack Status")
    root = workspace_root()
    linked = registered_modules()
    console.info(f"Workspace: {root}")

    table = Table(show_header=True, header_style="bold magenta")
    table.add_column("Module")
    table.add_column("Location")
    table.add_column("State")
    for name, mod in MODULES.items():
        dest = module_dest(root, name)
        if name in linked:
            state = "linked" if (dest / ".git").is_dir() else "linked (missing)"
            location = str(dest)
        else:
            state = "cloned" if (dest / ".git").is_dir() else "—"
            location = mod.directory
        table.add_row(name, location, state)
    console.console.print(table)

    deps = deps_dir()
    if deps.is_dir() and any(deps.iterdir()):
        console.info(f"Dependencies: {deps} (present). Verify with `ss doctor`.")
    else:
        console.info(f"Dependencies: {deps} (empty). Provision with `ss install`.")
    return 0


def sync(dry_run: bool) -> int:
    """Bring the workspace to a working state in one shot.

    Provisions any missing dependencies (everything, like `ss install`), then
    fast-forwards every present module. Module cloning stays explicit (`ss add`)
    so `sync` never pulls in repos the user did not ask for.
    """
    from . import setup as setup_svc

    console.header("SushiStack Sync")
    rc = setup_svc.run("provision", dry_run=dry_run)
    if rc != 0:
        return rc
    if dry_run:
        return 0
    return update(["all"])

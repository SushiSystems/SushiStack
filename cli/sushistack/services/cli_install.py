"""`ss install-cli` service: install a module's own developer CLI (`sr`, `se`).

The umbrella owns this so there is a single install seam for the whole stack: no
module ships its own bootstrap script. Each module CLI depends on ``sushicli``
(the shared presentation layer), which is not published to any index — so it
cannot be resolved as a normal pip dependency. This service installs the module
CLI into an isolated pipx venv, then injects ``sushicli`` from its sibling
checkout, exactly as `ss` itself is bootstrapped.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:  # Python 3.10 fallback
    import tomli as tomllib

from .. import console
from ..config import workspace_root
from .modules import _resolve_names, module_dest, sushicli_dir


def _run(cmd: list[str]) -> int:
    console.info("$ " + " ".join(cmd))
    return subprocess.run(cmd).returncode


def _ensure_pipx() -> list[str]:
    """Return a command prefix that runs pipx, installing it if necessary."""
    if subprocess.run([sys.executable, "-m", "pipx", "--version"],
                      capture_output=True).returncode == 0:
        return [sys.executable, "-m", "pipx"]
    console.info("pipx not found; installing it with pip ...")
    cmd = [sys.executable, "-m", "pip", "install", "pipx"]
    in_venv = sys.prefix != getattr(sys, "base_prefix", sys.prefix)
    if not in_venv:
        # --user only makes sense (and is only valid) outside a venv/conda env,
        # where user site-packages are visible; inside one, pip rejects it.
        cmd.append("--user")
    pip_help = subprocess.run([sys.executable, "-m", "pip", "install", "--help"],
                              capture_output=True, text=True).stdout
    if "--break-system-packages" in pip_help:
        cmd.append("--break-system-packages")
    if _run(cmd) != 0:
        raise RuntimeError("Failed to install pipx.")
    _run([sys.executable, "-m", "pipx", "ensurepath"])
    return [sys.executable, "-m", "pipx"]


def _dist_name(pkg_dir: Path) -> str:
    """The distribution name pipx installs, read from the package's pyproject."""
    with (pkg_dir / "pyproject.toml").open("rb") as fh:
        return str(tomllib.load(fh)["project"]["name"])


def install_cli(names: list[str] | None) -> int:
    """Install the developer CLI of one or more modules. Return exit code.

    Always editable, against the checkout it was invoked from: a non-editable
    install freezes the CLI at whatever revision was on disk at install time, so
    later `git pull`s on the module silently stop reaching the installed `sr`/`se`
    until someone thinks to reinstall by hand.
    """
    console.header("SushiStack Install-CLI")
    resolved = _resolve_names(names)
    if resolved is None:
        return 1
    root = workspace_root()

    sushicli = sushicli_dir(root)
    if sushicli is None:
        console.error(
            "sushicli checkout not found in the workspace, a linked path, or a "
            "sibling. The bootstrap normally fetches it; run it again, "
            "`ss link sushicli <path>`, or set SUSHICLI_DIR.")
        return 1

    try:
        pipx = _ensure_pipx()
    except RuntimeError as exc:
        console.error(str(exc))
        return 1

    failed = False
    for name in resolved:
        dest = module_dest(root, name)
        pkg_dir = dest / "cli"
        if not (pkg_dir / "pyproject.toml").is_file():
            console.warn(f"{name}: no cli/ package at {pkg_dir}; "
                         "clone or link the module first. Skipping.")
            failed = True
            continue
        dist = _dist_name(pkg_dir)
        console.info(f"{name}: installing {dist} from {pkg_dir}")
        rc = _run([*pipx, "install", "--force", "--editable", str(pkg_dir)])
        if rc == 0:
            # sushicli isn't a resolvable pip dependency; inject it (editable so
            # its edits apply without reinstalling the module CLI).
            rc = _run([*pipx, "inject", dist, "--editable", str(sushicli)])
        if rc != 0:
            console.error(f"{name}: install failed.")
            failed = True

    if failed:
        console.error("One or more module CLIs did not install. See messages above.")
        return 1
    console.success("Module CLI(s) installed. Open a new terminal if the command "
                    "is not yet on PATH.")
    return 0

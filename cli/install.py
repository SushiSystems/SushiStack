#!/usr/bin/env python3
"""Install the SushiStack `ss` CLI.

Usage:
    python cli/install.py            # install / upgrade (always editable)
    python cli/install.py --uninstall

Strategy:
  * All platforms -> pipx (isolated, puts `ss` on PATH; pipx is bootstrapped if absent).
  * Always installed --editable, against the workspace checkout at REPO_ROOT. `ss`
    is one half of a self-updating pair with `ss sync`/`ss update` (which pull this
    same checkout) -- a non-editable install would silently freeze `ss` at whatever
    revision was on disk when it was first installed, so every later fix would need
    a manual reinstall to take effect. There is no non-editable mode to opt into.

The CLI package directory is located automatically (the folder holding
pyproject.toml), so renaming the `cli/` folder later does not break this script.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PACKAGE_NAME = "sushistack-cli"
SUSHICLI_REPO_URL = "https://github.com/sushisystems/sushicli.git"


def find_sushicli_dir() -> Path:
	"""Locate (or fetch) the sushicli checkout (shared CLI presentation layer).

	Not published to any index, so pipx's isolated venv can't resolve it as a
	normal dependency — it's injected from source instead. Resolution order:
	SUSHICLI_DIR override, then the copy in the workspace (<workspace>/sushicli),
	then a sibling checkout (a developer's layout). If none is present it is
	cloned into the workspace so an end user never has to handle it — this makes
	`python cli/install.py` self-contained regardless of how it was invoked.
	"""
	override = os.environ.get("SUSHICLI_DIR")
	if override:
		return Path(override)
	for candidate in (REPO_ROOT / "sushicli", REPO_ROOT.parent / "sushicli"):
		if (candidate / "pyproject.toml").is_file():
			return candidate
	return clone_sushicli()


def clone_sushicli() -> Path:
	"""Clone sushicli into the workspace and return its path."""
	target = REPO_ROOT / "sushicli"
	repo_url = os.environ.get("SUSHICLI_REPO_URL", SUSHICLI_REPO_URL)
	print(f"[INFO] sushicli not found; cloning {repo_url} -> {target}")
	if run(["git", "clone", "--depth", "1", repo_url, str(target)]) != 0:
		sys.exit(
			"[ERROR] Failed to clone sushicli. Clone it into "
			f"{target} manually, or set SUSHICLI_DIR to an existing checkout."
		)
	if not (target / "pyproject.toml").is_file():
		sys.exit(f"[ERROR] Cloned sushicli but no pyproject.toml found in {target}.")
	return target


def find_package_dir() -> Path:
	"""Return the directory containing the CLI's pyproject.toml."""
	# Prefer common locations, then fall back to a shallow search.
	for name in ("cli", ".tools", "tools"):
		candidate = REPO_ROOT / name / "pyproject.toml"
		if candidate.is_file():
			return candidate.parent
	for pyproject in REPO_ROOT.glob("*/pyproject.toml"):
		return pyproject.parent
	sys.exit("[ERROR] Could not find the CLI package (no pyproject.toml under the repo root).")


def run(cmd: list[str]) -> int:
	print(f"[INFO] $ {' '.join(cmd)}")
	return subprocess.run(cmd).returncode


def ensure_pipx() -> str:
	"""Return a command prefix that runs pipx, installing it if necessary."""
	if subprocess.run([sys.executable, "-m", "pipx", "--version"],
	                   capture_output=True).returncode == 0:
		return f"{sys.executable} -m pipx"
	print("[INFO] pipx not found; installing it with pip...")
	
	cmd = [sys.executable, "-m", "pip", "install", "--user", "pipx"]
	pip_help = subprocess.run([sys.executable, "-m", "pip", "install", "--help"], 
	                          capture_output=True, text=True).stdout
	if "--break-system-packages" in pip_help:
		cmd.append("--break-system-packages")
		
	if run(cmd) != 0:
		sys.exit("[ERROR] Failed to install pipx.")
	run([sys.executable, "-m", "pipx", "ensurepath"])
	return f"{sys.executable} -m pipx"


def install() -> int:
	pkg_dir = find_package_dir()
	sushicli_dir = find_sushicli_dir()

	pipx = ensure_pipx().split()
	rc = run([*pipx, "install", "--force", "--editable", str(pkg_dir)])
	if rc == 0:
		# sushicli isn't a resolvable pip dependency (see pyproject.toml); inject
		# it into the venv pipx just created, always editable so future sushicli
		# edits apply without reinstalling this CLI.
		rc = run([*pipx, "inject", PACKAGE_NAME, "--editable", str(sushicli_dir)])

	if rc == 0:
		print("\n[SUCCESS] CLI installed. Try:  ss --help   (or: sushistack --help)")
		print("[NOTE] If `ss` is not found, open a new terminal "
		      "(pipx may have just added it to PATH).")
	else:
		print("\n[ERROR] Installation failed.")
	return rc


def uninstall() -> int:
	pipx = ensure_pipx().split()
	return run([*pipx, "uninstall", PACKAGE_NAME])


def main() -> None:
	parser = argparse.ArgumentParser(description="Install the SushiStack `ss` CLI.")
	parser.add_argument("--uninstall", action="store_true",
	                    help="Uninstall the CLI instead of installing.")
	args = parser.parse_args()
	sys.exit(uninstall() if args.uninstall else install())


if __name__ == "__main__":
	main()

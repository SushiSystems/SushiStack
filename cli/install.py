#!/usr/bin/env python3
"""Install the SushiStack `ss` CLI.

Usage:
    python cli/install.py            # install / upgrade
    python cli/install.py --editable # editable (dev) install
    python cli/install.py --uninstall

Strategy:
  * All platforms -> pipx (isolated, puts `ss` on PATH; pipx is bootstrapped if absent).

The CLI package directory is located automatically (the folder holding
pyproject.toml), so renaming the `cli/` folder later does not break this script.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PACKAGE_NAME = "sushistack-cli"


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


def install(editable: bool) -> int:
	pkg_dir = find_package_dir()

	pipx = ensure_pipx().split()
	cmd = [*pipx, "install", "--force", str(pkg_dir)]
	if editable:
		cmd.insert(cmd.index("install") + 1, "--editable")
	rc = run(cmd)

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
	parser.add_argument("--editable", action="store_true",
	                    help="Editable/development install.")
	parser.add_argument("--uninstall", action="store_true",
	                    help="Uninstall the CLI instead of installing.")
	args = parser.parse_args()
	sys.exit(uninstall() if args.uninstall else install(args.editable))


if __name__ == "__main__":
	main()

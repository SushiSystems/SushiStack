"""Dependency manifest reading.

The installer must not hard-code package names. Instead it asks an
``IDependencySource`` for the packages relevant to the current platform.

SushiStack owns no single manifest. Each module declares what it needs, and the
installer aggregates those fragments into one shared dependency set:

  * ``cli/manifests/*.deps.toml`` — base fragments shipped with the workspace
    (the module-independent build/toolchain infrastructure).
  * ``<module>/cli/sushistack.deps.toml`` — a fragment a module contributes from
    its own repo (kept under cli/, not the repo root).

When two fragments declare the same dependency name the first one wins and a
warning is emitted, so the union stays predictable. Tests can inject an
in-memory source.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:  # Python 3.10 fallback
    import tomli as tomllib

from .. import console
from ..config import config_dir, registered_modules, workspace_root

#: Path, relative to a module's repo root, of the fragment it contributes.
MODULE_MANIFEST_REL = Path("cli") / "sushistack.deps.toml"


@dataclass(frozen=True)
class Dependency:
    """One entry from the manifest, normalized."""

    name: str
    description: str
    required: bool
    gpu_only: bool
    linux_apt: list[str]
    windows_vcpkg: list[str]
    check_cmd: list[str]

    def packages_for(self, platform: str) -> list[str]:
        """Package names for the given platform ('windows' | other = linux)."""
        return self.windows_vcpkg if platform == "windows" else self.linux_apt


class IDependencySource(ABC):
    """Source of the dependency list. Abstraction the steps depend on."""

    @abstractmethod
    def all(self) -> list[Dependency]:
        """Every declared dependency, regardless of platform."""
        raise NotImplementedError

    def selected(self, platform: str, gpu: bool) -> list[Dependency]:
        """Dependencies relevant to this platform/GPU choice with packages.

        Filters out gpu-only entries when ``gpu`` is False and entries that
        declare no package for this platform.
        """
        out: list[Dependency] = []
        for dep in self.all():
            if dep.gpu_only and not gpu:
                continue
            if dep.packages_for(platform):
                out.append(dep)
        return out


def manifest_paths() -> list[Path]:
    """Every dependency-fragment file the installer should aggregate.

    Shipped fragments under ``cli/manifests/`` come first (sorted by name, so the
    aggregation order is stable), then each cloned module's own
    ``sushistack.deps.toml`` at the workspace root.
    """
    paths: list[Path] = []
    manifests_dir = config_dir() / "manifests"
    if manifests_dir.is_dir():
        paths.extend(sorted(manifests_dir.glob("*.deps.toml")))
    try:
        root = workspace_root()
    except SystemExit:
        root = None
    if root is not None:
        for module in sorted(p for p in root.iterdir() if p.is_dir()):
            fragment = module / MODULE_MANIFEST_REL
            if fragment.is_file():
                paths.append(fragment)
    # Modules linked to external checkouts (a developer's working repos that live
    # outside the workspace tree) contribute their fragment too.
    for module_path in registered_modules().values():
        fragment = Path(module_path) / MODULE_MANIFEST_REL
        if fragment.is_file() and fragment not in paths:
            paths.append(fragment)
    return paths


def _parse_manifest(path: Path) -> list[Dependency]:
    with path.open("rb") as fh:
        doc = tomllib.load(fh)
    deps: list[Dependency] = []
    for name, table in doc.items():
        if not isinstance(table, dict):
            continue
        deps.append(
            Dependency(
                name=name,
                description=str(table.get("description", "")),
                required=bool(table.get("required", True)),
                gpu_only=bool(table.get("gpu_only", False)),
                linux_apt=list(table.get("linux_apt", [])),
                windows_vcpkg=list(table.get("windows_vcpkg", [])),
                check_cmd=list(table.get("check_cmd", [])),
            )
        )
    return deps


class TomlDependencySource(IDependencySource):
    """Aggregates dependency fragments from across the workspace.

    ``paths`` can be injected (tests); otherwise the union of every fragment from
    ``manifest_paths()`` is read, with first-wins de-duplication by name.
    """

    def __init__(self, paths: list[Path] | None = None) -> None:
        self._paths = paths if paths is not None else manifest_paths()

    def all(self) -> list[Dependency]:
        if not self._paths:
            raise FileNotFoundError(
                "No dependency manifests found. Expected at least "
                "cli/manifests/*.deps.toml in the SushiStack workspace."
            )
        merged: dict[str, Dependency] = {}
        for path in self._paths:
            if not path.is_file():
                continue
            for dep in _parse_manifest(path):
                if dep.name in merged:
                    console.warn(
                        f"Duplicate dependency '{dep.name}' in {path.name} "
                        "ignored; the first fragment to declare it wins."
                    )
                    continue
                merged[dep.name] = dep
        return list(merged.values())

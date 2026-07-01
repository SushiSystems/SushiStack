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

#: Owner label for the base fragments under cli/manifests/ — the build/toolchain
#: infrastructure every module shares, owned by no single module.
SHARED_OWNER = "shared"

#: Reserved table name a fragment uses to declare module-level metadata
#: (currently ``depends_on``) rather than a dependency.
MODULE_META_TABLE = "module"


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
    owner: str = SHARED_OWNER  # which module contributed this dependency
    provides: str = ""  # capability tag; deps sharing one are any-of alternatives

    def packages_for(self, platform: str) -> list[str]:
        """Package names for the given platform ('windows' | other = linux)."""
        return self.windows_vcpkg if platform == "windows" else self.linux_apt


class IDependencySource(ABC):
    """Source of the dependency list. Abstraction the steps depend on."""

    @abstractmethod
    def all(self) -> list[Dependency]:
        """Every declared dependency, regardless of platform."""
        raise NotImplementedError

    def depends_on(self, module: str) -> list[str]:
        """Modules the given module directly builds on. Empty unless overridden."""
        return []

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


def manifest_sources() -> list[tuple[Path, str]]:
    """Every dependency fragment plus the module that owns it.

    Each entry is ``(path, owner)``: shipped fragments under ``cli/manifests/``
    are owned by :data:`SHARED_OWNER` (the module-independent build/toolchain
    infrastructure); a module's ``cli/sushistack.deps.toml`` is owned by the
    module's directory name. Shared fragments come first (sorted, stable order),
    then modules in the workspace, then linked external checkouts.
    """
    sources: list[tuple[Path, str]] = []
    manifests_dir = config_dir() / "manifests"
    if manifests_dir.is_dir():
        sources.extend((p, SHARED_OWNER) for p in sorted(manifests_dir.glob("*.deps.toml")))
    try:
        root = workspace_root()
    except SystemExit:
        root = None
    if root is not None:
        for module in sorted(p for p in root.iterdir() if p.is_dir()):
            fragment = module / MODULE_MANIFEST_REL
            if fragment.is_file():
                sources.append((fragment, module.name))
    # Modules linked to external checkouts (a developer's working repos that live
    # outside the workspace tree) contribute their fragment too.
    seen = {p for p, _ in sources}
    for name, module_path in registered_modules().items():
        fragment = Path(module_path) / MODULE_MANIFEST_REL
        if fragment.is_file() and fragment not in seen:
            sources.append((fragment, name))
            seen.add(fragment)
    return sources


def manifest_paths() -> list[Path]:
    """Every dependency-fragment file the installer should aggregate."""
    return [p for p, _ in manifest_sources()]


def _parse_manifest(path: Path, owner: str) -> tuple[list[Dependency], list[str]]:
    """Return this fragment's dependencies and its ``[module] depends_on`` list.

    The reserved ``[module]`` table carries module metadata (currently the
    ``depends_on`` list) rather than a dependency, so it is pulled out here and
    never becomes a :class:`Dependency`.
    """
    with path.open("rb") as fh:
        doc = tomllib.load(fh)
    depends_on: list[str] = []
    deps: list[Dependency] = []
    for name, table in doc.items():
        if not isinstance(table, dict):
            continue
        if name == MODULE_META_TABLE:
            depends_on = [str(m) for m in table.get("depends_on", [])]
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
                owner=owner,
                provides=str(table.get("provides", "")),
            )
        )
    return deps, depends_on


class TomlDependencySource(IDependencySource):
    """Aggregates dependency fragments from across the workspace.

    ``sources`` can be injected (tests) as ``(path, owner)`` pairs; otherwise the
    union of every fragment from :func:`manifest_sources` is read, with first-wins
    de-duplication by name. Alongside the merged dependencies it records, per
    owning module, which modules that module ``depends_on`` — so callers can
    reason about a module's *effective* dependency set (its own plus those it
    builds on).
    """

    def __init__(self, sources: list[tuple[Path, str]] | None = None) -> None:
        self._sources = sources if sources is not None else manifest_sources()
        self._depends_on: dict[str, list[str]] = {}

    def all(self) -> list[Dependency]:
        if not self._sources:
            raise FileNotFoundError(
                "No dependency manifests found. Expected at least "
                "cli/manifests/*.deps.toml in the SushiStack workspace."
            )
        merged: dict[str, Dependency] = {}
        for path, owner in self._sources:
            if not path.is_file():
                continue
            deps, depends_on = _parse_manifest(path, owner)
            if depends_on:
                self._depends_on.setdefault(owner, []).extend(depends_on)
            for dep in deps:
                if dep.name in merged:
                    console.warn(
                        f"Duplicate dependency '{dep.name}' in {path.name} "
                        "ignored; the first fragment to declare it wins."
                    )
                    continue
                merged[dep.name] = dep
        return list(merged.values())

    def depends_on(self, module: str) -> list[str]:
        """Modules the given module directly builds on (``[module] depends_on``).

        Populated as a side effect of :meth:`all`; call ``all()`` first (the
        readiness reporter does).
        """
        return self._depends_on.get(module, [])

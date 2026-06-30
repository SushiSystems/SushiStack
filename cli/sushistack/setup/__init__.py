"""SushiRuntime one-shot installer pipeline.

A SOLID, dependency-injected pipeline that takes a bare machine to a working
build: detect what is present, install what is missing (driven by the
``dependencies.toml`` manifest), generate ``config.local.toml``, then verify by
building and smoke-testing.

The public entry point is :func:`factory.build_pipeline`; everything else is an
implementation detail behind small interfaces (:class:`pipeline.Step`,
:class:`package_managers.IPackageManager`,
:class:`dependency_source.IDependencySource`).
"""

from __future__ import annotations

from .factory import build_pipeline, build_uninstall_pipeline
from .pipeline import InstallContext, InstallPipeline, Step, StepResult

__all__ = [
    "build_pipeline",
    "build_uninstall_pipeline",
    "InstallContext",
    "InstallPipeline",
    "Step",
    "StepResult",
]

"""`ss install` service: thin wrapper that runs the installer pipeline.

The CLI command parses flags, this builds the pipeline via the composition root,
runs it, and maps success to an exit code. All the real logic lives in the
``sushistack.setup`` package.
"""

from __future__ import annotations

from .. import console
from ..setup import build_pipeline, build_uninstall_pipeline


def run(step: str = "all", dry_run: bool = False,
        selection: dict[str, bool] | None = None) -> int:
    """Run one step (or the whole pipeline) and return a process exit code.

    By default everything is provisioned; ``selection`` (from --customize) narrows
    it per component.
    """
    detect_only = step == "detect"
    console.header("SushiStack Doctor" if detect_only else "SushiStack Install")
    if dry_run:
        console.info("Dry-run: showing actions without changing the system.")
    if not detect_only:
        if selection is None:
            console.info("Installing everything: intel/llvm + AdaptiveCpp + oneAPI + CUDA.")
        else:
            chosen = [k for k, v in selection.items() if v]
            console.info(f"Custom selection: {', '.join(chosen) if chosen else '(nothing)'}.")

    try:
        pipeline, ctx = build_pipeline(only=step, selection=selection, dry_run=dry_run)
    except (ValueError, FileNotFoundError) as exc:
        console.error(str(exc))
        return 1

    # Gather consent for the heavy Windows LLVM download up front — before the
    # progress spinner starts — so the prompt is actually answerable.
    if (not dry_run and step in ("all", "install", "provision") and ctx.install_acpp
            and ctx.cfg.is_windows):
        from ..setup.toolchains import (
            LLVM_WINDOWS_VERSION, _confirm_timeout, _find_windows_llvm,
        )
        if _find_windows_llvm() is None:
            ctx.assume_acpp_llvm = _confirm_timeout(
                f"[bold yellow]AdaptiveCpp needs LLVM {LLVM_WINDOWS_VERSION} "
                "(a ~2-3 GB download) to build on Windows.[/bold yellow]\n"
                "Install it now into the deps folder?",
                default=False,
            )
            if not ctx.assume_acpp_llvm:
                console.info("Skipping the LLVM download. Re-run `ss install` to retry, "
                             "or `ss install --customize` and deselect AdaptiveCpp.")

    ok = pipeline.run(ctx, show_progress=not detect_only)
    if ok:
        console.success("Inventory complete." if detect_only else "Install completed.")
        if step in ("all", "provision", "configure"):
            console.info("Next: `ss add sushiruntime` then build it with `sr build`, "
                         "or `ss status` to see what is installed.")
        return 0
    console.error("Inventory failed." if detect_only else "Install did not complete. See messages above.")
    return 1


def uninstall(
    gpu: bool = False,
    dry_run: bool = False,
    everything: bool = False,
) -> int:
    """Remove packages and config files placed by `ss install`. Return exit code."""
    console.header("SushiStack Remove")
    if dry_run:
        console.info("Dry-run: showing actions without changing the system.")
    if everything:
        console.warn(
            "--all wipes the whole shared dependencies/ tree (toolchains, vcpkg, "
            "portable cmake/ninja). Your system git/cmake are NOT touched."
        )

    try:
        pipeline, ctx = build_uninstall_pipeline(
            gpu=gpu, dry_run=dry_run, everything=everything,
        )
    except (ValueError, FileNotFoundError) as exc:
        console.error(str(exc))
        return 1

    ok = pipeline.run(ctx)
    if ok:
        console.success("Uninstall completed.")
        return 0
    console.error("Uninstall did not complete cleanly. See messages above.")
    return 1

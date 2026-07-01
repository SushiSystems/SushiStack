"""SushiStack developer CLI (`ss`).

The umbrella that provisions one shared dependency tree for the whole stack and
manages the module checkouts (sushiruntime, sushiengine, …) that live inside the
workspace. Each module keeps its own CLI (`sr`, `se`) for building and testing;
`ss` only owns downloading, installing, and module lifecycle.

Thin Typer layer: commands parse arguments and delegate to the service layer in
``sushistack.services``.
"""

from __future__ import annotations

from typing import List, Optional

import typer

from .services import modules as modules_svc
from .services import setup as setup_svc

app = typer.Typer(
    name="ss",
    help="SushiStack CLI — one shared dependency tree and module manager for the stack.",
    no_args_is_help=True,
    add_completion=False,
    rich_markup_mode="rich",
)


# --------------------------------------------------------------------------- #
# workspace
# --------------------------------------------------------------------------- #
@app.command("init")
def init():
    """Turn the current directory into a SushiStack workspace.

    Writes the [cyan].sushistack[/cyan] marker, ensures [cyan].gitignore[/cyan]
    excludes the shared [cyan]dependencies/[/cyan] tree and module checkouts, and
    creates the dependency directory. Run this once after cloning sushistack.
    """
    raise typer.Exit(modules_svc.init())


@app.command("home")
def home():
    """Print the resolved workspace root and dependency directory."""
    from .config import deps_dir, workspace_root
    typer.echo(str(workspace_root()))
    typer.echo(f"dependencies: {deps_dir()}")
    raise typer.Exit(0)


@app.command("status")
def status():
    """Show which modules are cloned and whether dependencies are present."""
    raise typer.Exit(modules_svc.status())


# --------------------------------------------------------------------------- #
# modules
# --------------------------------------------------------------------------- #
@app.command("add")
def add(
    modules: List[str] = typer.Argument(
        ..., help="Modules to clone: sushiruntime | sushiengine | sushiai | sushiblas | all."),
):
    """Clone one or more stack modules into the workspace."""
    raise typer.Exit(modules_svc.add(modules))


@app.command("link")
def link(
    module: str = typer.Argument(
        ..., help="Module name: sushiruntime | sushiengine | sushiai | sushiblas."),
    path: str = typer.Argument(..., help="Path to an existing checkout of that module."),
):
    """Register an existing checkout (outside the workspace) as a module.

    For developers whose working repos live elsewhere: `ss` then aggregates that
    checkout's dependencies and tracks it, with no second clone. The module's own
    CLI resolves the shared deps via SUSHISTACK_HOME.
    """
    raise typer.Exit(modules_svc.link(module, path))


@app.command("install-cli")
def install_cli(
    modules: List[str] = typer.Argument(
        ..., help="Modules whose CLI to install: sushiruntime | sushiengine | "
                  "sushiai | sushiblas | all."),
):
    """Install a module's developer CLI (`sr`, `se`) into an isolated pipx venv.

    The single install seam for the stack: no module ships its own bootstrap
    script. This installs the module's [cyan]cli/[/cyan] package and injects the
    shared [cyan]sushicli[/cyan] presentation layer from its sibling checkout.

    Always installed editable, against the checkout it was invoked from -- a
    non-editable install would freeze the CLI at whatever revision existed at
    install time, so `git pull`s on the checkout would silently stop reaching it.
    """
    from .services import cli_install as cli_install_svc
    raise typer.Exit(cli_install_svc.install_cli(modules))


@app.command("update")
def update(
    modules: Optional[List[str]] = typer.Argument(
        None, help="Modules to update (omit for all present modules)."),
):
    """Fast-forward (`git pull`) the workspace and the present modules (cloned or linked)."""
    raise typer.Exit(modules_svc.update(modules))


# --------------------------------------------------------------------------- #
# dependencies
# --------------------------------------------------------------------------- #
@app.command("install")
def install(
    customize: bool = typer.Option(
        False, "--customize",
        help="Pick which components to install in an interactive TUI instead of "
             "installing everything."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show, don't change."),
):
    """Provision the shared dependencies into the workspace's dependencies/ tree.

    Installs everything by default — all three SYCL toolchains (intel/llvm,
    AdaptiveCpp, oneAPI) plus CUDA. SYCL is a heavy ecosystem; a missing toolchain
    only causes confusion later. Use [bold]--customize[/bold] to choose a subset.
    """
    selection = None
    if customize:
        from .services import customize as customize_svc
        selection = customize_svc.choose_components()
        if selection is None:
            raise typer.Exit(1)
    raise typer.Exit(setup_svc.run("provision", dry_run=dry_run, selection=selection))


@app.command("sync")
def sync(
    dry_run: bool = typer.Option(False, "--dry-run", help="Show, don't change."),
):
    """Bring the workspace up to date: install missing deps, then update modules."""
    raise typer.Exit(modules_svc.sync(dry_run=dry_run))


@app.command("doctor")
def doctor():
    """Inventory tools, compilers, and dependencies; report what is missing."""
    raise typer.Exit(setup_svc.run("detect", dry_run=False))


@app.command("remove")
def remove(
    all: bool = typer.Option(
        False, "--all",
        help="[bold red]Wipe everything[/bold red]: vcpkg ports, downloaded "
             "toolchains (intel/llvm + AdaptiveCpp + oneAPI, several GB), and the "
             "portable cmake/ninja — the whole dependencies/ tree."),
    gpu: bool = typer.Option(False, "--gpu", help="Include GPU-only deps in removal."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show what would be removed."),
):
    """Remove provisioned dependencies. Use [bold]--all[/bold] to reclaim the lot."""
    raise typer.Exit(setup_svc.uninstall(gpu=gpu, dry_run=dry_run, everything=all))


if __name__ == "__main__":
    app()

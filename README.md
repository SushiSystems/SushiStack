# SushiStack

A shared workspace for the Sushi stack. Installs the toolchains and libraries all modules need into one `dependencies/` directory and manages the module checkouts.

Each module has its own CLI (`sr`, `se`) for building and testing. `ss` only handles dependencies and module lifecycle.

## Layout

```
sushistack/
  cli/                     ← the `ss` CLI
    manifests/             ← dependency fragments (*.deps.toml)
  dependencies/            ← toolchains, vcpkg, cmake/ninja (git-ignored)
  sushicli/                ← shared CLI presentation layer (fetched automatically)
  sushiruntime/            ← added by `ss add sushiruntime`
  sushiengine/             ← added by `ss add sushiengine`
  .sushistack              ← workspace marker
```

Modules resolve their compiler and vcpkg from `../dependencies`.

## Install

One command on a fresh machine installs Python and Git if they are missing,
clones the workspace, installs the `ss` CLI, fetches the shared `sushicli`
presentation layer, and downloads the toolchains and libraries into
`dependencies/`:

```bash
curl -fsSL https://sushisystems.io/install.sh | bash      # Linux / WSL
```

```powershell
irm https://sushisystems.io/install.ps1 | iex             # Windows (PowerShell)
```

On Windows use `irm` (Invoke-RestMethod), not `curl` — in PowerShell `curl` is
an alias for `Invoke-WebRequest` and does not pipe a script the same way.

### Manual, step by step

```bash
git clone https://github.com/sushisystems/sushistack.git
cd sushistack
python cli/install.py                # install the `ss` CLI via pipx

ss init                              # write the .sushistack marker and .gitignore entries
ss install                           # download toolchains and libraries
ss add sushiruntime sushiengine      # clone modules into the workspace (aliases: sr se)
ss install-cli sushiruntime          # install that module's own CLI (`sr`)

cd sushiruntime && sr build
```

## `ss` commands

| Command | What it does |
|---|---|
| `ss init` | Write the `.sushistack` workspace marker and add `dependencies/` to `.gitignore`. |
| `ss install [--customize] [--dry-run]` | Download and install shared dependencies. `--customize` opens an interactive picker to select which toolchains to install. |
| `ss add <sushiruntime\|sushiengine\|sushiai\|sushiblas\|all>` | Clone one or more modules into the workspace. Aliases: `sr`, `se`, `sa`, `sb`. |
| `ss link <module> <path>` | Register an existing checkout outside the workspace as a module (no clone). Also accepts `sushicli` to point at your own checkout. |
| `ss install-cli <module…> [--no-editable]` | Install a module's own developer CLI (`sr`, `se`) into an isolated pipx venv and inject `sushicli`. |
| `ss update [module…]` | Run `git pull --ff-only` on present modules (cloned or linked). Omit arguments to update all. |
| `ss sync [--dry-run]` | Install missing dependencies, then update all modules. |
| `ss status` | Show which modules are present and whether dependencies are installed. |
| `ss doctor` | Check tools, compilers, and dependencies; report what is missing. |
| `ss remove [--gpu] [--all] [--dry-run]` | Remove installed dependencies. `--all` removes the entire `dependencies/` tree. |
| `ss home` | Print the workspace root and the `dependencies/` path. |

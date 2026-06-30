# SushiStack

A shared workspace for the Sushi stack. Installs the toolchains and libraries all modules need into one `dependencies/` directory and manages the module checkouts.

Each module has its own CLI (`sr`, `se`) for building and testing. `ss` only handles dependencies and module lifecycle.

## Layout

```
sushistack/
  cli/                     ← the `ss` CLI
    manifests/             ← dependency fragments (*.deps.toml)
  dependencies/            ← toolchains, vcpkg, cmake/ninja (git-ignored)
  sushiruntime/            ← added by `ss add runtime`
  sushiengine/             ← added by `ss add engine`
  .sushistack              ← workspace marker
```

Modules resolve their compiler and vcpkg from `../dependencies`.

## Quick start

```bash
git clone https://github.com/sushisystems/sushistack.git
cd sushistack
python cli/install.py       # installs the `ss` CLI via pipx

ss init                     # write .sushistack marker and .gitignore entries
ss install                  # download toolchains and libraries
ss add runtime engine       # clone modules into the workspace

cd sushiruntime && sr build
```

One-shot installers:

```bash
curl -fsSL https://sushisystems.io/install.sh | bash      # Linux / WSL
irm https://sushisystems.io/install.ps1 | iex             # Windows
```

## `ss` commands

| Command | What it does |
|---|---|
| `ss init` | Write the `.sushistack` workspace marker and add `dependencies/` to `.gitignore`. |
| `ss install [--customize] [--dry-run]` | Download and install shared dependencies. `--customize` opens an interactive picker to select which toolchains to install. |
| `ss add <runtime\|engine\|ai\|blas\|all>` | Clone one or more modules into the workspace. |
| `ss link <module> <path>` | Register an existing checkout outside the workspace as a module. |
| `ss update [module…]` | Run `git pull` on cloned modules. Omit arguments to update all. |
| `ss sync [--dry-run]` | Install missing dependencies, then update all modules. |
| `ss status` | Show which modules are present and whether dependencies are installed. |
| `ss doctor` | Check tools, compilers, and dependencies; report what is missing. |
| `ss remove [--gpu] [--all] [--dry-run]` | Remove installed dependencies. `--all` removes the entire `dependencies/` tree. |
| `ss home` | Print the workspace root and the `dependencies/` path. |

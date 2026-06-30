# SushiStack

The umbrella workspace for the Sushi technology stack. One shared dependency
tree, one installer, every module beside it.

SushiStack owns no compute and no math — it only **provisions** the toolchain and
libraries the stack needs and **manages the module checkouts** that live inside
it (SushiRuntime, SushiEngine, SushiAI, SushiBLAS, …). Each module keeps its own
CLI for building and testing (`sr`, `se`); `ss` keeps everything they share in
one place so nothing is downloaded twice and one command reclaims it all.

## Layout

```
sushistack/                ← clone this first
  cli/                     ← the `ss` CLI and dependency engine
    manifests/             ← per-module dependency fragments (*.deps.toml)
  dependencies/            ← shared toolchains, vcpkg, cmake/ninja (git-ignored)
  sushiruntime/            ← `ss add runtime`
  sushiengine/             ← `ss add engine`
  .sushistack              ← workspace marker
```

The whole `dependencies/` tree is shared by every module, so `sr` and `se`
resolve their compiler and vcpkg from `../dependencies` instead of provisioning
their own.

## Quick start

```bash
# 1. Clone the workspace and put `ss` on PATH.
git clone https://github.com/sushisystems/sushistack.git
cd sushistack
python cli/install.py            # installs the `ss` CLI via pipx

# 2. Provision the shared dependencies and pull in the modules you want.
ss init                          # mark the workspace, set up .gitignore
ss install                      # download the toolchain + libraries
ss add runtime engine            # clone the modules into the workspace

# 3. Build a module with its own CLI.
cd sushiruntime && sr build
```

Or in one shot, hosted at sushisystems.io:

```bash
curl -fsSL https://sushisystems.io/install.sh | bash      # Linux / WSL
irm https://sushisystems.io/install.ps1 | iex             # Windows
```

## `ss` commands

| Command | What it does |
|---|---|
| `ss init` | Mark the current directory as a workspace; set up `.gitignore` and `dependencies/`. |
| `ss install [--profile P] [--gpu]` | Provision the shared dependencies. |
| `ss add <runtime\|engine\|ai\|blas\|all>` | Clone stack modules into the workspace. |
| `ss update [module…]` | Fast-forward (`git pull`) cloned modules. |
| `ss sync` | Install missing deps, then update modules — bring everything current. |
| `ss status` | Show which modules are cloned and whether deps are present. |
| `ss doctor` | Inventory tools, compilers, and deps; report what is missing. |
| `ss remove [--all]` | Remove provisioned deps; `--all` wipes the whole tree. |
| `ss home` | Print the resolved workspace root. |

Install profiles: `lightweight` (AdaptiveCpp only), `normal` (intel-llvm +
AdaptiveCpp, default), `full` (+ Intel oneAPI).

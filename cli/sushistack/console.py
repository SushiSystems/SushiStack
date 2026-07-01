"""CLI output for the SushiStack CLI.

Thin wrapper around :mod:`sushicli` — the actual theme/icon/renderer logic
(and its `[cli]` config schema) lives there and is shared with the
sushiruntime and sushiengine CLIs. See sushicli's README to change colors.
"""

from __future__ import annotations

from sushicli import build_console

from .config import config_dir

_cfg_dir = config_dir()
_console = build_console([_cfg_dir / "config.toml", _cfg_dir / "config.local.toml"])

# Raw Rich console, for callers that print a Table/Panel or other Rich
# renderable directly instead of going through the semantic helpers below.
console = _console.console

info = _console.info
success = _console.success
warn = _console.warn
error = _console.error
command = _console.command
header = _console.header
fail_panel = _console.fail_panel
accent = _console.accent

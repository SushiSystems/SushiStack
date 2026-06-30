"""Interactive component picker for `ss install --customize`.

Everything installs by default; this is the escape hatch for users who want a
subset. The picker lays the optional components out as cards side by side — like a
single horizontal row of choices — each with a big checkbox above it. Arrow keys
move between cards, space toggles the focused one, enter continues, and a final
confirmation guards against an accidental enter.

It captures keys directly (msvcrt on Windows, termios on Unix) and renders with
rich, so it needs no extra dependency. A non-interactive stdin (a pipe) falls
back to installing everything.
"""

from __future__ import annotations

import sys

from rich.align import Align
from rich.console import Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from .. import console
from ..config import CUSTOMIZABLE_COMPONENTS


def _getch() -> tuple[str, str]:
    """Read one keypress as (kind, value): kind is 'char' or 'arrow'.

    Arrow values are normalised to 'left'|'right'|'up'|'down'.
    """
    try:
        import msvcrt  # Windows
        ch = msvcrt.getwch()
        if ch in ("\x00", "\xe0"):  # arrow / function key prefix
            code = msvcrt.getwch()
            return ("arrow", {"K": "left", "M": "right", "H": "up", "P": "down"}.get(code, ""))
        return ("char", ch)
    except ImportError:
        import termios
        import tty
        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        try:
            tty.setraw(fd)
            ch = sys.stdin.read(1)
            if ch == "\x1b":  # escape — could be a bare ESC or an arrow sequence
                seq = sys.stdin.read(2)
                return ("arrow", {"[D": "left", "[C": "right", "[A": "up", "[B": "down"}.get(seq, "esc"))
            return ("char", ch)
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)


def _checkbox(checked: bool) -> Panel:
    """A big checkbox box: filled green when checked."""
    glyph = "X" if checked else " "
    return Panel(
        Text(glyph, justify="center", style="bold"),
        width=9,
        padding=(0, 1),
        border_style="green" if checked else "grey50",
        style="on green" if checked else "",
    )


def _card(key: str, label: str, checked: bool, focused: bool) -> Panel:
    body = Group(
        Align.center(_checkbox(checked)),
        Align.center(Text(key, style="bold cyan" if focused else "bold")),
        Align.center(Text(label, style="dim", justify="center")),
    )
    return Panel(
        body,
        width=24,
        padding=(1, 1),
        border_style="bold cyan" if focused else "grey37",
        title="[bold]▸[/bold]" if focused else "",
    )


def _render(items, checked, focus):
    cards = [_card(k, lbl, checked[i], i == focus) for i, (k, lbl, _f) in enumerate(items)]
    row = Table.grid(padding=(0, 1))
    row.add_row(*cards)
    head = Text("Select the components to install", style="bold magenta", justify="center")
    foot = Text("←/→ move    space toggle    enter continue    esc cancel",
                style="dim", justify="center")
    return Align.center(Group(head, Text(""), Align.center(row), Text(""), foot))


def _selection_from_checked(items, checked) -> dict[str, bool]:
    enabled = {items[i][2] for i in range(len(items)) if checked[i]}
    return {field: (field in enabled) for _k, _l, field in CUSTOMIZABLE_COMPONENTS}


def _confirm(items, checked) -> bool:
    chosen = [items[i][0] for i in range(len(items)) if checked[i]]
    console.console.print()
    if chosen:
        console.console.print("[bold]These will be installed:[/bold] "
                              + ", ".join(f"[green]{c}[/green]" for c in chosen))
    else:
        console.warn("Nothing selected — this installs no toolchains.")
    answer = input("Are you sure? [y/N] ").strip().lower()
    return answer in ("y", "yes")


def choose_components() -> dict[str, bool] | None:
    """Run the picker. Return the selection, or None if the user cancelled."""
    from rich.live import Live

    items = list(CUSTOMIZABLE_COMPONENTS)
    if not sys.stdin.isatty():
        console.warn("Not a TTY; --customize needs an interactive terminal. "
                     "Proceeding with everything.")
        return _selection_from_checked(items, [True] * len(items))

    checked = [True] * len(items)
    focus = 0
    while True:  # selection -> confirm; loop back if not confirmed
        with Live(_render(items, checked, focus), console=console.console,
                  auto_refresh=False, screen=True) as live:
            while True:
                kind, val = _getch()
                if kind == "arrow" and val == "left":
                    focus = (focus - 1) % len(items)
                elif kind == "arrow" and val == "right":
                    focus = (focus + 1) % len(items)
                elif kind == "char" and val == " ":
                    checked[focus] = not checked[focus]
                elif kind == "char" and val in ("\r", "\n"):
                    break  # continue to confirmation
                elif (kind == "char" and val in ("\x1b", "q")) or (kind == "arrow" and val == "esc"):
                    return None
                live.update(_render(items, checked, focus), refresh=True)
        if _confirm(items, checked):
            return _selection_from_checked(items, checked)
        # not confirmed: drop back into the picker so they can adjust.

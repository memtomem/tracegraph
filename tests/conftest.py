"""Shared pytest setup.

Force a color-free terminal for the whole suite so CLI-output substring assertions are
robust under any local or CI color setting. Two Rich consoles can colorize, on different
triggers:

* typer's *internal* error console (the BadParameter panel on stderr) — forced to color when
  ``GITHUB_ACTIONS`` / ``FORCE_COLOR`` / ``PY_COLORS`` is set (typer's ``FORCE_TERMINAL``).
* the app's own standalone ``Console()`` (the query result/summary on stdout) — a plain Rich
  Console, which honors ``FORCE_COLOR`` (and ``NO_COLOR``) but *not* ``GITHUB_ACTIONS`` /
  ``PY_COLORS``. So real CI colors only the error panel, while a dev's ``FORCE_COLOR=1`` also
  colors the stdout summary.

Either way the escapes can land between the words of an asserted phrase (e.g. ``3 match(es)``
→ ``\\x1b[1;2;36m3\\x1b[0m match…``) and break a raw substring check. ``TERM=dumb`` disables
color on *both* consoles regardless of those flags; we also drop ``FORCE_COLOR`` / ``PY_COLORS``
so nothing re-enables it. This deliberately mutates the process environment at import time —
before any ``Console`` reads it — rather than in a fixture (which would run too late); the
pytest process is ephemeral so the unrestored env is harmless. (Box-drawing/wrapping still
happens; ``_panel_text`` collapses that and also strips escapes defensively, in case a single
test module is run without this conftest.)
"""

import os

os.environ["TERM"] = "dumb"
os.environ.pop("FORCE_COLOR", None)
os.environ.pop("PY_COLORS", None)

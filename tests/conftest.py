"""Shared pytest setup.

Force a color-free terminal for the whole suite so CLI-output substring assertions are
robust under any local or CI color setting. Rich — both the app's ``Console`` (the query
result/summary on stdout) and typer's *internal* error console (the BadParameter panel on
stderr) — emits ANSI/SGR escapes whenever color is forced, by CI (``FORCE_COLOR`` /
``PY_COLORS`` / ``GITHUB_ACTIONS``) or a developer's shell. Those escapes can land between
the words of an asserted phrase (e.g. ``3 match(es)`` → ``\\x1b[1;2;36m3\\x1b[0m match…``) and
break a raw substring check that passes locally with no color.

``TERM=dumb`` disables color on every Rich console regardless of those flags; we also drop
``FORCE_COLOR`` / ``PY_COLORS`` so nothing re-enables it. This must run at import time —
before any ``Console`` is constructed — so it lives at conftest module level, not in a
fixture. (Box-drawing/wrapping still happens; the ``_panel_text`` helper handles that, and
also strips escapes defensively in case a single test module is run without this conftest.)
"""

import os

os.environ["TERM"] = "dumb"
os.environ.pop("FORCE_COLOR", None)
os.environ.pop("PY_COLORS", None)

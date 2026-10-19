"""Describe an exception raised by code tracegraph does not control, without running more of it.

A boundary that promises ``ValueError`` often names the exception it caught. Doing that runs the
caller's code again: ``str(exc)`` calls its ``__str__``, and a metaclass can make the type's
``__name__`` a property. Either can raise, or return a ``str`` subclass whose own methods raise
while the message is built, and that error would escape in place of the ``ValueError``.
"""

from __future__ import annotations

#: Stands in for a type name that cannot be read safely.
_UNNAMED = "exception"


def type_name(exc: BaseException) -> str:
    """The exception's type name, or a fixed text when reading it fails or is not a plain str."""
    try:
        name = type(exc).__name__
    except KeyboardInterrupt:
        raise
    except BaseException:
        return _UNNAMED
    return name if type(name) is str else _UNNAMED


def describe(exc: BaseException) -> str:
    """``"Type: message"``, or the type alone when the message is empty or cannot be read safely."""
    name = type_name(exc)
    try:
        text = str(exc)
        return f"{name}: {text}" if type(text) is str and text else name
    except KeyboardInterrupt:
        raise
    except BaseException:  # the composition too: building the text can fail (MemoryError)
        return name

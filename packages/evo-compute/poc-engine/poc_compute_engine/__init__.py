"""Runtime package root.

At runtime the ``geostatistics`` topic (and any future topic) is resolved
dynamically via PEP 562 module ``__getattr__`` — there is no real submodule.
A type checker, however, reads ``__init__.pyi`` instead of this file, so the
*static* surface is whatever the generated stubs declare. That split is the
whole point of the POC:

    runtime  -> dynamic, live, covers every discovered task
    static   -> generated stubs, point-in-time, give IDE autocomplete/checks
"""

from __future__ import annotations

from ._engine import get_topic, list_topics


def __getattr__(name: str):
    if name in list_topics():
        return get_topic(name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__():
    # Topics drive tab-completion at runtime; the generated stub drives it statically.
    return sorted(list_topics())

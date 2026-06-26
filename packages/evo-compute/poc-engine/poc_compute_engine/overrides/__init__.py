"""Per-task overrides.

A module ``poc_compute_engine.overrides.<topic>.<task>`` is auto-discovered by the
engine (convention import) and, if present, OWNS that task: its hand-written,
fully-typed ``run`` and result classes replace the generic schema-synthesised
surface — transparently to callers (no client code change) and reflected in the
generated stubs (which re-export the override instead of a schema-derived stub).

This is the "graceful capability gradient": most tasks ride the generic engine;
a task that needs bespoke validation, richer results, or extra helpers can be
upgraded in place by dropping a module here.
"""

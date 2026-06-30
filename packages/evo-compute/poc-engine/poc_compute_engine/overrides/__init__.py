"""Per-task overrides — the "graceful capability gradient".

Most tasks ride the **fully generic** engine: their ``run(...)`` signature and result
type are synthesised from the live discovery schema, with zero per-task Python. But a
task that needs something the schema cannot express — bespoke validation, a richer
hand-curated result type, extra helper methods, parameters beyond the schema — can be
*upgraded in place* by dropping a module here:

    poc_compute_engine/overrides/<topic>/<task>.py

The engine auto-discovers it by **convention import** (``importlib.import_module``) on
first access to ``client.<topic>.<task>``. If the module exists and exposes a
``bind(client, spec)`` factory, it OWNS that task — its runner replaces the generic
proxy transparently (no client-code change: ``client.<topic>.<task>.run(...)`` is
unchanged) and the generated stub re-exports its hand-written types instead of the
schema-derived ones. If no override module exists, the generic engine handles the task.

Crucially an override is *not* a parallel runtime: it still executes through the same
authenticated ``ComputeClient`` plumbing (``client._submit`` -> real
``evo.compute.JobClient``) and reuses the engine's shared helpers. It only adds the
typed shell and the task-specific behaviour.

See ``geostatistics/kriging.py`` for a worked example.
"""

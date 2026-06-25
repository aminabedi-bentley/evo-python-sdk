"""Runtime demo — run with:  python demo.py

Shows that WITHOUT any per-task class, the generic engine:
  1. lists topics/tasks dynamically from the (mock) discovery response,
  2. exposes a real per-task signature at runtime (Jupyter `run?` / inspect),
  3. executes poc_compute_engine.geostatistics.kriging.run(...) and returns a typed result object (with get_object/to_dataframe),
  4. still RUNS a task added to discovery *after* stubs were generated
     (live breadth) — even though no stub exists for it.
"""

from __future__ import annotations

import inspect

import poc_compute_engine
from poc_compute_engine.mock_discovery import register_runtime_task

print("== 1. Dynamic discovery (no per-task code) ==")
print("dir(poc_compute_engine):           ", [n for n in dir(poc_compute_engine) if not n.startswith('_')])
print("dir(.geostatistics):        ", dir(poc_compute_engine.geostatistics))  # tab-completion source

print("\n== 2. Runtime signature built from the schema ==")
run = poc_compute_engine.geostatistics.kriging.run
print("signature:                  run", inspect.signature(run))
print("docstring:                  ", (run.__doc__ or '').splitlines()[0])

print("\n== 3. Execute -> schema-driven typed result (resolution mocked) ==")
result = poc_compute_engine.geostatistics.kriging.run(
    source="grade",
    target="kriged_grade",
    variogram="vario-123",
    max_samples=16,
)
print("message:                    ", result.message)
print("target object reference:    ", result.target.reference)
print("target schema_id:           ", result.target.schema_id)
# Output attribute reference is resolved from attribute_path[schema_id] in the
# LIVE results schema -> self-heals if the platform changes that expression.
print("output attribute (resolved):", result.target.attribute.reference)
obj = result.target.get_object()  # generic load dispatched on schema_id
print("loaded object:              ", obj)
print("as dataframe:               ", result.target.to_dataframe())

print("\n== 4. Missing required arg is caught at runtime ==")
try:
    poc_compute_engine.geostatistics.kriging.run(source="grade")  # no target/variogram
except TypeError as exc:
    print("TypeError:", exc)

print("\n== 5. LIVE breadth: a task advertised AFTER stubs were generated ==")
register_runtime_task(
    {
        "topic": "geostatistics",
        "name": "turning_bands",
        "version": "0.1.0",
        "feature_flag": "preview",
        "description": "Conditional simulation via turning bands (newly advertised).",
        "parameters": {
            "type": "object",
            "properties": {
                "source": {"type": "string", "reference_to": "attribute"},
                "target": {"type": "string", "reference_to": "attribute"},
                "realisations": {"type": "integer", "default": 10},
            },
            "required": ["source", "target"],
        },
    }
)
print("dir(.geostatistics) now:    ", dir(poc_compute_engine.geostatistics))
sim = poc_compute_engine.geostatistics.turning_bands.run(source="grade", target="sim", realisations=5)
print("turning_bands runs live:    ", sim.message)
print("(No stub AND no results schema -> runs, message-only result, no get_object/typed DX.)")

"""Runtime demo — run with:  python demo.py

Shows the architecture end to end:
  1. tasks come from TWO sources unioned by the engine:
       * offline bundled schemas (schemas/<topic>/<task>/schema.json) -> have stubs
       * live discovery (mock_discovery) -> stub-less breadth
  2. kriging is routed to a hand-written OVERRIDE (convention import) -> richer
     typed result with task-specific helpers (.summary(), .portal_url()),
  3. declustering & normal_score have NO override -> the generic schema-driven
     engine synthesises run(...) and a schema-shaped result,
  4. a task added to discovery AFTER stubs were generated still RUNS (live breadth).
"""

from __future__ import annotations

import inspect

import poc_compute_engine
from poc_compute_engine.mock_discovery import register_runtime_task

print("== 1. Catalogue = bundled schemas + live discovery ==")
print("dir(poc_compute_engine):    ", [n for n in dir(poc_compute_engine) if not n.startswith('_')])
print("dir(.geostatistics):        ", dir(poc_compute_engine.geostatistics))  # tab-completion source
print("kriging proxy:              ", repr(poc_compute_engine.geostatistics.kriging))
print("declustering proxy:         ", repr(poc_compute_engine.geostatistics.declustering))

print("\n== 2. kriging -> OVERRIDE (hand-written, richer result) ==")
run = poc_compute_engine.geostatistics.kriging.run
print("signature:                  run", inspect.signature(run))
result = poc_compute_engine.geostatistics.kriging.run(
    source="grade",
    target="kriged_grade",
    variogram="vario-123",
    max_samples=16,
)
print("message:                    ", result.message)
print("summary() [override-only]:  ", result.summary())
print("portal_url() [override-only]", result.portal_url())
print("target object reference:    ", result.target.reference)
print("output attribute:           ", result.target.attribute.name)
print("as dataframe:               ", result.target.to_dataframe())

print("\n== 2b. Override adds validation the schema can't express ==")
try:
    poc_compute_engine.geostatistics.kriging.run(
        source="grade", target="t", variogram="v", max_samples=0
    )
except ValueError as exc:
    print("ValueError:", exc)

print("\n== 3. declustering & normal_score -> GENERIC engine (no override) ==")
declus = poc_compute_engine.geostatistics.declustering.run(source="grade", target="weights", cell_size=50.0)
print("declustering message:       ", declus.message)
print("declustering attribute:     ", declus.target.attribute.reference)
ns = poc_compute_engine.geostatistics.normal_score.run(source="grade", target="grade_ns")
print("normal_score message:       ", ns.message)
print("normal_score object:        ", ns.target.get_object())

print("\n== 4. Missing required arg is caught at runtime (generic path) ==")
try:
    poc_compute_engine.geostatistics.declustering.run(source="grade")  # no target
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
print("(Not bundled, no stub, no results schema -> runs, message-only, no typed DX.)")

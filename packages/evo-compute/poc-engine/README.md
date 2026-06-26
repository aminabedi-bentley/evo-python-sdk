# POC: generated stubs over a generic runtime engine (+ per-task overrides)

A self-contained proof of concept for *"Static DX in the proposed generic compute
engine: generated type stubs, with an override seam for tasks that need it."*

It demonstrates two claims at once:

1. **Generic by default** — execution stays generic and schema-driven, while a
   generated `.pyi` stub gives full static IDE/type-checker DX. Same call shape
   either way: `poc_compute_engine.geostatistics.<task>.run(...)`.
2. **Override when it pays** — a single task (`kriging`) is upgraded in place by a
   hand-written override module. The engine routes to it automatically (convention
   import), and the generated stub re-exports the override's richer typed surface —
   **no client code change**. The other two tasks stay stub-only generic.

## Two task sources, unioned by the engine

* **Offline bundled schemas** — `schemas/<topic>/<task>/schema.json` (a snapshot of
  `core-compute-tasks`). This single snapshot drives BOTH stub generation and runtime
  for bundled tasks, so they can never drift. `topic`/`name` come from the path.
* **Live discovery** — `mock_discovery.fetch_discovery()` (stand-in for
  `GET /compute/.../tasks?details=true`) is consulted **only for tasks NOT in the
  bundle**, proving live breadth: a task the platform advertises after stubs were
  generated still RUNS, just without static DX.

## Layout

```
poc-engine/
  schemas/                              # OFFLINE source of truth (stubs + runtime)
    geostatistics/kriging/schema.json
    geostatistics/declustering/schema.json
    geostatistics/normal_score/schema.json
  poc_compute_engine/
    __init__.py            # RUNTIME: PEP 562 __getattr__/__dir__ (dynamic topics)
    _engine.py             # RUNTIME: catalogue (bundled ∪ live) + proxies + override routing
    _schemas.py            # loads the offline schemas/ snapshot
    mock_discovery.py      # LIVE discovery stand-in (non-bundled tasks only)
    overrides/
      geostatistics/
        kriging.py         # OVERRIDE: hand-written typed run + KrigingResult (+ helpers)
    __init__.pyi           # STATIC (generated): declares topics
    geostatistics/
      __init__.pyi         # STATIC (generated): re-exports kriging, declustering, normal_score
      kriging.pyi          # STATIC (generated): RE-EXPORTS the override
      declustering.pyi     # STATIC (generated): schema-derived
      normal_score.pyi     # STATIC (generated): schema-derived
  generate_stubs.py        # generator: offline schemas -> .pyi, OVERRIDE-AWARE
  demo.py                  # runtime behaviour
  typed_usage_ok.py        # correct usage  -> 0 type errors
  typed_usage_bad.py       # wrong usage    -> 8 type errors
```

## The override seam

When the engine resolves `geostatistics.kriging`, it attempts a convention import of
`poc_compute_engine.overrides.geostatistics.kriging`. If that module exists it OWNS
the task: its hand-written `run` and `KrigingResult` are used instead of the
schema-synthesised ones. If it does not exist (declustering, normal_score), the
generic schema-driven path is used. Callers don't know or care which they got.

An override buys what the generic engine can't synthesise:

* **task-specific validation** (`max_samples > 0`; `simple` kriging requires `mean`),
* **richer typed results** with hand-curated helpers (`KrigingResult.summary()`,
  `.portal_url()`),
* explicit per-task control for debugging/docs.

It still REUSES the generic plumbing (`_mock_resolve`, `_fabricate_output`,
`_load_object`), so the override is a thin typed shell, not a parallel runtime.

The generator is **override-aware**: for kriging it emits a re-export stub
(`from poc_compute_engine.overrides.geostatistics.kriging import KrigingResult, run`);
for the others it derives the stub from the schema. So the static surface always
matches whatever the runtime will actually call.

## Inputs *and* outputs are schema-driven (generic path)

The schema describes the **result** as well as the parameters, using the same
semantic vocabulary (`output`, `reference_to`, `supported_schemas`, `attribute_path`).
So the generic engine hydrates the result with **no per-task output code**:

* `result.target.get_object()` — loads the typed object, dispatched on `schema_id`.
* `result.target.to_dataframe()` — generic, polymorphic on the object type.
* `result.target.attribute.reference` — resolved from `attribute_path[schema_id]`, so
  it **self-heals** if the platform changes that expression.

## Run it

```bash
cd poc-engine

# 1) (Re)generate the stubs from the OFFLINE schemas (override-aware, no network)
python generate_stubs.py

# 2) See the runtime: bundled ∪ live catalogue, kriging routed to the override
#    (summary/portal_url/validation), declustering & normal_score generic, and a
#    task advertised AFTER stub generation that still RUNS live.
python demo.py

# 3) See the static DX the stubs provide (this is what the IDE uses):
pyright typed_usage_ok.py     # -> 0 errors
pyright typed_usage_bad.py    # -> 8 errors, each caught before running
#   (mypy works too: mypy typed_usage_ok.py)
```

## What each part proves

| Claim | Where to see it |
|---|---|
| Namespace `topic.task.run(...)` with **no per-task code** for generic tasks | declustering / normal_score in `demo.py` step 3 |
| **Override** is auto-routed by convention import, transparent to callers | `demo.py` step 1 (`(override)` vs `(generic)`) + step 2 |
| Override adds validation + helpers the schema can't express | `demo.py` step 2/2b (`summary()`, `portal_url()`, `max_samples=0`) |
| Stubs are generated **offline** from `schemas/` and are **override-aware** | `generate_stubs.py`; `kriging.pyi` is a re-export |
| Outputs hydrate generically from the `results` schema | `demo.py` step 3 (declustering/normal_score) |
| Static autocomplete + type-checking (override + generic) | `typed_usage_ok.py` / `typed_usage_bad.py` + `pyright`/`mypy` |
| Execution is **live**; hints are **point-in-time** | `demo.py` step 5: `turning_bands` runs live, but is flagged statically (error #5) |

## Caveats (it's a POC)

* Reference resolution and object loading are **mocked** (`_engine._mock_resolve`,
  `_engine._load_object`).
* The server response is **fabricated** from the `results` schema
  (`_fabricate_output`) so the result has something to hydrate.
* Both discovery and the schema snapshot are local files/dicts, not HTTP clients.
* Stubs are regenerated by hand via `generate_stubs.py`; in the real system this runs
  in CI on schema (or override) change.
* The one piece that stays hand-authored on the generic path is the
  `schema_id -> Python loader class` registry — bounded by the object taxonomy and
  **shared across all tasks**, not per-task.

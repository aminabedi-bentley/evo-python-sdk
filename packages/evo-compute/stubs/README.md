# Static typing for the generic compute engine

`ComputeClient` builds its `client.<topic>.<task>.run(...)` namespace at runtime from the
live discovery catalogue. Nothing about that surface exists in `engine.py`, so a type
checker sees `Any` and an editor offers no completion.

This directory closes that gap. An **offline** generator turns a checked-in snapshot of the
task catalogue into [`src/evo/compute/engine.pyi`](../src/evo/compute/engine.pyi), which
type checkers read instead of `engine.py`. Every snapshotted task then gets completion,
signature help, hover documentation, parameter type-checking and a typed result.

```
stubs/
  snapshot/
    manifest.json                    # provenance: source endpoint, capture date, task versions
    <topic>/<task>.json              # the discovery payload for one task, verbatim
  checks/
    usage_ok.py                      # must type-check clean
    usage_bad.py                     # must be rejected, one way per mistake
```

## Using the generated types

Passing a dictionary literal needs no import — the checker knows the expected shape from
the call site, and the editor completes the keys:

```python
result = await client.geostatistics.declustering.run(
    source={"object": pointset_url},
    grid={"object": grid_url},
    target={"object": pointset_url, "attribute": {"operation": "create", "name": "weight"}},
    neighborhood={"ellipsoid": ..., "max_samples": 20},
)
print(result["message"])           # `result` is a TypedDict, so keys are checked
```

To name a shape in your own annotations, import it under `TYPE_CHECKING`. The generated
types exist only in the stub — they are not runtime objects:

```python
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from evo.compute.engine import DeclusteringSource
```

## Regenerating

```shell
make stubs-compute                        # rewrite engine.pyi from the snapshot
make check-stubs-compute                  # fail if engine.pyi is stale
python -m evo.compute._stubgen generate   # the same thing, without uv
```

Generation reads only the snapshot: no credentials, no network, no import of the tasks it
describes. `tests/test_stubgen.py::test_stub_is_up_to_date` regenerates and compares, so a
snapshot change that is not accompanied by a regenerated stub fails the normal test run.

## Refreshing the snapshot

Capture is the one online step and is run by hand, not by CI:

```shell
EVO_ACCESS_TOKEN=... EVO_HUB_URL=... EVO_ORG_ID=... \
    python -m evo.compute._stubgen capture
make stubs-compute
```

The snapshot is versioned as source: the task payloads and `manifest.json` are committed,
so the diff shows exactly which task changed and how, and the stub is reproducible from
any commit. That only holds if a refresh writes the files the same way it found them, so
the serialiser is pinned and a test asserts the committed files are already in capture
format. `manifest.json` records the endpoint the snapshot came from, the date it was
captured, and each task's version; a test asserts it still agrees with the files beside it.

## Decisions

**One artifact, `engine.pyi`, not `__init__.pyi`.** A stub for the package's `__init__`
would declare a *second* `ComputeClient` that has nothing to do with
`evo.compute.engine.ComputeClient`, so the two would disagree depending on how the class
was imported. Stubbing the module that defines the class keeps one type, and
`evo/compute/__init__.py` re-exports it as it already does.

**The stub does not declare `__getattr__`.** It could, and then every attribute would
type-check — including typos. Leaving it out is what makes an unknown task a static error.
The cost is that a task published after the snapshot is also an error, which is the
deliberate trade: total runtime breadth, point-in-time static breadth.
`ComputeClient.arun(topic, task, parameters)` is the typed escape hatch for anything the
snapshot does not know about, and is declared in the stub for exactly that reason.

**Results are `TypedDict`s, not classes.** The engine submits with `result_type=dict` and
returns what the platform sends, so the runtime value really is a dictionary. A `TypedDict`
types the keys without pretending attribute access will work.

**Reference parameters are typed as they are transmitted.** A property annotated
`reference_to: geoscience-object` is a URL string on the wire, so the stub says `str`.
Deep validation holds those leaves to the same declared type, so the static promise and
the runtime check agree; a leaf declared as the resolved object it becomes is relaxed on
both sides, because the caller passes a bare reference instead. When the typed
object/dataframe I/O layer lands, those leaves become the richer input union;
`_TaskRenderer._annotation` and `validation._relaxed_reference` are the two places that
have to change together.

**Names are task-scoped and de-duplicated.** Every generated type is prefixed with its
task, so two tasks never fight over `Source`; result-side shapes take a further `Result`
prefix so an input and an output that share a schema title stay distinct. Within a task,
structurally identical objects collapse onto one type — the published schemas inline the
same filter shape at four different depths.

## The runtime half

The stub is one half of a contract the engine enforces at run time from the same schema:

| Mistake | Caught statically by | Caught at run time by |
|---|---|---|
| unknown topic or task | the stub (no `__getattr__`) | discovery lookup in `arun` |
| unknown or missing parameter | the stub's `run(...)` signature | signature binding, then `validate_parameters` |
| wrong scalar type, bad enum member, missing nested field | the stub's `TypedDict`s and `Literal`s | `validate_parameters(..., deep=True)` |

Deep validation is opt-in — `ComputeClient(context, deep_validation=True)`, or
`arun(..., deep_validation=True)` for a single call — because it costs a full JSON Schema
pass. `tests/test_stubgen.py::TestDeepValidationAgreement` asserts the two halves agree:
the payload from `usage_ok.py` passes deep validation, and the mistakes `usage_bad.py`
makes are rejected at run time too.

## Verified with

| Checker | Result |
|---|---|
| pyright | `usage_ok.py` clean; `usage_bad.py` reports all seven mistakes |
| mypy | `usage_ok.py` clean; `usage_bad.py` reports all seven mistakes |
| PyCharm / VS Code | completion, signature help and hover text on `client.<topic>.<task>.run(...)` |

`tests/test_stubgen.py` runs both. `mypy` is an evo-compute test dependency, so that check
runs in CI; `pyright` needs a node runtime, so it is only checked when it is on the `PATH`.

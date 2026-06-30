# POC: authenticated, instance-bound generic engine (live discovery)

A proof of concept answering technical feasibility questions for a **fully generic compute engine**:

> 1- If we want a **fully generic engine that reads schemas live from discovery** (no
> SDK release per new task), **how do we authenticate** the discovery + execution
> calls?

> 2- How do pre-generated stubs give **static DX** for a live discovery engine?
> *(walked through in [`demo.ipynb`](./demo.ipynb) §3 "the live, schema-driven namespace"
> and §7 "live breadth vs. the generated stubs")*

> 3- How do we handle specialized per-task overrides (typed runners) without breaking the generic engine?
> *(walked through in [`demo.ipynb`](./demo.ipynb) §5 "a specialized typed runner — the override seam")*

**Answers demonstrated here:** authenticate once when the client is opened, bind
the dynamic task namespace to that authenticated **client instance** (not the
module), and **fail fast** — discovery runs eagerly on open (`async with` /
`connect`), so bad credentials are rejected before any namespace is handed out.

```python
from evo.notebooks import ServiceManagerWidget
from poc_compute_engine import ComputeClient

manager = await ServiceManagerWidget.with_auth_code(client_id=...).login()  # real OAuth
async with ComputeClient(manager) as client:       # <- discovery + auth happen HERE (fail-fast)
    await client.geostatistics.kriging_gcp.run(     # <- submitted via the real evo.compute.JobClient
        source="grade", target="kriged_grade", variogram="vario-123",
    )
```

`ComputeClient` is **async-native**: it's an async context manager (or use `client = await
ComputeClient.connect(manager)` and `await client.aclose()`). Discovery runs eagerly on
open, so a bad token fails before any namespace is handed out. In a notebook use top-level
`await`; in a script drive it from `asyncio.run(...)`.

`ComputeClient` accepts any real `evo.common.IContext`. `ServiceManagerWidget` (from
`evo.notebooks`) is one, so the whole stack runs against the **real Evo compute platform**
— no local server, no mocks. See [`demo.ipynb`](./demo.ipynb) for the full runnable walk-through.

## Why instance-bound fixes the auth problem

A naive **module-level** approach would resolve `poc_compute_engine.geostatistics.kriging`
via a PEP 562 `__getattr__`. That hook receives only the attribute *name* — no `self`,
nowhere to thread a token or org — so it can neither authenticate nor scope to a tenant,
and a process-global discovery cache can't serve two users at once.

Hanging the namespace off a `ComputeClient` **instance** gives every hop a `self`
carrying the `APIConnector` (bearer token + refresh) and the org id. Discovery,
resolution and execution all flow through that one connector.

| Concern | POC answer  |
|---|---|
| Where do creds live? | `client._connector` (from `IContext`) |
| When is discovery fetched? | on open (`async with`/`connect`), authenticated, **fail-fast** |
| Token refresh on expiry | via the connector's authorizer |
| Two tenants at once | two clients, two orgs/tokens |
| Bare-id resolution lookups | connector on hand → authenticated lookup |

## Layout

```
poc-engine/
  poc_compute_engine/                  # THE ENGINE PACKAGE
    __init__.py        # re-exports ComputeClient + DiscoveryClient
    engine.py          # ComputeClient (fail-fast open) + instance-bound proxies + result hydration
    discovery.py       # DiscoveryClient: lists the live catalogue (the engine delegates discovery to it)
    overrides/         # OPTIONAL per-task specialized runners (convention-imported)
      geostatistics/kriging_gcp.py  # worked example: hand-written typed runner for one task
    schemas/<topic>/<task>/schema.json   # OFFLINE discovery snapshot, shipped as package data (stub source)
    __init__.pyi       # STATIC (generated offline): types the ComputeClient tree
  generate_stubs.py    # offline generator: reads poc_compute_engine/schemas/ -> emits __init__.pyi (no auth/network)
  demo.ipynb           # runnable demo against REAL Evo (OAuth, live discovery)
  typed_usage_ok.py    # correct usage -> 0 type errors
  typed_usage_bad.py   # wrong usage   -> 8 type errors
```

Both halves of the engine's platform I/O are delegated to dedicated, connector-backed
clients **inside** the package: EXECUTION to the real `evo.compute.JobClient`, and
DISCOVERY to `DiscoveryClient` (`discovery.py`). The stub generator, demo notebook, and
type-check fixtures are POC scaffolding and live in the POC folder.

This is the **fully-generic** variant: **by default there is no per-task Python code**.
Every topic/task and `run(...)` signature is synthesised from the live discovery schema;
**execution is delegated to the real `evo.compute.JobClient`** (`submit` → poll →
results), and results hydrate from the schema's `results` block
(`get_object`/`to_dataframe`, self-healing `attribute_path`).

On top of that, a task can opt into a **specialized typed runner** — see
[the override seam](#specialized-typed-runners-the-override-seam) below — without any
client-code change. The two ideas compose: most tasks ride the generic engine; a few
high-value ones get a hand-curated surface.

> **Discovery is the gap the engine fills.** `evo.compute` ships a `JobClient` for
> *executing* a task, but its `TasksApi` only has `execute_task` — there is **no
> discovery/`list_tasks` client** in the SDK. So the engine delegates discovery to its own
> `DiscoveryClient` (the symmetric twin of `JobClient`) and execution to `JobClient` — no
> inline HTTP. A real `DiscoveryApi` is the natural thing to upstream into evo-compute;
> `DiscoveryClient` is a minimal sketch of it, usable standalone via
> `DiscoveryClient.from_context(ctx)`.

## Auth model — the REAL SDK against the REAL platform (no mocks)

There are **no test doubles**. `ComputeClient` takes any real `evo.common.IContext`:

* In a notebook, `evo.notebooks.ServiceManagerWidget` performs the real OAuth login and
  org/hub/workspace selection, and *is* an `IContext` (`get_connector()` + `get_org_id()`).
* `evo.common.APIConnector` (carried by the context) is the real request/serialise path;
  its `evo.aio.AioTransport` (aiohttp) makes genuine HTTPS calls to the Evo compute service.
* Discovery is a real `GET /compute/orgs/{org_id}/tasks` — verified against the live
  OpenAPI: it returns `DiscoveryAPIResponse { ..., results: [TaskResource, ...] }`, and
  each `TaskResource` carries exactly `topic`/`name`/`version`/`feature_flag`/`parameters`/
  `results`, which is what the engine consumes.
* Execution is the real `evo.compute.JobClient.submit(...)` → 303 + `Location` → status
  polling → results. A revoked/expired/unentitled token raises a typed SDK exception
  (`UnauthorizedException`/`ForbiddenException`) — and because discovery runs on open
  (`__aenter__`/`connect`), that happens **before any namespace exists**.

The engine is **async-native**: `run(...)` is a coroutine and `ComputeClient` is an async
context manager. The SDK transport + the authorizer's `asyncio.Lock` are bound to the loop
they're opened on; because the engine simply runs *on the caller's own loop* (the notebook
kernel's, or the one `asyncio.run` creates) there is no second loop and therefore no
cross-loop hazard — no background thread, no `nest_asyncio` reentrancy hack, and none of
the opaque "Reached maximum number of retries" failures a sync facade is prone to. Open
with `async with ComputeClient(ctx) as client:` (auto-closes) or `client = await
ComputeClient.connect(ctx)` (then `await client.aclose()`); both run discovery eagerly.

> **Why async-native?** An earlier sync facade drove the loop-bound SDK objects via
> `run_until_complete`, which required `nest_asyncio.apply()` inside Jupyter — a global
> monkeypatch of asyncio that reviewers rightly flag. Going async-native deletes that line
> entirely and matches the async SDK underneath, at the cost of one `await` per call.

## Specialized typed runners (the override seam)

The generic engine is great for breadth, but some tasks deserve a hand-curated surface:
validation the JSON Schema can't express, a richer result type, extra helpers, or
parameters beyond the schema. Drop a module at
`poc_compute_engine/overrides/<topic>/<task>.py` exposing a `bind(client, spec)` factory,
and the engine **auto-discovers it by convention import** on first access to
`client.<topic>.<task>` — its runner replaces the generic proxy **transparently** (same
`client...run(...)` DX, no client-code change). Tasks without an override stay generic.

The worked example is [`overrides/geostatistics/kriging_gcp.py`](./poc_compute_engine/overrides/geostatistics/kriging_gcp.py):

```python
client.geostatistics.kriging_gcp        # -> KrigingGcpRunner (specialized override)
client.geostatistics.declustering   # -> generic _TaskProxy (no override)

res = await client.geostatistics.kriging_gcp.run(
    source="grade", target="kriged_grade", variogram="vario-123",
    kriging_type="simple", mean=2.5,   # `mean` exists ONLY on the override
)
res.summary()      # override-only helper
res.portal_url()   # override-only helper
```

> **Hyphenated live names.** The platform advertises tasks with hyphens (e.g.
> `kriging-gcp`), which aren't valid Python identifiers. The engine normalises `-`↔`_`,
> so the task is reachable as `client.geostatistics.kriging_gcp` (and override modules
> live at `overrides/<topic>/kriging_gcp.py`). The raw `getattr(ns, "kriging-gcp")`
> form still resolves too.

It is **not** a parallel runtime: execution still flows through the same authenticated
`client._submit` → real `evo.compute.JobClient`, and it reuses the engine's shared
helpers. It adds only the typed shell and the bespoke behaviour. The override also owns
its **static** surface: `generate_stubs.py` detects the module and re-exports its
hand-written `KrigingGcpRunner`/`KrigingGcpResult` into the `.pyi` instead of schema-derived
stubs, so autocomplete and type-checks match the specialized runtime exactly.

## Static DX = a point-in-time snapshot

`generate_stubs.py` reads an **offline** discovery snapshot checked into
`poc_compute_engine/schemas/` and
emits a single `__init__.pyi` typing the `ComputeClient` tree
(`client.geostatistics.kriging_gcp.run(...) -> KrigingGcpResult`). It needs **no auth and no
network**, so the build artifact is reproducible in CI without credentials. The `schema.json`
files are copied verbatim from the platform's discovery (they mirror its `TaskResource`
shape).

Because the runtime is always live, a task advertised *after* the snapshot still runs —
it's just not statically known until stubs are regenerated. That is the inherent
generic-vs-codegen trade: **total runtime breadth, point-in-time static breadth**. The
notebook makes this concrete by diffing the live discovery catalogue against the stubbed
set.

## Run it

The headline demo is the notebook, run against **real Evo**:

```bash
cd poc-engine
pip install -e ../../evo-sdk-common -e ..   # evo.common / evo.oauth / evo.compute (+ aiohttp, evo.notebooks)
jupyter lab demo.ipynb                       # real OAuth, live discovery, live-vs-stub breadth
```

Offline checks (no credentials needed):

```bash
# 1) Regenerate the typed stub from the offline schema snapshot
python generate_stubs.py                      # -> wrote __init__.pyi (3 tasks)

# 2) Static DX from the generated stub
pyright typed_usage_ok.py     # -> 0 errors
pyright typed_usage_bad.py    # -> 8 errors (incl. a live-but-not-stubbed task + an override-typed param)
#   (mypy agrees: mypy typed_usage_ok.py)
```

## Caveats (it's a POC)

* The client/auth/transport/serialisation **and execution** are **all real SDK against
  the real Evo platform** — no test doubles, no local server. Discovery and `JobClient`
  execution hit live endpoints with your real OAuth token.
* Reference resolution and object loading are **stand-ins** (`_mock_resolve`,
  `_load_object`): the engine wires submission to the real `JobClient`, but actually
  *running* a task needs real object/attribute references (see the SDK's full kriging
  code-sample). The *engine* — generic dispatch + schema-driven typing — is the POC's
  contribution, not the platform's business logic.
* `poc_compute_engine/discovery.py` is a minimal sketch of a `DiscoveryApi` the SDK
  doesn't yet ship; the engine delegates discovery to it (symmetric with `JobClient`),
  and it's also usable standalone via `DiscoveryClient.from_context(ctx)`. It shares the
  engine's connector (ref-counted `open`/`close`), so it's safe to call alongside a live
  engine.
* Stubs are regenerated by hand here; in the real system this runs offline in CI.

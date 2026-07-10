"""The generic runtime engine — instance-bound and authenticated.

The POC thesis: a **fully generic** engine that reads the task catalogue **live from
discovery**, so a new platform task needs *no SDK release*. The price of "live" is
that every call needs credentials — which is solved by binding the dynamic namespace
to an authenticated client **instance** instead of the module:

    ctx = ServiceManagerWidget(...)  # any real evo IContext (OAuth + workspace)
    async with ComputeClient(ctx) as client:        # <- FAILS FAST on __aenter__ if auth is bad
        await client.geostatistics.kriging_gcp.run(...)   # <- authenticated through ctx (real HTTP)

Why instance-bound (not module ``__getattr__``): a module hook gets only the
attribute name — no ``self``, nowhere to thread a token or org. An instance hook has
``self``, so it can authenticate, call discovery, scope to the org, and let two
clients with different tenants/tokens coexist.

There is **no per-task Python code by default** — topics/tasks and ``run(...)``
signatures are synthesised from the live discovery schema, and results hydrate from the
schema's ``results`` block. A task that needs more than the schema can express can opt
into a hand-written *specialized runner* (see ``overrides/``), auto-discovered by
convention and routed to transparently; everything else stays generic.

The engine is **async-native**: the real SDK connector + transport are async (real
aiohttp), so ``run(...)`` is a coroutine and the client is an async context manager. This
avoids any event-loop juggling — no background-thread loop, no ``nest_asyncio`` reentrancy
hack — and structurally rules out the cross-loop errors a sync facade is prone to. In a
notebook, use top-level ``await``; in a script, drive it from ``asyncio.run(...)``.
"""

from __future__ import annotations

import importlib
import inspect
from typing import Any, Literal, Optional, Union
from uuid import UUID

from evo.common import APIConnector, IContext
from evo.compute import JobClient  # type: ignore[import-untyped]  # evo-compute ships no py.typed marker (real SDK packaging gap)

from .discovery import DiscoveryClient
from .resolver import (
    AttributeInput,
    FileInput,
    LoadedObject,
    ObjectHandle,
    ObjectInput,
    ObjectLoader,
    ReferenceResolver,
    TargetAttrInput,
)

# The engine delegates both halves of its platform I/O to dedicated, connector-backed
# clients — EXECUTION to the real ``evo.compute.JobClient`` (submit -> poll -> results),
# and DISCOVERY to ``DiscoveryClient`` (``GET .../tasks``; the capability the SDK still
# lacks). There is no inline HTTP here.
#
# Everything that touches the network is a coroutine awaited on the caller's own event
# loop (the notebook kernel's loop, or the one ``asyncio.run`` creates). Because the
# context's SDK objects (aiohttp transport + the authorizer's ``asyncio.Lock``) are bound
# to the loop they were opened on, and we now run *on that same loop*, there is no
# cross-loop hazard to work around. ``call_api`` raises typed SDK exceptions
# (UnauthorizedException/ForbiddenException/...) on error statuses, which is exactly the
# fail-fast behaviour we propagate from ``__aenter__``.


_JSON_TO_PY: dict[str, type] = {
    "string": str,
    "integer": int,
    "number": float,
    "boolean": bool,
    "object": dict,
    "array": list,
}


class ComputeClient:
    """Authenticated entry point. Discovery runs on ``__aenter__`` (fail-fast).

    Async-native: use it as an async context manager (``async with ComputeClient(ctx)
    as client:``) or via the ``connect`` factory (``client = await
    ComputeClient.connect(ctx)``). Either way the authenticated discovery call happens
    eagerly so a bad token fails before any namespace is handed out.
    """

    def __init__(self, context: IContext, *, object_loader: ObjectLoader | None = None) -> None:
        self._context = context
        self._connector: APIConnector = context.get_connector()
        self._org_uuid: UUID = context.get_org_id()
        self._org_id: str = str(self._org_uuid)
        self._opened = False
        self._catalogue: list[dict] = []
        # DISCOVERY is delegated to a dedicated connector-backed client (the symmetric
        # twin of ``JobClient`` for EXECUTION); the engine holds no inline HTTP.
        self._discovery = DiscoveryClient(self._connector, self._org_uuid)
        # REFERENCE RESOLUTION turns friendly Python values into the wire payload by
        # reading each task's schema annotations. It needs to load objects (to read a
        # ``schema_id`` before choosing an attribute's JMESPath), so it takes an
        # injectable loader: the real SDK-backed loader by default; callers (notebook,
        # tests) can supply their own — e.g. an offline fake — to stay credential-free.
        loader = object_loader if object_loader is not None else _SdkObjectLoader(context)
        self._resolver = ReferenceResolver(loader)

    @classmethod
    async def connect(cls, context: IContext, *, object_loader: ObjectLoader | None = None) -> "ComputeClient":
        """Construct and open in one step (for callers not using ``async with``).

        The caller owns the lifetime and should ``await client.aclose()`` when done.
        """
        client = cls(context, object_loader=object_loader)
        await client._open()
        return client

    async def _open(self) -> None:
        if self._opened:
            return
        await self._connector.open()
        # FAIL FAST: the very first thing we do is an authenticated discovery call.
        # A missing/invalid/unentitled token makes ``call_api`` raise a typed SDK
        # exception (UnauthorizedException/ForbiddenException) right here, before any
        # namespace is ever handed out.
        try:
            self._catalogue = await self._discovery.list_tasks()
        except BaseException:
            await self._connector.close()
            raise
        self._opened = True

    async def aclose(self) -> None:
        """Close the real transport."""
        if not self._opened:
            return
        self._opened = False
        await self._connector.close()

    async def __aenter__(self) -> "ComputeClient":
        await self._open()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    # -- catalogue helpers ------------------------------------------------- #
    def _topics(self) -> list[str]:
        return sorted({t["topic"] for t in self._catalogue})

    def _tasks(self, topic: str) -> list[str]:
        return sorted(t["name"] for t in self._catalogue if t["topic"] == topic)

    def _spec(self, topic: str, task: str) -> dict | None:
        want = _norm(task)
        for t in self._catalogue:
            if t["topic"] == topic and (t["name"] == task or _norm(t["name"]) == want):
                return t
        return None

    async def refresh(self) -> None:
        """Re-fetch the live catalogue (e.g. to pick up newly advertised tasks)."""
        self._catalogue = await self._discovery.list_tasks()

    # -- authenticated execution via the REAL SDK JobClient ----------------- #
    async def _submit(self, topic: str, task: str, params: dict, preview: bool) -> dict:
        """Submit + poll + fetch results through ``evo.compute.JobClient``."""
        job: JobClient[dict] = await JobClient.submit(
            connector=self._connector,
            org_id=self._org_uuid,
            topic=topic,
            task=task,
            parameters=params,
            result_type=dict,
            preview=preview,
        )
        return await job.wait_for_results()

    # -- dynamic namespace ------------------------------------------------- #
    def __getattr__(self, name: str) -> "_TopicProxy":
        # __getattr__ only fires for names not found normally (never real attrs).
        if name.startswith("_"):
            raise AttributeError(name)
        if not object.__getattribute__(self, "_opened"):
            raise RuntimeError(
                "ComputeClient is not open yet. Use `async with ComputeClient(ctx) as "
                "client:` or `client = await ComputeClient.connect(ctx)` before accessing "
                "topics (discovery runs on open)."
            )
        topics = object.__getattribute__(self, "_topics")()
        if name in topics:
            return _TopicProxy(self, name)
        raise AttributeError(f"no compute topic {name!r}. Available: {', '.join(topics)}")

    def __dir__(self):
        return sorted(set(super().__dir__()) | set(self._topics()))

    def __repr__(self) -> str:
        return f"<ComputeClient org={self._org_id!r} topics={self._topics()}>"


def _py_annotation(prop: dict) -> Any:
    if "enum" in prop:
        return Literal[tuple(prop["enum"])]  # type: ignore[misc]
    # Reference-bearing properties advertise the *friendly* input union they accept
    # (loaded objects, ObjectHandles, URLs, ...) rather than the raw JSON type.
    if prop.get("target") == "attribute":
        return TargetAttrInput
    ref = prop.get("reference_to")
    if ref == "geoscience-object":
        return ObjectInput
    if ref == "file":
        return FileInput
    if ref == "attribute":
        return AttributeInput
    jtype = prop.get("type", "")
    if isinstance(jtype, list):
        # JSON Schema allows a type union (e.g. ["string", "null"]). Map each member
        # and Optional-ify when "null" is present; fall back to Any if nothing maps.
        members = [_JSON_TO_PY[t] for t in jtype if t != "null" and t in _JSON_TO_PY]
        base = members[0] if len(members) == 1 else (Union[tuple(members)] if members else Any)
        return Optional[base] if "null" in jtype else base
    return _JSON_TO_PY.get(jtype, Any)


def _signature_from_schema(spec: dict) -> inspect.Signature:
    schema = spec["parameters"]
    props: dict = schema.get("properties", {})
    required = list(schema.get("required", []))
    optional = [name for name in props if name not in required]

    params: list[inspect.Parameter] = []
    for name in required:
        params.append(
            inspect.Parameter(
                name, inspect.Parameter.KEYWORD_ONLY, annotation=_py_annotation(props[name])
            )
        )
    for name in optional:
        params.append(
            inspect.Parameter(
                name,
                inspect.Parameter.KEYWORD_ONLY,
                default=props[name].get("default", None),
                annotation=_py_annotation(props[name]),
            )
        )
    params.append(
        inspect.Parameter(
            "preview",
            inspect.Parameter.KEYWORD_ONLY,
            default=bool(spec.get("feature_flag")),
            annotation=bool,
        )
    )
    return inspect.Signature(params, return_annotation="TaskResult")


def _mock_resolve(spec: dict, kwargs: dict) -> dict:
    """Legacy flat stand-in, retained only for the hand-written override illustration.

    The generic engine now resolves references for real via
    :class:`poc_compute_engine.resolver.ReferenceResolver` (see ``ComputeClient`` /
    ``_make_run``). This helper survives solely so the ``overrides/`` example — which uses
    a deliberately simplified flat input surface — keeps working end to end.
    """
    props: dict = spec["parameters"].get("properties", {})
    resolved: dict = {}
    for key, value in kwargs.items():
        ref = props.get(key, {}).get("reference_to")
        if ref == "attribute":
            resolved[key] = f"attributes[?name=='{value}']"
        elif ref == "geoscience-object":
            resolved[key] = f"https://mock-hub/objects/{value}"
        elif ref == "file":
            resolved[key] = f"https://mock-hub/file/v2/.../files/{value}"
        else:
            resolved[key] = value
    return resolved


class _SdkObjectLoader:
    """Default :class:`~poc_compute_engine.resolver.ObjectLoader` backed by the real SDK.

    Resolves a user-supplied object handle to its validated URL + ``schema_id`` by
    downloading the object's metadata through ``evo.objects``. Handles that already carry
    a ``schema_id`` (an :class:`ObjectHandle` or a typed/loaded object exposing
    ``.metadata``) short-circuit the network call. Imports are lazy so the engine stays
    importable in offline/type-check contexts where credentials aren't configured.
    """

    def __init__(self, context: IContext) -> None:
        self._context = context

    async def load(self, handle: Any) -> LoadedObject:
        # 1. Explicit handle with a known schema id -> no network needed.
        if isinstance(handle, ObjectHandle) and handle.schema_id is not None:
            return LoadedObject(reference=handle.reference, schema_id=handle.schema_id)
        # 2. A typed/loaded object already exposes its metadata.
        meta = getattr(handle, "metadata", None)
        if meta is not None and getattr(meta, "schema_id", None) is not None:
            return LoadedObject(reference=str(meta.url), schema_id=str(meta.schema_id))
        # 3. Fall back to a real metadata download by reference/URL.
        from evo.objects import DownloadedObject  # lazy: avoid import-time SDK coupling

        reference = handle.reference if isinstance(handle, ObjectHandle) else handle
        obj = await DownloadedObject.from_context(self._context, reference)
        return LoadedObject(reference=str(obj.metadata.url), schema_id=str(obj.metadata.schema_id))


# --------------------------------------------------------------------------- #
# Output side: hydrate the result GENERICALLY from the discovery `results` block.
# (Unchanged from v1 — schema-driven, nothing task-specific.)
# --------------------------------------------------------------------------- #


class Table:
    """A stand-in for the pandas DataFrame the real SDK returns."""

    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows

    def __repr__(self) -> str:
        cols = list(self.rows[0].keys()) if self.rows else []
        return f"Table(rows={len(self.rows)}, columns={cols})"


class GeoscienceObject:
    """A stand-in for a loaded, typed geoscience object (Regular3DGrid, BlockModel, ...)."""

    def __init__(self, schema_id: str, reference: str, name: str | None) -> None:
        self.schema_id = schema_id
        self.reference = reference
        self.name = name

    def to_dataframe(self) -> Table:
        kind = (self.schema_id or "").split("/")[0]
        rows = {
            "pointset": [{"x": 0.0, "y": 0.0, "z": 0.0, "value": 1.0}, {"x": 1.0, "y": 0.0, "z": 0.0, "value": 2.0}],
            "regular-3d-grid": [{"i": 0, "j": 0, "k": 0, "value": 1.0}, {"i": 1, "j": 0, "k": 0, "value": 2.0}],
        }.get(kind, [{"value": 1.0}])
        return Table(rows)

    def __repr__(self) -> str:
        return f"GeoscienceObject(schema_id={self.schema_id!r}, name={self.name!r})"


def _load_object(schema_id: str | None, reference: str | None, name: str | None) -> GeoscienceObject:
    return GeoscienceObject(schema_id or "unknown", reference or "", name)


class File:
    """A stand-in for a referenced File resource (a ``reference_to: file`` output)."""

    def __init__(self, reference: str, name: str | None = None) -> None:
        self.reference = reference
        self.name = name

    def __repr__(self) -> str:
        return f"File(reference={self.reference!r})"


def _load_file(reference: str | None, name: str | None) -> File:
    return File(reference or "", name)


def _types_of(spec: dict) -> list:
    """The declared JSON-Schema ``type`` as a list (handles ``["object", "null"]``)."""
    t = spec.get("type")
    return t if isinstance(t, list) else [t]


def _is_object_node(spec: dict) -> bool:
    return "object" in _types_of(spec) or "properties" in spec


def _is_array_node(spec: dict) -> bool:
    return "array" in _types_of(spec)


class _ResultNode:
    """Generic, schema-shaped view over one object in the result payload.

    Every RFC 168 *output* property type is hydrated generically from the ``results``
    block: ``output: geoscience-object`` (``get_object``/``to_dataframe``),
    ``output: file`` (``get_file``), and ``output: attribute`` (``get_attribute``).
    Nested groups recurse, arrays of output objects become lists of ``_ResultNode``,
    and nullable outputs (``type: ["object", "null"]``) that came back ``null`` are
    surfaced as ``None`` — nothing here is task-specific.
    """

    def __init__(self, schema_node: dict, payload: dict, schema_id: str | None = None) -> None:
        props: dict = schema_node.get("properties", {})
        self._schema_id = payload.get("schema_id", schema_id) if isinstance(payload, dict) else schema_id
        # Which RFC 168 output kind this node is (geoscience-object | file | attribute | None).
        self._output = schema_node.get("output")
        self._is_object = self._output == "geoscience-object"
        self._is_file = self._output == "file"
        self._is_attribute = self._output == "attribute"
        for fname, fspec in props.items():
            val = payload.get(fname) if isinstance(payload, dict) else None
            setattr(self, fname, self._hydrate(fspec, val))

    def _hydrate(self, fspec: Any, val: Any) -> Any:
        if not isinstance(fspec, dict):
            return val
        # Array of outputs -> list of hydrated items (each against the item schema).
        if _is_array_node(fspec) and isinstance(val, list):
            item_schema = fspec.get("items", {})
            return [self._hydrate(item_schema, item) for item in val]
        # Group / output object -> recurse (nullable output that returned null stays None).
        if _is_object_node(fspec):
            if val is None:
                return None
            return _ResultNode(fspec, val, self._schema_id)
        # Scalar (string / number / attribute-URL / file-URL literal) -> passthrough.
        return val

    def get_object(self) -> GeoscienceObject:
        if not self._is_object:
            raise AttributeError("this result field is not a geoscience object")
        return _load_object(self._schema_id, getattr(self, "reference", None), getattr(self, "name", None))

    def get_file(self) -> File:
        if not self._is_file:
            raise AttributeError("this result field is not a file")
        return _load_file(getattr(self, "reference", None), getattr(self, "name", None))

    def get_attribute(self) -> str:
        if not self._is_attribute:
            raise AttributeError("this result field is not an attribute")
        return getattr(self, "reference", "")

    def to_dataframe(self) -> Table:
        return self.get_object().to_dataframe()

    def __getattr__(self, name: str) -> Any:
        # Result fields are synthesised from the schema at runtime (set via ``setattr``
        # in ``__init__``). This hook never fires for those real attributes; it exists so
        # static checkers treat schema-shaped access (``result.target``) as ``Any`` rather
        # than an unknown-attribute error, and so genuinely-missing names raise cleanly.
        raise AttributeError(name)

    def __repr__(self) -> str:
        fields = [k for k in self.__dict__ if not k.startswith("_")]
        tag = f" {self._output}" if self._output else ""
        return f"<{', '.join(fields)}{tag}>"


class TaskResult(_ResultNode):
    """The object `run(...)` returns. Built entirely from the `results` schema."""

    def __init__(self, results_schema: dict | None, payload: dict) -> None:
        # An empty schema drives the same generic init (no fields, all output flags
        # False); the no-`results` case just adds the platform's status ``message``.
        super().__init__(results_schema or {}, payload)
        if results_schema is None:
            self.message = payload.get("message", "")

    def __repr__(self) -> str:
        return f"TaskResult(message={getattr(self, 'message', None)!r})"


def _make_run(client: ComputeClient, spec: dict):
    sig = _signature_from_schema(spec)
    topic, task = spec["topic"], spec["name"]

    async def run(**kwargs: Any) -> TaskResult:
        bound = sig.bind_partial(**kwargs)
        bound.apply_defaults()
        provided = dict(bound.arguments)
        preview = bool(provided.pop("preview", False))

        required = set(spec["parameters"].get("required", []))
        missing = required - {k for k, v in provided.items() if v is not None}
        if missing:
            raise TypeError(f"{task}.run() missing required parameter(s): {sorted(missing)}")

        resolved = await client._resolver.resolve(spec, provided)
        # AUTHENTICATED execution via the real evo.compute.JobClient: it attaches the
        # bearer token, submits the job, polls status, and returns the PLATFORM's
        # results. A revoked/expired/unentitled token raises a typed SDK error here.
        results = await client._submit(topic, task, resolved, preview=preview)

        if spec.get("results"):
            return TaskResult(spec["results"], results or {})
        return TaskResult(None, {"message": f"{task} submitted (no result schema advertised)."})

    run.__name__ = "run"
    run.__qualname__ = f"{task}.run"
    run.__signature__ = sig  # type: ignore[attr-defined]
    run.__doc__ = (spec.get("description") or "") + "\n\nParameters are resolved from the live schema."
    return run


class _TaskProxy:
    def __init__(self, client: ComputeClient, spec: dict) -> None:
        self._spec = spec
        self.run = _make_run(client, spec)

    def __dir__(self):
        return ["run"]

    def __repr__(self) -> str:
        return f"<task {self._spec['topic']}/{self._spec['name']} v{self._spec.get('version')}>"


def _norm(name: str) -> str:
    """Normalise a task identifier for attribute access.

    The live platform advertises hyphenated task names (e.g. ``kriging-gcp``),
    which are not valid Python identifiers. We map ``-`` to ``_`` so a task is
    reachable as ``client.<topic>.kriging_gcp`` (and so override modules, which
    must be importable, live at ``overrides/<topic>/kriging_gcp.py``). Lookups
    compare on the normalised form, so the raw hyphenated name still resolves too.
    """
    return name.replace("-", "_")


def _load_override(topic: str, task: str) -> Any | None:
    """Convention import of a per-task override module, if one exists.

    Returns the imported ``poc_compute_engine.overrides.<topic>.<task>`` module, or
    ``None`` when no such module exists. A ``ModuleNotFoundError`` for a *different*
    module (i.e. a genuinely broken override that fails to import its own deps) is
    re-raised, so override bugs surface loudly instead of silently falling back.
    """
    mod_name = f"{__package__}.overrides.{topic}.{_norm(task)}"
    try:
        return importlib.import_module(mod_name)
    except ModuleNotFoundError as exc:
        missing = exc.name or ""
        if mod_name == missing or mod_name.startswith(missing + "."):
            return None  # the override (or its package) simply isn't present
        raise


class _TopicProxy:
    def __init__(self, client: ComputeClient, topic: str) -> None:
        self._client = client
        self._topic = topic

    def __getattr__(self, name: str) -> Any:
        spec = self._client._spec(self._topic, name)
        if spec is None:
            available = ", ".join(self._client._tasks(self._topic))
            raise AttributeError(f"no task {name!r} in topic {self._topic!r}. Available: {available}")
        # A per-task override, if present, OWNS the task: its hand-written, fully-typed
        # runner replaces the generic proxy transparently (same ``.run(...)`` DX). Most
        # tasks have no override and ride the generic engine.
        override = _load_override(self._topic, name)
        if override is not None and hasattr(override, "bind"):
            return override.bind(self._client, spec)
        return _TaskProxy(self._client, spec)

    def __dir__(self):
        return [_norm(t) for t in self._client._tasks(self._topic)]

    def __repr__(self) -> str:
        tasks = ", ".join(self._client._tasks(self._topic))
        return f"<topic {self._topic!r} tasks=[{tasks}]>"

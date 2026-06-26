"""The generic runtime engine.

There is **no** per-task Python class here. The ``poc_compute_engine.geostatistics.kriging``
namespace and the ``run(...)`` callables are synthesised on demand from the (mock)
discovery schema:

* ``_TopicProxy.__dir__``  -> tab-completion of task names for a topic
* ``_TaskProxy.__dir__``   -> exposes ``run``
* ``run.__signature__``    -> per-task signature built from the schema (so
  ``inspect.signature`` and Jupyter ``run?`` popups work at runtime)

Parameter *resolution* is intentionally mocked (see ``_mock_resolve``) — the POC is
about demonstrating the DX, not real reference resolution.
"""

from __future__ import annotations

import importlib
import inspect
from types import ModuleType
from typing import Any, Literal

from . import _schemas
from .mock_discovery import fetch_discovery

_JSON_TO_PY: dict[str, type] = {
    "string": str,
    "integer": int,
    "number": float,
    "boolean": bool,
    "object": dict,
    "array": list,
}


class DiscoveryClient:
    """Reader over the task catalogue.

    The catalogue is the UNION of two sources, in this precedence order:

    * **bundled** offline ``schema.json`` files (``_schemas``) — the tasks that
      also have generated stubs, and
    * **live** discovery (``mock_discovery.fetch_discovery``) — tasks the platform
      advertises that are NOT in the bundle (no stubs).

    Bundled specs win on name collisions so a pinned, stub-backed contract is never
    silently shadowed by a live one.
    """

    def _catalogue(self) -> list[dict]:
        seen: set[tuple[str, str]] = set()
        catalogue: list[dict] = []
        for spec in _schemas.load_bundled_specs():
            key = (spec["topic"], spec["name"])
            seen.add(key)
            catalogue.append(spec)
        for spec in fetch_discovery()["results"]:
            key = (spec["topic"], spec["name"])
            if key not in seen:
                catalogue.append(spec)
        return catalogue

    def topics(self) -> list[str]:
        return sorted({t["topic"] for t in self._catalogue()})

    def tasks(self, topic: str) -> list[str]:
        return sorted(t["name"] for t in self._catalogue() if t["topic"] == topic)

    def get(self, topic: str, name: str) -> dict | None:
        for t in self._catalogue():
            if t["topic"] == topic and t["name"] == name:
                return t
        return None


_client = DiscoveryClient()


def list_topics() -> list[str]:
    return _client.topics()


def get_topic(name: str) -> "_TopicProxy":
    if name not in _client.topics():
        raise AttributeError(f"no compute topic {name!r}")
    return _TopicProxy(name)


def _py_annotation(prop: dict) -> Any:
    if "enum" in prop:
        return Literal[tuple(prop["enum"])]  # type: ignore[misc]
    return _JSON_TO_PY.get(prop.get("type", ""), Any)


def _signature_from_schema(spec: dict) -> inspect.Signature:
    schema = spec["parameters"]
    props: dict = schema.get("properties", {})
    required = list(schema.get("required", []))
    optional = [name for name in props if name not in required]

    params: list[inspect.Parameter] = []
    for name in required:
        params.append(
            inspect.Parameter(
                name,
                inspect.Parameter.KEYWORD_ONLY,
                annotation=_py_annotation(props[name]),
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
    """Stand-in for real reference resolution; just shows where it would happen."""
    props: dict = spec["parameters"].get("properties", {})
    resolved: dict = {}
    for key, value in kwargs.items():
        ref = props.get(key, {}).get("reference_to")
        if ref == "attribute":
            resolved[key] = f"attributes[?name=='{value}']"  # mocked JMESPath
        elif ref == "geoscience-object":
            resolved[key] = f"https://mock-hub/objects/{value}"  # mocked URL
        elif ref == "file":
            resolved[key] = f"https://mock-hub/file/v2/.../files/{value}"  # mocked URL
        else:
            resolved[key] = value
    return resolved


# --------------------------------------------------------------------------- #
# Output side: hydrate the result GENERICALLY from the discovery `results`
# block. Nothing here is task-specific — it is driven entirely by the schema's
# `output` / `reference_to` / `supported_schemas` / `attribute_path` vocabulary.
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
    """Mocked object load. The real engine fetches via the geoscience-object SDK,
    dispatched on ``schema_id`` — a generic capability shared by every task."""
    return GeoscienceObject(schema_id or "unknown", reference or "", name)


def _fabricate_output(spec: dict, provided: dict) -> dict | None:
    """Mock a server response that conforms to the task's `results` schema.

    Real execution returns this; here we synthesise it so the result has
    something to hydrate. Fabrication is itself schema-driven.
    """
    results = spec.get("results")
    if not results:
        return None
    return _fabricate_node(results, spec, provided, schema_id=None)


def _fabricate_node(node: dict, spec: dict, provided: dict, schema_id: str | None) -> dict:
    props: dict = node.get("properties", {})
    ref_spec = props.get("reference", {})
    if ref_spec.get("reference_to") == "geoscience-object":
        schema_id = ref_spec.get("supported_schemas", ["unknown"])[0]
    out: dict = {}
    for fname, fspec in props.items():
        if isinstance(fspec, dict) and (fspec.get("type") == "object" or "properties" in fspec):
            out[fname] = _fabricate_node(fspec, spec, provided, schema_id)
        else:
            out[fname] = _fabricate_scalar(fname, fspec, spec, provided, schema_id)
    return out


def _fabricate_scalar(fname: str, fspec: dict, spec: dict, provided: dict, schema_id: str | None):
    ref = fspec.get("reference_to")
    if fname == "message":
        return f"{spec['name']} completed; wrote attribute {provided.get('target')!r}."
    if fname == "schema_id":
        return schema_id
    if fname == "description":
        return None
    if ref == "geoscience-object":
        return f"https://mock-hub/objects/{spec['name']}-output"
    if ref == "attribute" and "attribute_path" in fspec:
        # SELF-HEAL: read the resolution expression from the LIVE schema, keyed
        # by the runtime object schema_id. A field rename in attribute_path is
        # picked up here with no client release.
        exprs = fspec["attribute_path"].get(schema_id or "")
        return exprs[0] if exprs else None
    if fname == "name":
        return provided.get("target", f"{spec['name']}-output")
    return None


class _ResultNode:
    """Generic, schema-shaped view over one object in the result payload.

    Mirrors whatever the `results` schema declares; when a node is tagged
    ``output: geoscience-object`` it also exposes ``get_object``/``to_dataframe``.
    """

    def __init__(self, schema_node: dict, payload: dict, schema_id: str | None = None) -> None:
        props: dict = schema_node.get("properties", {})
        self._schema_id = payload.get("schema_id", schema_id) if isinstance(payload, dict) else schema_id
        self._is_object = schema_node.get("output") == "geoscience-object"
        for fname, fspec in props.items():
            val = payload.get(fname) if isinstance(payload, dict) else None
            if isinstance(fspec, dict) and (fspec.get("type") == "object" or "properties" in fspec):
                setattr(self, fname, _ResultNode(fspec, val or {}, self._schema_id))
            else:
                setattr(self, fname, val)

    def get_object(self) -> GeoscienceObject:
        if not self._is_object:
            raise AttributeError("this result field is not a geoscience object")
        return _load_object(self._schema_id, getattr(self, "reference", None), getattr(self, "name", None))

    def to_dataframe(self) -> Table:
        return self.get_object().to_dataframe()

    def __repr__(self) -> str:
        fields = [k for k in self.__dict__ if not k.startswith("_")]
        return f"<{', '.join(fields)}>"


class TaskResult(_ResultNode):
    """The object `run(...)` returns. Built entirely from the `results` schema."""

    def __init__(self, results_schema: dict | None, payload: dict) -> None:
        if results_schema is None:
            # Task advertised no result schema (e.g. a brand-new task): degrade
            # gracefully to a generic message-only result.
            self._schema_id = None
            self._is_object = False
            self.message = payload.get("message", "")
            self.submitted = payload.get("_submitted", {})
        else:
            super().__init__(results_schema, payload)

    def __repr__(self) -> str:
        return f"TaskResult(message={getattr(self, 'message', None)!r})"


def _make_run(spec: dict):
    sig = _signature_from_schema(spec)

    def run(**kwargs: Any) -> TaskResult:
        bound = sig.bind_partial(**kwargs)
        bound.apply_defaults()
        provided = dict(bound.arguments)
        provided.pop("preview", None)

        required = set(spec["parameters"].get("required", []))
        missing = required - {k for k, v in provided.items() if v is not None}
        if missing:
            raise TypeError(f"{spec['name']}.run() missing required parameter(s): {sorted(missing)}")

        # Inputs would be resolved + POSTed here; execution is mocked.
        _mock_resolve(spec, provided)
        output = _fabricate_output(spec, provided)
        if output is not None:
            return TaskResult(spec["results"], output)
        return TaskResult(None, {"message": f"{spec['name']} submitted (no result schema advertised)."})

    run.__name__ = "run"
    run.__qualname__ = f"{spec['name']}.run"
    run.__signature__ = sig  # type: ignore[attr-defined]
    run.__doc__ = (spec.get("description") or "") + "\n\nParameters are resolved from the live schema."
    return run


def _load_override(topic: str, task: str) -> ModuleType | None:
    """Convention import of a per-task override, if one exists.

    The engine looks for ``poc_compute_engine.overrides.<topic>.<task>``. If that
    module exists it OWNS the task: its hand-written, fully-typed ``run`` is used
    instead of the schema-synthesised one, transparently to the caller (no client
    code change). If it does not exist, the generic schema-driven path is used.
    """
    mod_name = f"{__package__}.overrides.{topic}.{task}"
    try:
        return importlib.import_module(mod_name)
    except ModuleNotFoundError:
        return None


class _TaskProxy:
    def __init__(self, spec: dict, override: ModuleType | None = None) -> None:
        self._spec = spec
        self._override = override
        # An override gets to fully control `run`; otherwise synthesise it.
        self.run = override.run if override is not None else _make_run(spec)

    def __dir__(self):
        return ["run"]

    def __repr__(self) -> str:
        kind = "override" if self._override is not None else "generic"
        return f"<task {self._spec['topic']}/{self._spec['name']} v{self._spec.get('version')} ({kind})>"


class _TopicProxy:
    def __init__(self, topic: str) -> None:
        self._topic = topic

    def __getattr__(self, name: str) -> _TaskProxy:
        spec = _client.get(self._topic, name)
        if spec is None:
            available = ", ".join(_client.tasks(self._topic))
            raise AttributeError(f"no task {name!r} in topic {self._topic!r}. Available: {available}")
        override = _load_override(self._topic, name)
        return _TaskProxy(spec, override)

    def __dir__(self):
        return _client.tasks(self._topic)

    def __repr__(self) -> str:
        return f"<topic {self._topic!r} tasks=[{', '.join(_client.tasks(self._topic))}]>"

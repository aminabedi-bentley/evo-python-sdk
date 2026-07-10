"""The real reference resolver — the piece the POC previously stubbed.

``engine._mock_resolve`` faked URLs/JMESPath for top-level params only. Real task
schemas are **nested** (``source: {object, attribute, filter}``) and carry a small,
**closed annotation vocabulary** that fully describes how a friendly Python value maps to
the platform's wire payload. This module implements a single generic walker that reads
that vocabulary off *any* task's ``parameters`` schema and produces the wire-correct
payload — so one resolver serves every task, present and future, with no per-task code.

The closed vocabulary (everything the platform uses):

    reference_to: geoscience-object   leaf is an object handle  -> validated object URL
    reference_to: attribute           leaf is an attribute      -> JMESPath expression
    reference_to: file                leaf is a file handle     -> File API URL
    supported_schemas: [...]          allowed object schema ids -> validate the loaded obj
    attribute_from: <pointer>         sibling pointer to the object an attribute lives on
    attribute_path: {schema_id: [...]} per-object-schema JMESPath base -> pick by schema_id
    target: attribute                 marks a create/update attribute slot (oneOf branch)
    discriminator: <key>              discriminated union -> pick oneOf branch by value[key]

``attribute`` resolution needs the *owning object's* ``schema_id`` to choose the right
JMESPath base (a ``pointset`` keeps attributes under ``locations.attributes``; a
``block-model`` under ``attributes``). The owning object is located with ``attribute_from``
and then **loaded** to read its ``schema_id`` — which is why the resolver takes an
injectable :class:`ObjectLoader`: a real authenticated SDK load in the notebook, a fake
in-memory loader in offline tests (reproducible + type-checkable without credentials).

As an ergonomic shortcut, a caller holding an **already-loaded** object can pass its SDK
typed attribute directly (``pointset.attributes["grade"]``): the resolver reads the owning
object and the JMESPath straight off the attribute (see :func:`_typed_attribute_parts`),
skipping the ``attribute_from``/loader round-trip entirely.

The resolver mirrors the SDK's ``tasks/common/source_target.py`` semantics
(``_convert_object_reference`` -> URL, ``_get_attribute_expression`` -> ``base[?...]``)
but is driven **generically from the schema annotations** rather than hand-written per
task.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Protocol, Union, runtime_checkable

__all__ = [
    "AttributeInput",
    "AttributeRef",
    "CreateAttr",
    "FileInput",
    "FileRef",
    "LoadedObject",
    "ObjectHandle",
    "ObjectInput",
    "ObjectLoader",
    "ReferenceResolver",
    "SchemaValidationError",
    "TargetAttrInput",
    "UpdateAttr",
]


# --------------------------------------------------------------------------- #
# Loader boundary — the one place the resolver reaches the platform.
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class LoadedObject:
    """The minimum an :class:`ObjectLoader` must return: a URL and a schema id.

    ``schema_id`` drives ``attribute_path``/``supported_schemas``; ``reference`` is the
    validated object URL that lands in the wire payload.
    """

    reference: str
    schema_id: str


@runtime_checkable
class ObjectLoader(Protocol):
    """Resolves a user-supplied object handle to a :class:`LoadedObject`.

    Real implementation (notebook): authenticated load via the objects service. Fake
    implementation (tests): a dict lookup. The resolver only depends on this protocol, so
    it stays offline-testable and free of SDK/credential coupling.
    """

    async def load(self, handle: Any) -> LoadedObject: ...


@dataclass(frozen=True)
class ObjectHandle:
    """A plain, loader-agnostic handle to a geoscience object.

    Carrying ``schema_id`` here lets the resolver validate ``supported_schemas`` and pick
    an ``attribute_path`` *without* a load round-trip; if it is ``None`` the resolver asks
    the loader. ``reference`` is the object URL (or bare id the loader understands).
    """

    reference: str
    schema_id: str | None = None


# --------------------------------------------------------------------------- #
# Friendly input value types (the ergonomic surface a caller passes).
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class AttributeRef:
    """An attribute selected by name on some owning object.

    The owning object is *not* named here — the schema's ``attribute_from`` says which
    sibling object the attribute belongs to, and the resolver loads that object to pick
    the JMESPath base. Pass ``expression`` to bypass resolution with a ready JMESPath.
    """

    name: str
    expression: str | None = None


@dataclass(frozen=True)
class CreateAttr:
    """Target slot: create a new attribute called ``name``."""

    name: str


@dataclass(frozen=True)
class UpdateAttr:
    """Target slot: update an existing attribute (by name on the target object)."""

    name: str


@dataclass(frozen=True)
class FileRef:
    """A file handle to be resolved to a File API URL."""

    file_id: str


class SchemaValidationError(ValueError):
    """A referenced object's ``schema_id`` is not in the annotation's ``supported_schemas``."""


# --------------------------------------------------------------------------- #
# Friendly input unions — the *accepted* type for each kind of reference.
#
# These name exactly what a caller may pass for a reference-bearing parameter, so the
# generated ``run(...)`` signature and ``.pyi`` can advertise them instead of a bare
# ``str``/``dict``. A geoscience-object reference accepts a plain URL ``str``, an
# :class:`ObjectHandle`, or an already-:class:`LoadedObject` (SDK-typed objects that
# expose ``.metadata`` are also accepted at runtime and load through the same path).
# --------------------------------------------------------------------------- #

ObjectInput = Union[str, ObjectHandle, LoadedObject]
"""What a ``reference_to: geoscience-object`` parameter accepts."""

FileInput = Union[str, FileRef]
"""What a ``reference_to: file`` parameter accepts."""

AttributeInput = Union[str, AttributeRef]
"""What a ``reference_to: attribute`` (read) parameter accepts."""

TargetAttrInput = Union[str, CreateAttr, UpdateAttr]
"""What a ``target: attribute`` slot accepts (create a new / update an existing attribute)."""


# --------------------------------------------------------------------------- #
# Schema helpers.
# --------------------------------------------------------------------------- #

# "pointset/[>=1.2,<2]" -> family "pointset", range "[>=1.2,<2]"
_SUPPORTED_RE = re.compile(r"^(?P<family>[^/]+)/\[(?P<lo>[^,]+),(?P<hi>[^\]]+)\]$")


def _family(schema_id: str) -> str:
    """The object-family part of a schema id (``pointset/1.2.0`` -> ``pointset``)."""
    return schema_id.split("/", 1)[0]


def _schema_matches(schema_id: str, pattern: str) -> bool:
    """Loose match of a concrete ``schema_id`` against a ``supported_schemas`` pattern.

    Patterns look like ``"pointset/[>=1.2,<2]"``. We match on the object family and, when
    both the pattern and the id expose a parseable version, keep it within the declared
    half-open range. Family match alone is accepted when versions are unparseable — the
    POC favours not rejecting a legitimate object over strict SemVer parsing.
    """
    m = _SUPPORTED_RE.match(pattern)
    fam = m.group("family") if m else _family(pattern)
    if _family(schema_id) != fam:
        return False
    if not m:
        return True
    ver = _parse_version(schema_id)
    if ver is None:
        return True  # can't parse -> don't over-reject in the POC
    lo_ok = _cmp_bound(ver, m.group("lo").strip())
    hi_ok = _cmp_bound(ver, m.group("hi").strip())
    return lo_ok and hi_ok


def _parse_version(schema_id: str) -> tuple[int, ...] | None:
    tail = schema_id.split("/", 1)[1] if "/" in schema_id else schema_id
    nums = re.findall(r"\d+", tail)
    return tuple(int(n) for n in nums[:3]) if nums else None


def _cmp_bound(ver: tuple[int, ...], bound: str) -> bool:
    m = re.match(r"(>=|<=|>|<|=)?\s*([\d.]+)", bound)
    if not m:
        return True
    op = m.group(1) or ">="
    target = tuple(int(n) for n in m.group(2).split(".") if n != "")
    n = max(len(ver), len(target))
    a = ver + (0,) * (n - len(ver))
    b = target + (0,) * (n - len(target))
    return {
        ">=": a >= b,
        "<=": a <= b,
        ">": a > b,
        "<": a < b,
        "=": a == b,
    }[op]


def _deref(node: dict, root: dict) -> dict:
    """Resolve a local ``$ref`` (``#/$defs/Name`` / ``#/definitions/Name``) once."""
    ref = node.get("$ref")
    if not ref or not ref.startswith("#/"):
        return node
    target: Any = root
    for part in ref[2:].split("/"):
        target = target[part]
    # Merge sibling keys (e.g. an ``attribute_from`` placed next to a ``$ref``).
    merged = dict(target)
    for k, v in node.items():
        if k != "$ref":
            merged[k] = v
    return merged


def _pick_branch(node: dict, value: Any, root: dict) -> dict:
    """Select the ``oneOf``/``anyOf`` branch of a discriminated union by ``value[key]``.

    Branches encode the tag as ``properties[<key>].const``; we match that against the
    discriminator value the caller supplied. Falls back to the node itself when the value
    is not a dict or nothing matches (the generic pass-through then applies).
    """
    key = node["discriminator"]
    if isinstance(key, dict):  # OpenAPI-style {"propertyName": "type"}
        key = key.get("propertyName", "type")
    tag = value.get(key) if isinstance(value, dict) else None
    for branch in node.get("oneOf", []) or node.get("anyOf", []):
        b = _deref(branch, root)
        const = b.get("properties", {}).get(key, {}).get("const")
        if const == tag:
            return b
    return node


# --------------------------------------------------------------------------- #
# The resolver.
# --------------------------------------------------------------------------- #


@dataclass
class _Ctx:
    """Per-``resolve`` state: the schema root (for ``$ref``) and the resolved param root.

    ``resolved_root`` is built incrementally so an ``attribute_from`` pointer that targets
    an already-resolved sibling object (e.g. ``"1/object"``) reads the *resolved URL*.
    """

    schema_root: dict
    resolved_root: dict = field(default_factory=dict)


class ReferenceResolver:
    """Walks a task's ``parameters`` schema + kwargs and emits the wire payload.

    One instance serves every task. Construct it with an :class:`ObjectLoader`; call
    :meth:`resolve` with a discovery task ``spec`` and the caller's kwargs.
    """

    def __init__(self, loader: ObjectLoader, *, strict_schemas: bool = True) -> None:
        self._loader = loader
        self._strict = strict_schemas
        self._object_cache: dict[str, LoadedObject] = {}

    async def resolve(self, spec: dict, kwargs: dict[str, Any]) -> dict[str, Any]:
        """Resolve ``kwargs`` against ``spec['parameters']`` into a wire payload dict."""
        schema = spec["parameters"]
        ctx = _Ctx(schema_root=schema)
        out: dict[str, Any] = {}
        ctx.resolved_root = out
        props: dict = schema.get("properties", {})
        for key, value in kwargs.items():
            if value is None and key not in schema.get("required", []):
                # Preserve an *explicit* null only when the schema declares the field
                # nullable (missing != null: e.g. declustering ``power`` = null -> KNN).
                if _is_nullable(props.get(key, {})) and key in kwargs:
                    out[key] = None
                continue
            node = _deref(props.get(key, {}), schema)
            # Resolve top-level params in order, writing each into ``resolved_root`` live
            # so a later param's ``attribute_from`` pointer can read an already-resolved
            # sibling object (e.g. a filter's "/source/object").
            out[key] = await self._resolve_node(node, value, [key], ctx)
        return out

    async def _resolve_node(
        self, node: dict, value: Any, path: list[str], ctx: _Ctx
    ) -> Any:
        node = _deref(node, ctx.schema_root)

        # 1. Discriminated union -> pick the branch, then resolve within it.
        if "discriminator" in node and (node.get("oneOf") or node.get("anyOf")):
            node = _pick_branch(node, value, ctx.schema_root)

        # 2. reference_to leaves.
        ref = node.get("reference_to")
        if ref == "geoscience-object":
            return await self._resolve_object(node, value)
        if ref == "attribute":
            return await self._resolve_attribute(node, value, path, ctx)
        if ref == "file":
            return _resolve_file(value)

        # 3. target attribute slot (create/update oneOf).
        if node.get("target") == "attribute":
            return await self._resolve_target_attribute(node, value, path, ctx)

        # 4. object with properties -> recurse into provided children. Register the
        # container in ``resolved_root`` up front and fill it in place, so a child's
        # ``attribute_from`` (e.g. ``source.attribute`` -> ``1/object``) can read a sibling
        # object that was resolved a moment earlier in the same pass.
        props = node.get("properties")
        if props:
            # Typed-attribute shorthand: a source/target frame (``{object, attribute}``)
            # given a bare SDK typed attribute (``pointset.attributes["grade"]``) expands
            # to ``{object: <its owning object>, attribute: <the attribute>}`` — both the
            # object URL and the JMESPath then fall out of the normal recursion.
            if "object" in props and "attribute" in props:
                parts = _typed_attribute_parts(value)
                if parts is not None and parts.owner is not None and not isinstance(value, dict):
                    value = {"object": parts.owner, "attribute": value}
        if props and isinstance(value, dict):
            resolved: dict[str, Any] = {}
            _set_at(ctx.resolved_root, path, resolved)
            for k, v in value.items():
                child = _deref(props.get(k, {}), ctx.schema_root)
                resolved[k] = await self._resolve_node(child, v, path + [k], ctx)
            return resolved

        # 5. array -> resolve each item against the item schema.
        if node.get("type") == "array" and isinstance(value, list):
            item_schema = _deref(node.get("items", {}), ctx.schema_root)
            return [
                await self._resolve_node(item_schema, item, path + [str(i)], ctx)
                for i, item in enumerate(value)
            ]

        # 6. scalar / already-shaped value -> pass through.
        return value

    # -- leaf resolvers ---------------------------------------------------- #

    async def _resolve_object(self, node: dict, value: Any) -> str:
        loaded = await self._load(value)
        supported = node.get("supported_schemas")
        if supported and self._strict:
            if not any(_schema_matches(loaded.schema_id, p) for p in supported):
                raise SchemaValidationError(
                    f"object schema {loaded.schema_id!r} is not in supported_schemas "
                    f"{supported!r}"
                )
        return loaded.reference

    async def _resolve_attribute(
        self, node: dict, value: Any, path: list[str], ctx: _Ctx
    ) -> Any:
        # Typed-attribute shorthand: derive the JMESPath straight from the SDK attribute
        # (its key/name + container), mirroring the SDK's ``_get_attribute_expression`` —
        # no ``attribute_from``/loader round-trip needed.
        parts = _typed_attribute_parts(value)
        if parts is not None:
            return parts.expression
        if isinstance(value, AttributeRef) and value.expression is not None:
            return value.expression
        name = value.name if isinstance(value, AttributeRef) else value
        if not isinstance(name, str):
            # Already a resolved expression or unknown shape -> pass through.
            return value
        base = await self._attribute_base(node, path, ctx)
        return f"{base}[?name=='{name}']"

    async def _resolve_target_attribute(
        self, node: dict, value: Any, path: list[str], ctx: _Ctx
    ) -> dict[str, Any]:
        # Typed-attribute shorthand: an existing attribute -> update (by its expression);
        # a pending one -> create (by name). Mirrors the SDK's ``_validate_target_attribute``.
        parts = _typed_attribute_parts(value)
        if parts is not None:
            if parts.exists:
                return {"operation": "update", "reference": parts.expression}
            return {"operation": "create", "name": parts.name}
        # create -> {operation: create, name}; update -> {operation: update, reference}.
        if isinstance(value, CreateAttr):
            return {"operation": "create", "name": value.name}
        if isinstance(value, UpdateAttr):
            ref_node = _find_update_reference_node(node, ctx.schema_root)
            base = await self._attribute_base(ref_node, path, ctx)
            return {"operation": "update", "reference": f"{base}[?name=='{value.name}']"}
        if isinstance(value, dict):
            return value  # caller passed the wire shape verbatim
        # bare string name -> create (the common case).
        if isinstance(value, str):
            return {"operation": "create", "name": value}
        return value

    async def _attribute_base(self, node: dict, path: list[str], ctx: _Ctx) -> str:
        """The JMESPath container for an attribute, chosen by the owning object schema.

        ``attribute_from`` locates the owning object; we load it (or read a supplied
        ``schema_id``) and index ``attribute_path`` by the object's schema family. Every
        candidate for a family shares the same container (they differ only by
        ``attribute_type``), so the base is unambiguous once the family is known.
        """
        amap = node.get("attribute_path")
        pointer = node.get("attribute_from")
        if not amap or not pointer:
            return "attributes"
        schema_id = await self._owning_schema_id(pointer, path, ctx)
        for pattern, templates in amap.items():
            if schema_id is not None and _schema_matches(schema_id, pattern):
                return _base_of(templates[0] if isinstance(templates, list) else templates)
        # Unknown object family -> safest generic default.
        return "attributes"

    async def _owning_schema_id(
        self, pointer: str, path: list[str], ctx: _Ctx
    ) -> str | None:
        """Resolve ``attribute_from`` to the owning object's ``schema_id``.

        Supports absolute JSON pointers (``/target/object``) and relative pointers
        (``1/object`` = up one instance level, then ``/object``). The pointed-at value is
        the object handle the caller supplied for that sibling; we load it for its
        ``schema_id`` (cached).
        """
        handle = _pointer_value(pointer, path, ctx)
        if handle is None:
            return None
        try:
            loaded = await self._load(handle)
        except Exception:
            return None
        return loaded.schema_id

    async def _load(self, handle: Any) -> LoadedObject:
        if isinstance(handle, ObjectHandle) and handle.schema_id is not None:
            loaded = LoadedObject(reference=handle.reference, schema_id=handle.schema_id)
            # Cache by the resolved reference so an ``attribute_from`` pointer that later
            # reads this sibling's resolved URL maps back to the same schema id.
            self._object_cache[loaded.reference] = loaded
            return loaded
        cache_key = _handle_key(handle)
        if cache_key is not None and cache_key in self._object_cache:
            return self._object_cache[cache_key]
        loaded = await self._loader.load(handle)
        if cache_key is not None:
            self._object_cache[cache_key] = loaded
        # Also key by the resolved reference URL: an ``attribute_from`` pointer reads the
        # already-resolved sibling *URL*, and must map back to this same schema id.
        self._object_cache[loaded.reference] = loaded
        return loaded


# --------------------------------------------------------------------------- #
# Module-level helpers (pure).
# --------------------------------------------------------------------------- #


def _is_nullable(node: dict) -> bool:
    t = node.get("type")
    return t == "null" or (isinstance(t, list) and "null" in t)


@dataclass(frozen=True)
class _TypedAttr:
    """The parts extracted from an SDK typed attribute for shorthand resolution."""

    owner: Any  # the owning object (an ``_obj``: DownloadedObject / typed object)
    expression: str  # the JMESPath selecting this attribute on its object
    exists: bool  # True -> update target; False -> create target
    name: str  # the attribute name (used when creating)


def _typed_attribute_parts(value: Any) -> _TypedAttr | None:
    """Recognise an SDK typed attribute and extract its owner + JMESPath.

    Accepts ``evo.objects.typed`` attributes so a caller can pass
    ``pointset.attributes["grade"]`` (or a pending/block-model attribute) directly instead
    of a separate object + :class:`AttributeRef`. Mirrors the SDK's
    ``tasks/common/source_target.py`` (`_get_attribute_expression`):

    - existing ``Attribute``: ``"{schema_path}[?key=='{key}']"`` (schema_path or ``attributes``)
    - ``PendingAttribute`` / ``BlockModelAttribute`` / ``BlockModelPendingAttribute``:
      ``"attributes[?name=='{name}']"``

    The import is lazy and failure-tolerant, so the resolver stays usable (and
    offline-testable) even where ``evo.objects`` isn't importable. Returns ``None`` for
    anything that isn't a typed attribute.
    """
    try:
        from evo.objects.typed import (  # lazy: keep the resolver SDK-decoupled
            Attribute,
            BlockModelAttribute,
            BlockModelPendingAttribute,
            PendingAttribute,
        )
    except Exception:
        return None

    if isinstance(value, Attribute):
        base = getattr(getattr(value, "_context", None), "schema_path", None) or "attributes"
        return _TypedAttr(
            owner=getattr(value, "_obj", None),
            expression=f"{base}[?key=='{value.key}']",
            exists=True,
            name=value.name,
        )
    if isinstance(value, (PendingAttribute, BlockModelAttribute, BlockModelPendingAttribute)):
        return _TypedAttr(
            owner=getattr(value, "_obj", None),
            expression=f"attributes[?name=='{value.name}']",
            exists=bool(getattr(value, "exists", False)),
            name=value.name,
        )
    return None


def _resolve_file(value: Any) -> str:
    file_id = value.file_id if isinstance(value, FileRef) else value
    if isinstance(file_id, str) and file_id.startswith("http"):
        return file_id
    return f"/file/v2/files/{file_id}"


def _base_of(template: str) -> str:
    """Strip a JMESPath predicate to its container base.

    ``locations.attributes[?attribute_type=='scalar']`` -> ``locations.attributes``.
    """
    idx = template.find("[")
    return template[:idx] if idx != -1 else template


def _find_update_reference_node(node: dict, root: dict) -> dict:
    """Find the ``reference`` leaf inside a ``target: attribute`` oneOf (the update branch)."""
    for branch in node.get("oneOf", []):
        b = _deref(branch, root)
        ref_node = b.get("properties", {}).get("reference")
        if ref_node is not None:
            return _deref(ref_node, root)
    return node


def _handle_key(handle: Any) -> str | None:
    if isinstance(handle, str):
        return handle
    if isinstance(handle, ObjectHandle):
        return handle.reference
    ref = getattr(handle, "reference", None) or getattr(handle, "url", None)
    return str(ref) if ref is not None else None


def _pointer_value(pointer: str, path: list[str], ctx: _Ctx) -> Any:
    """Resolve an ``attribute_from`` pointer to the sibling value the caller supplied.

    Absolute (``/target/object``): index the resolved param root from the top.
    Relative (``1/object``): drop N trailing segments from the current instance ``path``
    (N = the leading integer), then follow the remaining path segments.

    Reads from ``resolved_root`` first (so a sibling object already resolved to its URL is
    reused), falling back to nothing — the caller then treats the schema id as unknown.
    """
    if pointer.startswith("/"):
        segments = [s for s in pointer.split("/") if s != ""]
        return _index(ctx.resolved_root, segments)
    # Relative pointer: "<N>/<rest...>". N is the minimum climb; because the platform
    # writes the same relative pointer (e.g. "1/object") on an attribute whether it sits
    # directly under ``source`` or several levels deeper inside ``source.filter.where``,
    # we climb *at least* N ancestors and keep going until the trailing key resolves —
    # i.e. bind to the nearest enclosing frame that actually owns that object.
    head, _, rest = pointer.partition("/")
    try:
        up = int(head)
    except ValueError:
        return None
    rest_segments = [s for s in rest.split("/") if s != ""]
    for climb in range(max(up, 1), len(path) + 1):
        segments = path[:-climb] + rest_segments
        value = _index(ctx.resolved_root, segments)
        if value is not None:
            return value
    return None


def _index(root: Any, segments: list[str]) -> Any:
    cur = root
    for seg in segments:
        if isinstance(cur, dict) and seg in cur:
            cur = cur[seg]
        else:
            return None
    return cur


def _set_at(root: dict, path: list[str], container: dict) -> None:
    """Attach ``container`` at ``path`` inside ``root``, creating intermediate dicts.

    Used to register a nested object's resolved container *before* its children are
    resolved, so sibling ``attribute_from`` pointers can read it mid-pass.
    """
    cur = root
    for seg in path[:-1]:
        nxt = cur.get(seg)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[seg] = nxt
        cur = nxt
    if path:
        cur[path[-1]] = container

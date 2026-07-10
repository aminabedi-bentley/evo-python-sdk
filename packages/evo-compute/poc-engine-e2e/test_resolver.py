"""Offline tests for :mod:`poc_compute_engine.resolver`.

Runs with **no credentials and no network**: a fake in-memory :class:`ObjectLoader`
stands in for the objects service, and the resolver is driven against the *real* bundled
task schemas (``poc_compute_engine/schemas/``). Each test pins one branch of the closed
annotation vocabulary so a regression in any single rule is caught in isolation.

Run:  ``uv run python test_resolver.py``   (exits non-zero on first failure)
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, cast

from poc_compute_engine.resolver import (
    AttributeRef,
    CreateAttr,
    LoadedObject,
    ObjectHandle,
    ReferenceResolver,
    SchemaValidationError,
    UpdateAttr,
)

_SCHEMAS = Path(__file__).resolve().parent / "poc_compute_engine" / "schemas" / "geostatistics"


def _spec(task: str) -> dict:
    schema = json.loads((_SCHEMAS / task / "schema.json").read_text())
    topic, name = "geostatistics", task
    return {"topic": topic, "name": name, **schema}


class FakeLoader:
    """A dict-backed :class:`ObjectLoader`: handle (id/url/ObjectHandle) -> LoadedObject."""

    def __init__(self, objects: dict[str, LoadedObject]) -> None:
        self._objects = objects

    async def load(self, handle: Any) -> LoadedObject:
        key = handle.reference if isinstance(handle, ObjectHandle) else handle
        if key not in self._objects:
            raise KeyError(f"fake loader has no object for {key!r}")
        return self._objects[key]


# Two catalogue objects with different families -> different attribute containers.
POINTSET = LoadedObject(reference="https://hub/objects/ps-1", schema_id="pointset/1.2.0")
BLOCKMODEL = LoadedObject(reference="https://hub/objects/bm-1", schema_id="block-model/1.0.0")
VARIOGRAM = LoadedObject(reference="https://hub/objects/vg-1", schema_id="variogram/1.1.0")

_LOADER = FakeLoader(
    {
        "ps-1": POINTSET,
        "bm-1": BLOCKMODEL,
        "vg-1": VARIOGRAM,
        POINTSET.reference: POINTSET,
        BLOCKMODEL.reference: BLOCKMODEL,
        VARIOGRAM.reference: VARIOGRAM,
    }
)


def _resolver(strict: bool = True) -> ReferenceResolver:
    return ReferenceResolver(_LOADER, strict_schemas=strict)


# --------------------------------------------------------------------------- #
# Tests.
# --------------------------------------------------------------------------- #


async def test_object_ref_to_url() -> None:
    """``reference_to: geoscience-object`` -> the loaded object's validated URL."""
    spec = _spec("kriging-gcp")
    out = await _resolver().resolve(
        spec, {"source": {"object": "ps-1"}, "target": {"object": "bm-1"}}
    )
    assert out["source"]["object"] == POINTSET.reference, out
    assert out["target"]["object"] == BLOCKMODEL.reference, out


async def test_attribute_expression_by_owning_schema() -> None:
    """``reference_to: attribute`` picks the JMESPath base from the OWNING object's family.

    ``source.attribute``'s ``attribute_from='1/object'`` points at ``source.object`` (a
    pointset) -> ``locations.attributes``. ``target`` update points at a block-model ->
    ``attributes``.
    """
    spec = _spec("kriging-gcp")
    out = await _resolver().resolve(
        spec,
        {
            "source": {"object": "ps-1", "attribute": AttributeRef("grade")},
            "target": {"object": "bm-1", "attribute": UpdateAttr("kriged")},
        },
    )
    assert out["source"]["attribute"] == "locations.attributes[?name=='grade']", out
    assert out["target"]["attribute"] == {
        "operation": "update",
        "reference": "attributes[?name=='kriged']",
    }, out


async def test_target_create_attribute() -> None:
    """``target: attribute`` with a create input -> ``{operation: create, name}``."""
    spec = _spec("kriging-gcp")
    out = await _resolver().resolve(
        spec, {"target": {"object": "bm-1", "attribute": CreateAttr("kriged_grade")}}
    )
    assert out["target"]["attribute"] == {"operation": "create", "name": "kriged_grade"}, out


async def test_discriminated_union_kriging_method() -> None:
    """``discriminator: type`` selects the ``oneOf`` branch by ``value['type']``."""
    spec = _spec("kriging-gcp")
    out = await _resolver().resolve(spec, {"kriging_method": {"type": "simple", "mean": 1.5}})
    assert out["kriging_method"] == {"type": "simple", "mean": 1.5}, out


async def test_filter_discriminated_condition() -> None:
    """A ``source.filter.where`` condition resolves its inner ``reference_to: attribute``.

    Exercises: nested ``discriminator`` (``where.type == 'condition'``) + an attribute leaf
    whose ``attribute_from`` climbs back to ``source.object`` (a pointset).
    """
    spec = _spec("kriging-gcp")
    out = await _resolver().resolve(
        spec,
        {
            "source": {
                "object": "ps-1",
                "filter": {
                    "where": {
                        "type": "condition",
                        "operator": "greater_than",
                        "threshold": 0.5,
                        "attribute": AttributeRef("grade"),
                    }
                },
            }
        },
    )
    where = out["source"]["filter"]["where"]
    assert where["attribute"] == "locations.attributes[?name=='grade']", out
    assert where["operator"] == "greater_than" and where["threshold"] == 0.5, out


async def test_supported_schemas_rejects_wrong_family() -> None:
    """``supported_schemas`` on ``variogram`` rejects a non-variogram object (strict)."""
    spec = _spec("kriging-gcp")
    try:
        await _resolver().resolve(spec, {"variogram": "ps-1"})  # pointset, not variogram
    except SchemaValidationError:
        pass
    else:
        raise AssertionError("expected SchemaValidationError for a non-variogram object")


async def test_supported_schemas_accepts_valid() -> None:
    spec = _spec("kriging-gcp")
    out = await _resolver().resolve(spec, {"variogram": "vg-1"})
    assert out["variogram"] == VARIOGRAM.reference, out


async def test_object_handle_short_circuits_loader() -> None:
    """An ``ObjectHandle`` carrying its own ``schema_id`` needs no loader entry."""
    spec = _spec("kriging-gcp")
    handle = ObjectHandle(reference="https://hub/objects/xyz", schema_id="pointset/1.2.0")
    out = await _resolver().resolve(
        spec, {"source": {"object": handle, "attribute": AttributeRef("grade")}}
    )
    assert out["source"]["object"] == "https://hub/objects/xyz", out
    assert out["source"]["attribute"] == "locations.attributes[?name=='grade']", out


async def test_declustering_explicit_null_power_preserved() -> None:
    """Nullable ``power`` sent as ``None`` survives (missing != null: null -> KNN mode)."""
    spec = _spec("declustering")
    out = await _resolver().resolve(
        spec,
        {"source": {"object": "ps-1"}, "grid": {"object": "bm-1"}, "power": None},
    )
    assert "power" in out and out["power"] is None, out


async def test_declustering_omits_unset_optional() -> None:
    """An optional non-provided field is simply absent (not null)."""
    spec = _spec("declustering")
    out = await _resolver().resolve(spec, {"source": {"object": "ps-1"}})
    assert "power" not in out, out


# --- typed-attribute shorthand (SDK ``pointset.attributes["grade"]`` style) ----------- #

# Owners carry their schema_id inline, so these tests need no loader entries.
_PS_OWNER = ObjectHandle(reference=POINTSET.reference, schema_id="pointset/1.2.0")
_BM_OWNER = ObjectHandle(reference=BLOCKMODEL.reference, schema_id="block-model/1.0.0")


async def test_typed_attribute_source_shorthand() -> None:
    """A bare SDK typed attribute as ``source`` expands to object + JMESPath."""
    from evo.objects.typed import BlockModelAttribute

    attr = BlockModelAttribute(name="grade", attribute_type="Float64", obj=cast(Any, _BM_OWNER))
    out = await _resolver().resolve(_spec("kriging-gcp"), {"source": attr})
    assert out["source"]["object"] == BLOCKMODEL.reference, out
    assert out["source"]["attribute"] == "attributes[?name=='grade']", out


async def test_typed_attribute_target_pending_creates() -> None:
    """A pending typed attribute as ``target`` -> a create operation."""
    from evo.objects.typed import BlockModelPendingAttribute

    pending = BlockModelPendingAttribute(cast(Any, _BM_OWNER), "kriged")
    out = await _resolver().resolve(_spec("kriging-gcp"), {"target": pending})
    assert out["target"]["object"] == BLOCKMODEL.reference, out
    assert out["target"]["attribute"] == {"operation": "create", "name": "kriged"}, out


async def test_typed_attribute_target_existing_updates() -> None:
    """An existing typed attribute as ``target`` -> an update referencing its expression."""
    from evo.objects.typed import BlockModelAttribute

    attr = BlockModelAttribute(name="grade", attribute_type="Float64", obj=cast(Any, _BM_OWNER))
    out = await _resolver().resolve(_spec("kriging-gcp"), {"target": attr})
    assert out["target"]["attribute"] == {
        "operation": "update",
        "reference": "attributes[?name=='grade']",
    }, out


async def test_typed_attribute_in_explicit_attribute_field() -> None:
    """A typed attribute passed as the ``attribute`` field (object given) resolves directly."""
    from evo.objects.typed import BlockModelAttribute

    attr = BlockModelAttribute(name="grade", attribute_type="Float64", obj=cast(Any, _BM_OWNER))
    out = await _resolver().resolve(
        _spec("kriging-gcp"), {"source": {"object": "bm-1", "attribute": attr}}
    )
    assert out["source"]["attribute"] == "attributes[?name=='grade']", out


async def main() -> None:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            await t()
            print(f"  PASS  {t.__name__}")
        except Exception as exc:  # noqa: BLE001 - test harness
            failed += 1
            print(f"  FAIL  {t.__name__}: {type(exc).__name__}: {exc}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())

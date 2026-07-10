"""Static-DX sample: CORRECT usage. A type checker should report 0 errors.

All type information comes from the generated poc_compute_engine/__init__.pyi, which
types the authenticated ComputeClient tree. Run:
    pyright typed_usage_ok.py     (or: mypy typed_usage_ok.py)
"""

from typing import cast

from evo.common import IContext

from poc_compute_engine import ComputeClient
from poc_compute_engine.resolver import CreateAttr


# At runtime this is a real IContext (e.g. the ServiceManagerWidget `manager`); for a
# static type-check fixture we only need something typed as IContext.
async def main() -> None:
    async with ComputeClient(cast(IContext, None)) as client:
        # Autocomplete after `client.geostatistics.` lists the live tasks; `.run(` is typed.
        result = await client.geostatistics.kriging_gcp.run(
            source="grade",
            target="kriged_grade",
            variogram="vario-123",
            kriging_type="ordinary",  # Literal['simple', 'ordinary']
            max_samples=16,
        )

        message: str = result.message
        attr_name: str = result.target.attribute.name
        schema_id: str = result.target.schema_id

        obj = result.target.get_object()       # GeoscienceObject
        table = result.target.to_dataframe()   # Table

        # `kriging_gcp` is a SPECIALIZED override (poc_compute_engine/overrides/geostatistics/kriging_gcp.py),
        # so it exposes a hand-written surface the schema can't: a `mean` parameter and result
        # helpers. Same `await client...run(...)` DX; richer, fully-typed behaviour.
        simple = await client.geostatistics.kriging_gcp.run(
            source="grade",
            target="kriged_grade",
            variogram="vario-123",
            kriging_type="simple",
            mean=2.5,  # override-only parameter (absent from the discovery schema)
        )
        summary: str = simple.summary()      # override-only helper
        portal: str = simple.portal_url()    # override-only helper

        # `declustering` and `normal_score_gcp` stay GENERIC — their `run(...)` signatures
        # are synthesised from the live schema, so params take the schema-shaped values the
        # ReferenceResolver knows how to resolve (objects, attributes, targets, ...).
        declus = await client.geostatistics.declustering.run(
            source={"object": "ps-1"},
            grid={"object": "grid-1"},
            target={"object": "ps-1", "attribute": CreateAttr("weights")},
            neighborhood={"max_samples": 16},
            power=None,  # nullable -> KNN mode; the resolver preserves the explicit null
        )
        declus_attr: str = declus.target.attribute.name

        ns = await client.geostatistics.normal_score_gcp.run(
            method="forward",  # Literal['forward', 'backward']
            source={"object": "ps-1", "attribute": "grade"},
            distribution="dist-123",
            target={"object": "ps-1", "attribute": CreateAttr("grade_ns")},
        )
        ns_msg: str = ns.message

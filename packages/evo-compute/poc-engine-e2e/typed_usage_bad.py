"""Static-DX sample: WRONG usage. A type checker should FLAG every marked line.

Run:  pyright typed_usage_bad.py   (or: mypy typed_usage_bad.py)
"""

from typing import cast

from evo.common import IContext

from poc_compute_engine import ComputeClient


async def main() -> None:
    async with ComputeClient(cast(IContext, None)) as client:
        # 1. Wrong type for max_samples (str, expected int).
        await client.geostatistics.kriging_gcp.run(
            source="grade",
            target="kriged_grade",
            variogram="vario-123",
            max_samples="lots",  # type-error: expected int
        )

        # 2. Invalid Literal value for kriging_type.
        await client.geostatistics.kriging_gcp.run(
            source="grade",
            target="kriged_grade",
            variogram="vario-123",
            kriging_type="universal",  # type-error: not 'simple' | 'ordinary'
        )

        # 3. Missing required argument `variogram`.
        await client.geostatistics.kriging_gcp.run(  # type-error: missing 'variogram'
            source="grade",
            target="kriged_grade",
        )

        # 4. Unknown parameter.
        await client.geostatistics.kriging_gcp.run(
            source="grade",
            target="kriged_grade",
            variogram="vario-123",
            nugget=0.1,  # type-error: no such parameter
        )

        # 5. Task advertised at RUNTIME (post-snapshot) — runs live, but unknown to the
        #    stub snapshot until regenerated (point-in-time DX).
        await client.geostatistics.gaussian_simulation.run(  # type-error: unknown attribute
            source="grade",
            target="sim",
        )

        # 6. Unknown field on a typed result node.
        _ok = await client.geostatistics.kriging_gcp.run(source="grade", target="t", variogram="v")
        print(_ok.target.centroid)  # type-error: KrigingGcpResultTarget has no 'centroid'

        # 7. Wrong type for a hydrated object.
        table: int = _ok.target.to_dataframe()  # type-error: Table is not int

        # 8. Override-specific misuse: `mean` exists only on the specialized kriging_gcp override,
        #    and it is typed `float | None`.
        await client.geostatistics.kriging_gcp.run(
            source="grade",
            target="t",
            variogram="v",
            kriging_type="simple",
            mean="high",  # type-error: expected float | None
        )

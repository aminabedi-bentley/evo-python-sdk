"""Static-DX sample: WRONG usage. A type checker should FLAG every marked line.

Run:  pyright typed_usage_bad.py   (or: mypy typed_usage_bad.py)

This proves the stubs give real static checking — each error is caught BEFORE
running anything, exactly as it would be in an IDE.
"""

import poc_compute_engine

# 1. Wrong type for max_samples (str, expected int).
poc_compute_engine.geostatistics.kriging.run(
    source="grade",
    target="kriged_grade",
    variogram="vario-123",
    max_samples="lots",  # type-error: expected int
)

# 2. Invalid Literal value for kriging_type.
poc_compute_engine.geostatistics.kriging.run(
    source="grade",
    target="kriged_grade",
    variogram="vario-123",
    kriging_type="universal",  # type-error: not 'simple' | 'ordinary'
)

# 3. Missing required argument `variogram`.
poc_compute_engine.geostatistics.kriging.run(  # type-error: missing 'variogram'
    source="grade",
    target="kriged_grade",
)

# 4. Unknown parameter.
poc_compute_engine.geostatistics.kriging.run(
    source="grade",
    target="kriged_grade",
    variogram="vario-123",
    nugget=0.1,  # type-error: no such parameter
)

# 5. Task that exists at RUNTIME (added via discovery) but has no stub yet:
#    runs fine dynamically, but is unknown to the type checker (point-in-time DX).
poc_compute_engine.geostatistics.turning_bands.run(  # type-error: unknown attribute
    source="grade",
    target="sim",
)

# 6. Unknown field on a typed result node.
_ok = poc_compute_engine.geostatistics.kriging.run(source="grade", target="t", variogram="v")
print(_ok.target.centroid)  # type-error: KrigingResultTarget has no 'centroid'

# 7. Wrong type for a hydrated object.
table: int = _ok.target.to_dataframe()  # type-error: Table is not int

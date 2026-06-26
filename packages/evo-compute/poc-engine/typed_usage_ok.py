"""Static-DX sample: CORRECT usage. A type checker should report 0 errors.

Open this in an IDE (VS Code/Pylance, PyCharm) to see autocomplete + signature
help, OR run:  pyright typed_usage_ok.py   (or: mypy typed_usage_ok.py)

All type information here comes from the GENERATED STUBS, not from running code.
For kriging the stub re-exports the hand-written OVERRIDE, so the override's
richer typed surface (summary/portal_url) is what the checker sees.
"""

import poc_compute_engine

# kriging is OVERRIDE-backed: the stub re-exports overrides/geostatistics/kriging.py.
result = poc_compute_engine.geostatistics.kriging.run(
    source="grade",
    target="kriged_grade",
    variogram="vario-123",
    kriging_type="ordinary",  # Literal['simple', 'ordinary']
    max_samples=16,
)

# Override-only helpers are statically known.
summary: str = result.summary()
url: str = result.portal_url()

message: str = result.message
attr_name: str = result.target.attribute.name
schema_id: str = result.target.schema_id

# Output hydration is typed too: get_object() -> GeoscienceObject, to_dataframe() -> Table.
obj = result.target.get_object()
table = result.target.to_dataframe()

# declustering & normal_score are GENERIC (schema-derived stubs, no override).
declus = poc_compute_engine.geostatistics.declustering.run(
    source="grade",
    target="weights",
    cell_size=50.0,
)
declus_attr: str = declus.target.attribute.name

ns = poc_compute_engine.geostatistics.normal_score.run(
    source="grade",
    target="grade_ns",
    num_quantiles=200,
)
ns_msg: str = ns.message

"""Static-DX sample: CORRECT usage. A type checker should report 0 errors.

Open this in an IDE (VS Code/Pylance, PyCharm) to see autocomplete + signature
help, OR run:  pyright typed_usage_ok.py   (or: mypy typed_usage_ok.py)

All type information here comes from the GENERATED STUBS, not from running code.
"""

import poc_compute_engine

# Autocomplete after `poc_compute_engine.geostatistics.` lists: kriging, declustering.
# Signature help on `.run(` shows the typed, keyword-only parameters.
result = poc_compute_engine.geostatistics.kriging.run(
    source="grade",
    target="kriged_grade",
    variogram="vario-123",
    kriging_type="ordinary",  # Literal['simple', 'ordinary']
    max_samples=16,
)

# `result` is statically known to be KrigingResult.
message: str = result.message
attr_name: str = result.target.attribute.name
schema_id: str = result.target.schema_id

# Output hydration is typed too: get_object() -> GeoscienceObject, to_dataframe() -> Table.
obj = result.target.get_object()
table = result.target.to_dataframe()

declus = poc_compute_engine.geostatistics.declustering.run(
    source="grade",
    target="weights",
    cell_size=50.0,
)

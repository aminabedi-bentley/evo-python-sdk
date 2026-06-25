# AUTO-GENERATED FROM DISCOVERY SCHEMAS. DO NOT EDIT BY HAND.

from typing import Any, Literal

from poc_compute_engine._engine import GeoscienceObject, Table


class DeclusteringResultTargetAttribute:
    reference: str
    name: str

class DeclusteringResultTarget:
    reference: str
    name: str
    schema_id: str
    attribute: DeclusteringResultTargetAttribute
    def get_object(self) -> GeoscienceObject: ...
    def to_dataframe(self) -> Table: ...

class DeclusteringResult:
    message: str
    target: DeclusteringResultTarget

def run(
    *,
    source: str,
    target: str,
    cell_size: float = ...,
    preview: bool = False,
) -> DeclusteringResult:
    """Compute declustering weights for a sample attribute."""
    ...

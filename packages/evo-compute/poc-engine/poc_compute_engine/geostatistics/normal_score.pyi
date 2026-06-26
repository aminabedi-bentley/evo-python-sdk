# AUTO-GENERATED FROM OFFLINE SCHEMAS. DO NOT EDIT BY HAND.

from typing import Any, Literal

from poc_compute_engine._engine import GeoscienceObject, Table


class NormalScoreResultTargetAttribute:
    reference: str
    name: str

class NormalScoreResultTarget:
    reference: str
    name: str
    schema_id: str
    attribute: NormalScoreResultTargetAttribute
    def get_object(self) -> GeoscienceObject: ...
    def to_dataframe(self) -> Table: ...

class NormalScoreResult:
    message: str
    target: NormalScoreResultTarget

def run(
    *,
    source: str,
    target: str,
    weights: str = ...,
    num_quantiles: int = ...,
    preview: bool = False,
) -> NormalScoreResult:
    """Apply a normal-score transform to a sample attribute."""
    ...

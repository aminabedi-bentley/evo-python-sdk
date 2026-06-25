# AUTO-GENERATED FROM DISCOVERY SCHEMAS. DO NOT EDIT BY HAND.

from typing import Any, Literal

from poc_compute_engine._engine import GeoscienceObject, Table


class KrigingResultTargetAttribute:
    reference: str
    name: str

class KrigingResultTarget:
    reference: str
    name: str
    description: str | None
    schema_id: str
    attribute: KrigingResultTargetAttribute
    def get_object(self) -> GeoscienceObject: ...
    def to_dataframe(self) -> Table: ...

class KrigingResult:
    message: str
    target: KrigingResultTarget

def run(
    *,
    source: str,
    target: str,
    variogram: str,
    kriging_type: Literal['simple', 'ordinary'] = ...,
    max_samples: int = ...,
    preview: bool = True,
) -> KrigingResult:
    """Estimate a target attribute on a grid using kriging."""
    ...

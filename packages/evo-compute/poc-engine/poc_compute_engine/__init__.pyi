# AUTO-GENERATED OFFLINE FROM poc_compute_engine/schemas/ (a discovery snapshot). DO NOT EDIT BY HAND.
from typing import Any, Literal

from evo.common import IContext
from poc_compute_engine.discovery import DiscoveryClient as DiscoveryClient
from poc_compute_engine.engine import (
    GeoscienceObject as GeoscienceObject,
    Table as Table,
    TaskResult as TaskResult,
)

from poc_compute_engine.overrides.geostatistics.kriging_gcp import (
    KrigingGcpResult as KrigingGcpResult,
    KrigingGcpRunner as _GeostatisticsKrigingGcp,
)

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

class NormalScoreGcpResultTargetAttribute:
    reference: str
    name: str

class NormalScoreGcpResultTarget:
    reference: str
    name: str
    schema_id: str
    attribute: NormalScoreGcpResultTargetAttribute
    def get_object(self) -> GeoscienceObject: ...
    def to_dataframe(self) -> Table: ...

class NormalScoreGcpResult:
    message: str
    target: NormalScoreGcpResultTarget

class _GeostatisticsDeclustering:
    async def run(
        self,
        *,
        source: str,
        target: str,
        cell_size: float = ...,
        preview: bool = False,
    ) -> DeclusteringResult:
        """Compute declustering weights for a sample attribute."""
        ...

class _GeostatisticsNormalScoreGcp:
    async def run(
        self,
        *,
        source: str,
        target: str,
        weights: str = ...,
        num_quantiles: int = ...,
        preview: bool = False,
    ) -> NormalScoreGcpResult:
        """Apply a normal-score transform to a sample attribute."""
        ...

class _GeostatisticsNamespace:
    declustering: _GeostatisticsDeclustering
    kriging_gcp: _GeostatisticsKrigingGcp
    normal_score_gcp: _GeostatisticsNormalScoreGcp

class ComputeClient:
    def __init__(self, context: IContext) -> None: ...
    @classmethod
    async def connect(cls, context: IContext) -> ComputeClient: ...
    async def aclose(self) -> None: ...
    async def __aenter__(self) -> ComputeClient: ...
    async def __aexit__(self, *exc: object) -> None: ...
    async def refresh(self) -> None: ...
    geostatistics: _GeostatisticsNamespace


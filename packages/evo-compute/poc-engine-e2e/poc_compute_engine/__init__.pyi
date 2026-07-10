# AUTO-GENERATED OFFLINE FROM poc_compute_engine/schemas/ (a discovery snapshot). DO NOT EDIT BY HAND.
from typing import Any, Literal, TypedDict

from evo.common import IContext
from poc_compute_engine.discovery import DiscoveryClient as DiscoveryClient
from poc_compute_engine.engine import (
    GeoscienceObject as GeoscienceObject,
    Table as Table,
    TaskResult as TaskResult,
)
from poc_compute_engine.resolver import (
    AttributeInput as AttributeInput,
    FileInput as FileInput,
    ObjectInput as ObjectInput,
    TargetAttrInput as TargetAttrInput,
)

from poc_compute_engine.overrides.geostatistics.kriging_gcp import (
    KrigingGcpResult as KrigingGcpResult,
    KrigingGcpRunner as _GeostatisticsKrigingGcp,
)

class DeclusteringSourceInput(TypedDict, total=False):
    object: ObjectInput

class DeclusteringGridInput(TypedDict, total=False):
    object: ObjectInput

class DeclusteringTargetInput(TypedDict, total=False):
    object: ObjectInput
    attribute: TargetAttrInput

class DeclusteringNeighborhoodEllipsoidEllipsoidRangesInput(TypedDict, total=False):
    major: float
    semi_major: float
    minor: float

class DeclusteringNeighborhoodEllipsoidRotationInput(TypedDict, total=False):
    dip_azimuth: float
    dip: float
    pitch: float

class DeclusteringNeighborhoodEllipsoidInput(TypedDict, total=False):
    ellipsoid_ranges: DeclusteringNeighborhoodEllipsoidEllipsoidRangesInput
    rotation: DeclusteringNeighborhoodEllipsoidRotationInput

class DeclusteringNeighborhoodInput(TypedDict, total=False):
    ellipsoid: DeclusteringNeighborhoodEllipsoidInput
    max_samples: int
    min_samples: int
    max_empty_octants: int
    max_samples_per_octant: int | None
    max_samples_per_drillhole: int | None
    max_empty_quadrants: int | None
    max_samples_per_quadrant: int | None
    max_drillholes_per_estimate: int | None

class NormalScoreGcpSourceInput(TypedDict, total=False):
    object: ObjectInput
    attribute: AttributeInput

class NormalScoreGcpTargetInput(TypedDict, total=False):
    object: ObjectInput
    attribute: TargetAttrInput

class DeclusteringResultTargetAttribute:
    reference: str
    name: str

class DeclusteringResultTarget:
    reference: str
    name: str
    description: str | None
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
    description: str | None
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
        source: DeclusteringSourceInput,
        grid: DeclusteringGridInput,
        target: DeclusteringTargetInput,
        neighborhood: DeclusteringNeighborhoodInput,
        power: float | None = ...,
        preview: bool = True,
    ) -> DeclusteringResult:
        """Computes grid-based declustering weights by measuring each sample's influence on evaluation locations. Supports both KNN (arithmetic mean) and IDW (inverse-distance weighted) modes via an optional power parameter."""
        ...

class _GeostatisticsNormalScoreGcp:
    async def run(
        self,
        *,
        method: Literal['forward', 'backward'],
        source: NormalScoreGcpSourceInput,
        distribution: ObjectInput,
        target: NormalScoreGcpTargetInput,
        preview: bool = True,
    ) -> NormalScoreGcpResult:
        """For more information, please read the <a href='/docs/guides/geostatistics-tasks/tasks/normal-score'>guide</a>"""
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


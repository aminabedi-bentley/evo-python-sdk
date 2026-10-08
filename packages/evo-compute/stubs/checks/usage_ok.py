#  Copyright © 2026 Bentley Systems, Incorporated
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#      http://www.apache.org/licenses/LICENSE-2.0
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.

"""Correct use of the generated stub. A type checker must report **zero** errors here.

Run ``pyright stubs/checks/usage_ok.py`` (or ``mypy``) from ``packages/evo-compute``, or
let ``tests/test_stubgen.py`` do it when the checker is installed.
"""

from evo.common import IContext
from evo.objects.typed import Attribute, BaseObject, PendingAttribute

from evo.compute import ComputeClient, SyncComputeClient
from evo.compute.tasks import NormalScoreGcp
from evo.compute.tasks.common import Ellipsoid, EllipsoidRanges, Rotation, SearchNeighborhood
from evo.compute.tasks.geostatistics import Declustering


def _model_neighborhood() -> SearchNeighborhood:
    return SearchNeighborhood(
        ellipsoid=Ellipsoid(
            ranges=EllipsoidRanges(major=200.0, semi_major=150.0, minor=100.0),
            rotation=Rotation(dip_azimuth=45.0, dip=10.0, pitch=5.0),
        ),
        max_samples=20,
        min_samples=4,
        max_empty_octants=None,
        max_samples_per_octant=None,
        max_empty_quadrants=None,
        max_samples_per_quadrant=None,
        max_samples_per_drillhole=None,
        max_drillholes_per_estimate=None,
    )


async def neighborhood_objects_async(context: IContext) -> None:
    client = ComputeClient(context)
    result = await client.geostatistics.declustering.run(
        source="https://example.com/objects/samples",
        grid="https://example.com/objects/grid",
        target={"object": "https://example.com/objects/target", "attribute": "weights"},
        neighborhood=_model_neighborhood(),
        power=2.5,
    )
    await result.target.attribute.to_dataframe()
    imported = await Declustering.arun(
        context,
        source="https://example.com/objects/samples",
        grid="https://example.com/objects/grid",
        target={"object": "https://example.com/objects/target", "attribute": "weights"},
        neighborhood=_model_neighborhood(),
        power=2.5,
    )
    await imported.target.attribute.to_dataframe()


def neighborhood_objects_blocking(context: IContext) -> None:
    client = SyncComputeClient(context)
    result = client.geostatistics.declustering.run(
        source="https://example.com/objects/samples",
        grid="https://example.com/objects/grid",
        target={"object": "https://example.com/objects/target", "attribute": "weights"},
        neighborhood=_model_neighborhood(),
        power=2.5,
    )
    result.target.attribute.to_dataframe()
    imported = Declustering.run(
        context,
        source="https://example.com/objects/samples",
        grid="https://example.com/objects/grid",
        target={"object": "https://example.com/objects/target", "attribute": "weights"},
        neighborhood=_model_neighborhood(),
        power=2.5,
    )
    imported.target.attribute.to_dataframe()


async def declustering(context: IContext) -> None:
    client = ComputeClient(context)
    result = await client.geostatistics.declustering.run(
        source={"object": "https://example.com/objects/samples"},
        grid={"object": "https://example.com/objects/grid"},
        target={
            "object": "https://example.com/objects/samples",
            "attribute": {"operation": "create", "name": "declustering_weight"},
        },
        neighborhood={
            "ellipsoid": {
                "ellipsoid_ranges": {"major": 100.0, "semi_major": 100.0, "minor": 50.0},
                "rotation": {"dip_azimuth": 0.0, "dip": 0.0, "pitch": 0.0},
            },
            "max_samples": 20,
        },
        power=2.0,
    )
    # The raw payload is still a dict, and the declared keys are typed attributes on top.
    print(result["message"], result.target.attribute.name)
    # What the result refers to is loadable, because the node carries an ``output`` annotation.
    await result.target.load()
    await result.target.attribute.to_dataframe()


async def normal_score(context: IContext) -> None:
    client = ComputeClient(context)
    await client.geostatistics.normal_score_gcp.run(
        method="forward",
        source={
            "object": "https://example.com/objects/samples",
            "attribute": "grade",
        },
        distribution="https://example.com/objects/distribution",
        target={
            "object": "https://example.com/objects/samples",
            "attribute": {"operation": "create", "name": "grade_ns"},
        },
    )


async def not_in_the_snapshot(context: IContext) -> None:
    """A task published after the snapshot still runs -- ``arun`` is the typed escape hatch."""
    client = ComputeClient(context)
    result: dict = await client.arun("geostatistics", "some-new-task", {"source": "..."})
    print(result)


async def typed_handles(context: IContext, pointset: BaseObject, weights: PendingAttribute) -> None:
    """Reference resolution takes the handles the typed tasks take, so the stub does too.

    A parameter that wraps a single object reference accepts the object; one shaped
    ``{object, attribute}`` accepts a typed attribute, which knows both; and a ``target:
    attribute`` slot accepts the name of the attribute to create.
    """
    client = ComputeClient(context)
    await client.geostatistics.declustering.run(
        source=pointset,
        grid=pointset,
        target=weights,
        neighborhood={
            "ellipsoid": {
                "ellipsoid_ranges": {"major": 100.0, "semi_major": 100.0, "minor": 50.0},
                "rotation": {"dip_azimuth": 0.0, "dip": 0.0, "pitch": 0.0},
            },
            "max_samples": 20,
        },
    )


async def typed_attribute_source(context: IContext, grade: Attribute, target: BaseObject) -> None:
    client = ComputeClient(context)
    await client.geostatistics.kriging_gcp.run(
        source=grade,
        target={"object": target, "attribute": "kriged_grade"},
        kriging_method={"type": "ordinary"},
        variogram="https://example.com/objects/variogram",
        neighborhood={
            "ellipsoid": {
                "ellipsoid_ranges": {"major": 100.0, "semi_major": 100.0, "minor": 50.0},
                "rotation": {"dip_azimuth": 0.0, "dip": 0.0, "pitch": 0.0},
            },
            "max_samples": 20,
        },
    )


def declustering_without_await(context: IContext) -> None:
    """The same call through the blocking client: no ``await``, all the way through.

    ``run`` returns the result itself rather than a coroutine, and the loaders on every node
    below it block too -- which is the whole difference between the two entry points.
    """
    client = SyncComputeClient(context)
    result = client.geostatistics.declustering.run(
        source={"object": "https://example.com/objects/samples"},
        grid={"object": "https://example.com/objects/grid"},
        target={
            "object": "https://example.com/objects/samples",
            "attribute": {"operation": "create", "name": "declustering_weight"},
        },
        neighborhood={
            "ellipsoid": {
                "ellipsoid_ranges": {"major": 100.0, "semi_major": 100.0, "minor": 50.0},
                "rotation": {"dip_azimuth": 0.0, "dip": 0.0, "pitch": 0.0},
            },
            "max_samples": 20,
        },
        power=2.0,
    )
    print(result["message"], result.target.attribute.name)
    result.target.load()
    result.target.attribute.to_dataframe()


def not_in_the_snapshot_without_await(context: IContext) -> None:
    """``SyncComputeClient.run`` is the blocking escape hatch, mirroring ``arun``."""
    client = SyncComputeClient(context)
    result: dict = client.run("geostatistics", "some-new-task", {"source": "..."})
    print(result)


async def imported_task_async(context: IContext) -> None:
    result = await NormalScoreGcp.arun(
        context,
        method="forward",
        source={"object": "https://example.com/objects/samples", "attribute": "grade"},
        distribution="https://example.com/objects/distribution",
        target={
            "object": "https://example.com/objects/samples",
            "attribute": {"operation": "create", "name": "grade_ns"},
        },
    )
    name: str = result.target.attribute.name
    print(name)
    await result.target.load()


def imported_task_blocking(context: IContext) -> None:
    result = NormalScoreGcp.run(
        context,
        {
            "method": "forward",
            "source": {"object": "https://example.com/objects/samples", "attribute": "grade"},
            "distribution": "https://example.com/objects/distribution",
            "target": {
                "object": "https://example.com/objects/samples",
                "attribute": {"operation": "create", "name": "grade_ns"},
            },
        },
        preview=False,
    )
    name: str = result.target.attribute.name
    print(name)
    result.target.load()

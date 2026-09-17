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
from evo.compute.tasks import SearchNeighborhood
from evo.compute.tasks.common import Ellipsoid, EllipsoidRanges
from evo.compute.tasks.geostatistics.kriging import KrigingMethod


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


async def a_topic_the_snapshot_has_never_seen(context: IContext) -> None:
    """Execution is live; hints are point-in-time. An unlisted name is untyped, not wrong.

    The catalogue is per-organization and moves between SDK releases, so the snapshot is
    never all of it. These calls reach the engine exactly as a snapshotted task does; the
    only thing missing is the parameter and result types.
    """
    client = ComputeClient(context)
    await client.geology.some_task.run(anything=1)
    await client.geostatistics.a_task_added_last_week.run(source="...")


def a_topic_the_snapshot_has_never_seen_without_await(context: IContext) -> None:
    client = SyncComputeClient(context)
    client.converter.obj_import.run(file="...")


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


async def typed_attribute_source(context: IContext, grade: Attribute, kriged: PendingAttribute) -> None:
    """``kriging`` has an override, so the surface here is the runner's own, not the schema's.

    That is the point of one: the arguments are the SDK's (``search``, ``method``) and they
    take the typed models and handles rather than the wire shapes.
    """
    client = ComputeClient(context)
    await client.geostatistics.kriging.run(
        source=grade,
        target=kriged,
        variogram="https://example.com/objects/variogram",
        search=SearchNeighborhood(
            ellipsoid=Ellipsoid(ranges=EllipsoidRanges(major=100.0, semi_major=100.0, minor=50.0)),
            max_samples=20,
        ),
        method=KrigingMethod.ORDINARY,
    )


def overridden_task_without_await(context: IContext, grade: Attribute, kriged: PendingAttribute) -> None:
    """An override reached through the blocking client keeps its own arguments and result.

    Only the call is unwrapped: ``run`` hands back the runner's ``KrigingResult`` directly
    rather than a coroutine. That result is the hand-written one, so its own members are
    still awaited -- ``run_sync`` is there for those.
    """
    client = SyncComputeClient(context)
    result = client.geostatistics.kriging.run(
        source=grade,
        target=kriged,
        variogram="https://example.com/objects/variogram",
        search=SearchNeighborhood(
            ellipsoid=Ellipsoid(ranges=EllipsoidRanges(major=100.0, semi_major=100.0, minor=50.0)),
            max_samples=20,
        ),
        method=KrigingMethod.ORDINARY,
    )
    print(result.target_name, result.attribute_name)


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

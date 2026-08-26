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

from evo.compute import ComputeClient


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
    # The result is a TypedDict, so keys are checked and completed.
    print(result["message"], result["target"]["attribute"]["name"])


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

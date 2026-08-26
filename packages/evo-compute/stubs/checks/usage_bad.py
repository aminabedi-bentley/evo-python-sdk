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

"""Incorrect use of the generated stub. Every call below must be rejected statically.

``tests/test_stubgen.py`` asserts that the checker's report mentions each name listed in
``EXPECTED_ERRORS``, which is what makes this file a regression test for the stub rather
than a pile of broken code.
"""

from evo.common import IContext

from evo.compute import ComputeClient

EXPECTED_ERRORS = [
    "geology",  # topic that is not in the catalogue snapshot
    "made_up_task",  # task that is not in the catalogue snapshot
    "neighborhood",  # required parameter omitted
    "sourse",  # misspelled parameter
    "power",  # wrong scalar type
    "method",  # value outside the schema's enum
    "nope",  # key that the result schema does not define
]


async def unknown_topic(context: IContext) -> None:
    client = ComputeClient(context)
    await client.geology.declustering.run()


async def unknown_task(context: IContext) -> None:
    client = ComputeClient(context)
    await client.geostatistics.made_up_task.run()


async def missing_required_parameter(context: IContext) -> None:
    client = ComputeClient(context)
    await client.geostatistics.declustering.run(
        source={"object": "https://example.com/objects/samples"},
        grid={"object": "https://example.com/objects/grid"},
        target={
            "object": "https://example.com/objects/samples",
            "attribute": {"operation": "create", "name": "declustering_weight"},
        },
    )


async def misspelled_parameter(context: IContext) -> None:
    client = ComputeClient(context)
    await client.geostatistics.declustering.run(
        sourse={"object": "https://example.com/objects/samples"},
        grid={"object": "https://example.com/objects/grid"},
        target={
            "object": "https://example.com/objects/samples",
            "attribute": {"operation": "create", "name": "declustering_weight"},
        },
        neighborhood={
            "ellipsoid": {
                "ellipsoid_ranges": {"major": 100.0, "semi_major": 100.0, "minor": 50.0},
                "rotation": {},
            },
            "max_samples": 20,
        },
    )


async def wrong_scalar_type(context: IContext) -> None:
    client = ComputeClient(context)
    await client.geostatistics.declustering.run(
        source={"object": "https://example.com/objects/samples"},
        grid={"object": "https://example.com/objects/grid"},
        target={
            "object": "https://example.com/objects/samples",
            "attribute": {"operation": "create", "name": "declustering_weight"},
        },
        neighborhood={
            "ellipsoid": {
                "ellipsoid_ranges": {"major": 100.0, "semi_major": 100.0, "minor": 50.0},
                "rotation": {},
            },
            "max_samples": 20,
        },
        power="strong",
    )


async def value_outside_the_enum(context: IContext) -> None:
    client = ComputeClient(context)
    await client.geostatistics.normal_score_gcp.run(
        method="sideways",
        source={"object": "https://example.com/objects/samples", "attribute": "grade"},
        distribution="https://example.com/objects/distribution",
        target={
            "object": "https://example.com/objects/samples",
            "attribute": {"operation": "create", "name": "grade_ns"},
        },
    )


async def unknown_result_key(context: IContext) -> None:
    client = ComputeClient(context)
    result = await client.geostatistics.normal_score_gcp.run(
        method="forward",
        source={"object": "https://example.com/objects/samples", "attribute": "grade"},
        distribution="https://example.com/objects/distribution",
        target={
            "object": "https://example.com/objects/samples",
            "attribute": {"operation": "create", "name": "grade_ns"},
        },
    )
    print(result["nope"])

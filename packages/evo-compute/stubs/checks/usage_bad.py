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

from evo.compute import ComputeClient, SyncComputeClient

EXPECTED_ERRORS = [
    "neighborhood",  # required parameter omitted
    "sourse",  # misspelled parameter
    "power",  # wrong scalar type
    "method",  # value outside the schema's enum
    "upper_case",  # result attribute used as something other than the type it declares
    "await",  # blocking client's result awaited as though it were the async one's
    "declustering_weight",  # submitted job read as though it were the result
    "title_case",  # a submitted job's result attribute used as something other than its type
    "swap_case",  # the same through the blocking client's job
]


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
    await client.geostatistics.normal_score.run(
        method="sideways",
        source={"object": "https://example.com/objects/samples", "attribute": "grade"},
        distribution="https://example.com/objects/distribution",
        target={
            "object": "https://example.com/objects/samples",
            "attribute": {"operation": "create", "name": "grade_ns"},
        },
    )


async def misused_result_attribute(context: IContext) -> None:
    """A result node inherits ``__getattr__``, so an unknown key cannot be caught.

    What the generated types do guarantee is that the keys the schema *does* declare carry
    their real types all the way down, which is what this checks.
    """
    client = ComputeClient(context)
    result = await client.geostatistics.normal_score.run(
        method="forward",
        source={"object": "https://example.com/objects/samples", "attribute": "grade"},
        distribution="https://example.com/objects/distribution",
        target={
            "object": "https://example.com/objects/samples",
            "attribute": {"operation": "create", "name": "grade_ns"},
        },
    )
    print(result.target.attribute.name.upper_case())


async def awaited_blocking_result(context: IContext) -> None:
    """The blocking client has already done the waiting; there is nothing left to await."""
    client = SyncComputeClient(context)
    result = client.geostatistics.normal_score.run(
        method="forward",
        source={"object": "https://example.com/objects/samples", "attribute": "grade"},
        distribution="https://example.com/objects/distribution",
        target={
            "object": "https://example.com/objects/samples",
            "attribute": {"operation": "create", "name": "grade_ns"},
        },
    )
    await result.target.load()


async def submitted_job_read_as_a_result(context: IContext) -> None:
    """``submit`` hands back the job, not what it will produce.

    A result node carries ``__getattr__``, so any name reads off it; a job deliberately does
    not, which is what keeps the two from being confused for one another.
    """
    client = ComputeClient(context)
    job = await client.geostatistics.declustering.submit(
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
        power=2.0,
    )
    print(job.declustering_weight)


async def misused_result_of_a_submitted_job(context: IContext) -> None:
    """Waiting on the job yields the same typed result ``run`` returns, so the same mistake is caught."""
    client = ComputeClient(context)
    job = await client.geostatistics.normal_score.submit(
        method="forward",
        source={"object": "https://example.com/objects/samples", "attribute": "grade"},
        distribution="https://example.com/objects/distribution",
        target={
            "object": "https://example.com/objects/samples",
            "attribute": {"operation": "create", "name": "grade_ns"},
        },
    )
    result = await job.results()
    print(result.target.attribute.name.title_case())


def misused_result_of_a_blocking_submitted_job(context: IContext) -> None:
    client = SyncComputeClient(context)
    job = client.geostatistics.normal_score.submit(
        method="forward",
        source={"object": "https://example.com/objects/samples", "attribute": "grade"},
        distribution="https://example.com/objects/distribution",
        target={
            "object": "https://example.com/objects/samples",
            "attribute": {"operation": "create", "name": "grade_ns"},
        },
    )
    print(job.results().target.attribute.name.swap_case())

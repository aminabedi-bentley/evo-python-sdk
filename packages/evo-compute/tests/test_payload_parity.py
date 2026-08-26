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

"""Payload parity between the hand-written task runners and the generic engine (GSTAT-287).

Every case describes one job twice: once through the typed runner and once through the
discovery-driven engine, then asserts that ``JobClient.submit`` was handed the same
``parameters`` payload from both -- equal in value *and* in type, so that a ``1`` never
passes for a ``1.0``.

The engine side is written out as plain JSON literals on purpose. Dumping the runner's
model to build the engine call would make every assertion tautological; spelling the
payload out instead pins down exactly what a generic-engine caller has to send, and fails
loudly if a runner alias or serializer ever changes shape.

Discovery is mocked rather than recorded, so the suite needs no catalogue fixture and no
credentials. Each task's schema is taken from the runner's own parameter model, which is
all the engine reads to bind a call: the wire field names and which of them are required.
That makes this a test of the two code paths, not of the platform contract -- checking the
SDK models against live discovery is schema conformance, and is tested separately.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from unittest import IsolatedAsyncioTestCase, mock

from evo.common.test_tools import ORG as TEST_ORG
from evo.objects import ObjectReference
from evo.objects.typed import Attribute, PendingAttribute
from evo.objects.typed.base import BaseObject

from evo.compute import ComputeClient, DiscoveryClient, ParameterValidationError, TaskResource
from evo.compute.tasks import CreateAttribute, SearchNeighborhood, Source, Target
from evo.compute.tasks.common import Ellipsoid, EllipsoidRanges, Filter, FilterCondition, Rotation
from evo.compute.tasks.geostatistics.conditioned_simulator import ConSimParameters, ConSimRunner
from evo.compute.tasks.geostatistics.declustering import DeclusteringRunner, idw, knn
from evo.compute.tasks.geostatistics.kriging import (
    BlockDiscretisation,
    KrigingMethod,
    KrigingParameters,
    KrigingRunner,
)
from evo.compute.tasks.geostatistics.location_wise import (
    LocationWiseParameters,
    LocationWiseRunner,
    LocationWiseTarget,
    MeanAboveCutoff,
    ProbabilityAboveCutoff,
)

# --------------------------------------------------------------------------- #
# Shared inputs
# --------------------------------------------------------------------------- #


def _obj_url(suffix: str) -> str:
    """A structurally valid geoscience object URL, which the parameter models insist on."""
    return (
        "https://hub.test.evo.bentley.com/geoscience-object"
        "/orgs/00000000-0000-0000-0000-000000000001"
        "/workspaces/00000000-0000-0000-0000-000000000002"
        f"/objects/00000000-0000-0000-0000-0000000000{suffix}"
    )


POINTSET_URL = _obj_url("10")
GRID_URL = _obj_url("20")
TARGET_URL = _obj_url("30")
VARIOGRAM_URL = _obj_url("40")

GRADE_ATTRIBUTE = "locations.attributes[?name=='grade']"
SIMULATIONS_ATTRIBUTE = "locations.attributes[?name=='simulations']"


def _typed_object(url: str) -> mock.MagicMock:
    """Stands in for a typed geoscience object, which the models resolve down to its URL."""
    obj = mock.MagicMock(spec=BaseObject)
    obj.metadata.url = ObjectReference(url)
    return obj


def _existing_attribute(key: str, url: str, schema_path: str = "") -> mock.MagicMock:
    """Stands in for an attribute already on an object, which resolves to a key-based expression."""
    attribute = mock.MagicMock(spec=Attribute)
    attribute.key = key
    attribute.exists = True
    attribute._context = mock.MagicMock(schema_path=schema_path)
    attribute._obj = _typed_object(url)
    return attribute


def _pending_attribute(name: str, url: str) -> PendingAttribute:
    """An attribute that does not exist yet, which resolves to a create operation."""
    parent = mock.MagicMock()
    parent._obj = _typed_object(url)
    return PendingAttribute(parent, name)


def _search(min_samples: int | None = 4) -> SearchNeighborhood:
    return SearchNeighborhood(
        ellipsoid=Ellipsoid(
            ranges=EllipsoidRanges(major=200.0, semi_major=150.0, minor=100.0),
            rotation=Rotation(dip_azimuth=45.0, dip=10.0, pitch=5.0),
        ),
        max_samples=20,
        min_samples=min_samples,
    )


_ELLIPSOID_JSON: dict[str, Any] = {
    "ellipsoid_ranges": {"major": 200.0, "semi_major": 150.0, "minor": 100.0},
    "rotation": {"dip_azimuth": 45.0, "dip": 10.0, "pitch": 5.0},
}

SEARCH_JSON: dict[str, Any] = {"ellipsoid": _ELLIPSOID_JSON, "max_samples": 20, "min_samples": 4}

SEARCH_JSON_WITHOUT_MIN_SAMPLES: dict[str, Any] = {"ellipsoid": _ELLIPSOID_JSON, "max_samples": 20}


# --------------------------------------------------------------------------- #
# Type-strict comparison
# --------------------------------------------------------------------------- #


def payload_differences(expected: Any, actual: Any, path: str = "parameters") -> list[str]:
    """Compare two payloads by value *and* concrete type, reporting every divergence.

    ``==`` alone is too forgiving here: ``1 == 1.0`` and ``True == 1``, yet the platform
    reads those as different JSON types.
    """
    if type(expected) is not type(actual):
        return [f"{path}: expected {type(expected).__name__} {expected!r}, got {type(actual).__name__} {actual!r}"]

    if isinstance(expected, dict):
        differences = []
        for key in sorted(set(expected) - set(actual)):
            differences.append(f"{path}.{key}: missing, expected {expected[key]!r}")
        for key in sorted(set(actual) - set(expected)):
            differences.append(f"{path}.{key}: unexpected, got {actual[key]!r}")
        for key in expected.keys() & actual.keys():
            differences += payload_differences(expected[key], actual[key], f"{path}.{key}")
        return differences

    if isinstance(expected, list):
        if len(expected) != len(actual):
            return [f"{path}: expected {len(expected)} item(s), got {len(actual)}"]
        differences = []
        for index, (left, right) in enumerate(zip(expected, actual)):
            differences += payload_differences(left, right, f"{path}[{index}]")
        return differences

    return [] if expected == actual else [f"{path}: expected {expected!r}, got {actual!r}"]


# --------------------------------------------------------------------------- #
# Harness
# --------------------------------------------------------------------------- #


class FakeContext:
    """Minimal duck-typed IContext exposing just what the runners and the engine use."""

    def __init__(self) -> None:
        self._connector = mock.Mock()
        self._org_id = TEST_ORG.id

    def get_connector(self):
        return self._connector

    def get_org_id(self):
        return self._org_id


class _SubmitCaptured(Exception):
    """Raised in place of a real submission, once the payload has been recorded."""


@contextmanager
def _capture_submit(module: str) -> Iterator[mock.AsyncMock]:
    """Patch ``JobClient`` in ``module`` so submission stops as soon as the payload is known."""
    submit = mock.AsyncMock(side_effect=_SubmitCaptured)
    with mock.patch(f"{module}.JobClient") as job_client:
        job_client.submit = submit
        yield submit


def task_spec(runner_cls) -> TaskResource:
    """The discovery spec the engine would fetch for ``runner_cls``, built from its own model.

    Fields the runner folds into another field are ``exclude=True``: inputs to the model that
    never reach the wire. The live catalogue does not advertise them, so neither does this
    spec, even though they do appear in the model's validation schema.
    """
    model = runner_cls.params_type
    schema = model.model_json_schema(by_alias=True, mode="validation")
    folded = {field.alias or name for name, field in model.model_fields.items() if field.exclude}
    schema["properties"] = {name: prop for name, prop in schema.get("properties", {}).items() if name not in folded}
    schema["required"] = [name for name in schema.get("required", []) if name not in folded]
    return TaskResource(topic=runner_cls.topic, name=runner_cls.task, parameters=schema)


class PayloadParityTestCase(IsolatedAsyncioTestCase):
    """Runs one job through both paths and compares what each hands to ``JobClient.submit``."""

    def setUp(self) -> None:
        super().setUp()
        self.context = FakeContext()
        self.client = ComputeClient(self.context)

    async def runner_payload(self, runner_cls, params) -> dict[str, Any]:
        """The payload the hand-written runner submits for ``params``."""
        with _capture_submit("evo.compute.tasks.common.runner") as submit:
            with self.assertRaises(_SubmitCaptured):
                await runner_cls(self.context, params)
        return submit.await_args.kwargs["parameters"]

    async def engine_payload(self, runner_cls, **parameters: Any) -> dict[str, Any]:
        """The payload the generic engine submits for the same job, described as plain JSON."""
        catalogue = mock.patch.object(
            DiscoveryClient, "get_topic_tasks", mock.AsyncMock(return_value=[task_spec(runner_cls)])
        )
        with catalogue, _capture_submit("evo.compute.engine") as submit:
            with self.assertRaises(_SubmitCaptured):
                await getattr(self.client.geostatistics, runner_cls.task.replace("-", "_")).run(**parameters)
        return submit.await_args.kwargs["parameters"]

    def assertPayloadParity(self, runner_payload: dict[str, Any], engine_payload: dict[str, Any]) -> None:
        differences = payload_differences(runner_payload, engine_payload)
        if differences:
            self.fail("engine payload diverges from the runner payload:\n  " + "\n  ".join(differences))


# --------------------------------------------------------------------------- #
# Declustering
# --------------------------------------------------------------------------- #


class TestDeclusteringPayloadParity(PayloadParityTestCase):
    TARGET_JSON = {"object": TARGET_URL, "attribute": {"operation": "create", "name": "weights"}}

    def _target(self) -> Target:
        return Target(object=TARGET_URL, attribute=CreateAttribute(name="weights"))

    async def test_idw_declustering(self) -> None:
        """Inverse-distance weighting, with an explicit power."""
        runner_payload = await self.runner_payload(
            DeclusteringRunner,
            idw(source=POINTSET_URL, grid=GRID_URL, target=self._target(), neighborhood=_search(), power=2.5),
        )
        engine_payload = await self.engine_payload(
            DeclusteringRunner,
            source={"object": POINTSET_URL},
            grid={"object": GRID_URL},
            target=self.TARGET_JSON,
            neighborhood=SEARCH_JSON,
            power=2.5,
        )
        self.assertPayloadParity(runner_payload, engine_payload)

    async def test_idw_declustering_with_the_default_power(self) -> None:
        """The runner materialises its own ``power`` default, so the engine has to send it too."""
        runner_payload = await self.runner_payload(
            DeclusteringRunner,
            idw(source=POINTSET_URL, grid=GRID_URL, target=self._target(), neighborhood=_search()),
        )
        self.assertEqual(2.0, runner_payload["power"])
        engine_payload = await self.engine_payload(
            DeclusteringRunner,
            source={"object": POINTSET_URL},
            grid={"object": GRID_URL},
            target=self.TARGET_JSON,
            neighborhood=SEARCH_JSON,
            power=2.0,
        )
        self.assertPayloadParity(runner_payload, engine_payload)

    async def test_knn_declustering_sends_a_null_power(self) -> None:
        """KNN mode is a literal ``null`` power, not an omitted one; the engine must forward it."""
        runner_payload = await self.runner_payload(
            DeclusteringRunner,
            knn(source=POINTSET_URL, grid=GRID_URL, target=self._target(), neighborhood=_search()),
        )
        self.assertIn("power", runner_payload)
        self.assertIsNone(runner_payload["power"])
        engine_payload = await self.engine_payload(
            DeclusteringRunner,
            source={"object": POINTSET_URL},
            grid={"object": GRID_URL},
            target=self.TARGET_JSON,
            neighborhood=SEARCH_JSON,
            power=None,
        )
        self.assertPayloadParity(runner_payload, engine_payload)

    async def test_integer_power_is_not_accepted_as_the_float_the_runner_sends(self) -> None:
        """Guards the comparison itself: ``2`` must not pass for the runner's ``2.0``."""
        runner_payload = await self.runner_payload(
            DeclusteringRunner,
            idw(source=POINTSET_URL, grid=GRID_URL, target=self._target(), neighborhood=_search(), power=2.0),
        )
        engine_payload = await self.engine_payload(
            DeclusteringRunner,
            source={"object": POINTSET_URL},
            grid={"object": GRID_URL},
            target=self.TARGET_JSON,
            neighborhood=SEARCH_JSON,
            power=2,
        )
        self.assertEqual(
            ["parameters.power: expected float 2.0, got int 2"],
            payload_differences(runner_payload, engine_payload),
        )

    async def test_typed_objects_are_resolved_to_urls(self) -> None:
        """Notebook callers pass objects, not URLs; the engine caller has to pass the resolved URL."""
        runner_payload = await self.runner_payload(
            DeclusteringRunner,
            idw(
                source=_typed_object(POINTSET_URL),
                grid=_typed_object(GRID_URL),
                target=_pending_attribute("weights", TARGET_URL),
                neighborhood=_search(),
            ),
        )
        engine_payload = await self.engine_payload(
            DeclusteringRunner,
            source={"object": POINTSET_URL},
            grid={"object": GRID_URL},
            target=self.TARGET_JSON,
            neighborhood=SEARCH_JSON,
            power=2.0,
        )
        self.assertPayloadParity(runner_payload, engine_payload)

    async def test_search_without_min_samples_omits_the_key(self) -> None:
        """An unset ``min_samples`` is dropped by the neighborhood serializer, not sent as null."""
        runner_payload = await self.runner_payload(
            DeclusteringRunner,
            idw(
                source=POINTSET_URL,
                grid=GRID_URL,
                target=self._target(),
                neighborhood=_search(min_samples=None),
            ),
        )
        self.assertNotIn("min_samples", runner_payload["neighborhood"])
        engine_payload = await self.engine_payload(
            DeclusteringRunner,
            source={"object": POINTSET_URL},
            grid={"object": GRID_URL},
            target=self.TARGET_JSON,
            neighborhood=SEARCH_JSON_WITHOUT_MIN_SAMPLES,
            power=2.0,
        )
        self.assertPayloadParity(runner_payload, engine_payload)


# --------------------------------------------------------------------------- #
# Kriging
# --------------------------------------------------------------------------- #


class TestKrigingPayloadParity(PayloadParityTestCase):
    SOURCE_JSON = {"object": POINTSET_URL, "attribute": GRADE_ATTRIBUTE}
    TARGET_JSON = {"object": TARGET_URL, "attribute": {"operation": "create", "name": "kriged_grade"}}

    def _params(self, **overrides: Any) -> KrigingParameters:
        arguments: dict[str, Any] = dict(
            source=Source(object=POINTSET_URL, attribute=GRADE_ATTRIBUTE),
            target=Target(object=TARGET_URL, attribute=CreateAttribute(name="kriged_grade")),
            variogram=VARIOGRAM_URL,
            search=_search(),
        )
        arguments.update(overrides)
        return KrigingParameters(**arguments)

    async def test_ordinary_kriging(self) -> None:
        """The default method is materialised by the runner as a tagged object."""
        runner_payload = await self.runner_payload(KrigingRunner, self._params())
        engine_payload = await self.engine_payload(
            KrigingRunner,
            source=self.SOURCE_JSON,
            target=self.TARGET_JSON,
            variogram=VARIOGRAM_URL,
            neighborhood=SEARCH_JSON,
            kriging_method={"type": "ordinary"},
        )
        self.assertPayloadParity(runner_payload, engine_payload)

    async def test_simple_kriging_with_block_discretisation(self) -> None:
        """``method``/``search`` reach the wire under their ``kriging_method``/``neighborhood`` aliases."""
        runner_payload = await self.runner_payload(
            KrigingRunner,
            self._params(
                method=KrigingMethod.simple(mean=12.5),
                block_discretisation=BlockDiscretisation(nx=3, ny=3, nz=2),
            ),
        )
        engine_payload = await self.engine_payload(
            KrigingRunner,
            source=self.SOURCE_JSON,
            target=self.TARGET_JSON,
            variogram=VARIOGRAM_URL,
            neighborhood=SEARCH_JSON,
            kriging_method={"type": "simple", "mean": 12.5},
            block_discretisation={"nx": 3, "ny": 3, "nz": 2},
        )
        self.assertPayloadParity(runner_payload, engine_payload)

    async def test_typed_attributes_become_urls_and_jmespath_expressions(self) -> None:
        """An attribute that already exists resolves to a key lookup, and to an *update* target."""
        runner_payload = await self.runner_payload(
            KrigingRunner,
            self._params(
                source=_existing_attribute("src-key-1", POINTSET_URL, schema_path="locations.attributes"),
                target=_existing_attribute("tgt-key-9", TARGET_URL),
            ),
        )
        engine_payload = await self.engine_payload(
            KrigingRunner,
            source={"object": POINTSET_URL, "attribute": "locations.attributes[?key=='src-key-1']"},
            target={
                "object": TARGET_URL,
                "attribute": {"operation": "update", "reference": "attributes[?key=='tgt-key-9']"},
            },
            variogram=VARIOGRAM_URL,
            neighborhood=SEARCH_JSON,
            kriging_method={"type": "ordinary"},
        )
        self.assertPayloadParity(runner_payload, engine_payload)

    async def test_filters_are_folded_into_source_and_target(self) -> None:
        """``source_filter``/``target_filter`` are not wire fields; they nest under the object they filter."""
        runner_payload = await self.runner_payload(
            KrigingRunner,
            self._params(
                source_filter=Filter(
                    where=FilterCondition(attribute=GRADE_ATTRIBUTE, operator="greater_than", threshold=0.5)
                ),
                target_filter=Filter(
                    where=FilterCondition(
                        attribute="attributes[?name=='domain']", operator="in", values=["LMS1", "LMS2"]
                    )
                ),
            ),
        )
        self.assertNotIn("source_filter", runner_payload)
        engine_payload = await self.engine_payload(
            KrigingRunner,
            source={
                **self.SOURCE_JSON,
                "filter": {
                    "where": {
                        "type": "condition",
                        "attribute": GRADE_ATTRIBUTE,
                        "operator": "greater_than",
                        "values": None,
                        "threshold": 0.5,
                    }
                },
            },
            target={
                **self.TARGET_JSON,
                "filter": {
                    "where": {
                        "type": "condition",
                        "attribute": "attributes[?name=='domain']",
                        "operator": "in",
                        "values": ["LMS1", "LMS2"],
                        "threshold": None,
                    }
                },
            },
            variogram=VARIOGRAM_URL,
            neighborhood=SEARCH_JSON,
            kriging_method={"type": "ordinary"},
        )
        self.assertPayloadParity(runner_payload, engine_payload)

    async def test_the_folded_filter_fields_are_not_offered_as_engine_parameters(self) -> None:
        """Folding a filter is runner-side work, so ``source_filter`` is not a task parameter."""
        self.assertNotIn("source_filter", task_spec(KrigingRunner).parameters["properties"])
        with self.assertRaises(ParameterValidationError) as caught:
            await self.engine_payload(
                KrigingRunner,
                source=self.SOURCE_JSON,
                target=self.TARGET_JSON,
                variogram=VARIOGRAM_URL,
                neighborhood=SEARCH_JSON,
                source_filter={"where": {"type": "condition", "attribute": GRADE_ATTRIBUTE, "operator": "in"}},
            )
        self.assertIn("source_filter", str(caught.exception))


# --------------------------------------------------------------------------- #
# Conditional simulation (consim)
# --------------------------------------------------------------------------- #


class TestConSimPayloadParity(PayloadParityTestCase):
    #: The runner fills these in from its own field defaults, so a parity payload must carry them.
    RUNNER_DEFAULTS: dict[str, Any] = {
        "block_discretization": {"nx": 1, "ny": 1, "nz": 1},
        "kriging_method": "simple",
        "number_of_lines": 500,
        "number_of_simulations": 1,
        "random_seed": 38239342,
        "number_of_simulations_to_save": 5,
        "perform_validation": False,
    }

    def _params(self, **overrides: Any) -> ConSimParameters:
        arguments: dict[str, Any] = dict(
            source_object=POINTSET_URL,
            source_attribute=GRADE_ATTRIBUTE,
            target_object=GRID_URL,
            neighborhood=_search(),
            variogram_model=VARIOGRAM_URL,
        )
        arguments.update(overrides)
        return ConSimParameters(**arguments)

    async def test_minimal_conditional_simulation(self) -> None:
        """Every client-side default the runner applies has to be sent explicitly by the engine."""
        runner_payload = await self.runner_payload(ConSimRunner, self._params())
        engine_payload = await self.engine_payload(
            ConSimRunner,
            source_object=POINTSET_URL,
            source_attribute=GRADE_ATTRIBUTE,
            target_object=GRID_URL,
            neighborhood=SEARCH_JSON,
            variogram_model=VARIOGRAM_URL,
            **self.RUNNER_DEFAULTS,
        )
        self.assertPayloadParity(runner_payload, engine_payload)

    async def test_multiple_simulations_with_quantiles(self) -> None:
        """Counts stay ints and cutoffs stay floats across both paths."""
        runner_payload = await self.runner_payload(
            ConSimRunner,
            self._params(
                number_of_simulations=25,
                number_of_simulations_to_save=3,
                random_seed=7,
                location_wise_quantiles=[0.1, 0.5, 0.9],
                probability_above_cutoff=[1.0, 2.5],
            ),
        )
        engine_payload = await self.engine_payload(
            ConSimRunner,
            source_object=POINTSET_URL,
            source_attribute=GRADE_ATTRIBUTE,
            target_object=GRID_URL,
            neighborhood=SEARCH_JSON,
            variogram_model=VARIOGRAM_URL,
            **{
                **self.RUNNER_DEFAULTS,
                "number_of_simulations": 25,
                "number_of_simulations_to_save": 3,
                "random_seed": 7,
            },
            location_wise_quantiles=[0.1, 0.5, 0.9],
            probability_above_cutoff=[1.0, 2.5],
        )
        self.assertPayloadParity(runner_payload, engine_payload)


# --------------------------------------------------------------------------- #
# Location-wise
# --------------------------------------------------------------------------- #


class TestLocationWisePayloadParity(PayloadParityTestCase):
    SOURCE_JSON = {"object": POINTSET_URL, "attribute": SIMULATIONS_ATTRIBUTE}
    TARGET_JSON = {"object": GRID_URL}

    def _params(self, **overrides: Any) -> LocationWiseParameters:
        arguments: dict[str, Any] = dict(
            source=Source(object=POINTSET_URL, attribute=SIMULATIONS_ATTRIBUTE),
            target=LocationWiseTarget(object=GRID_URL),
        )
        arguments.update(overrides)
        return LocationWiseParameters(**arguments)

    async def test_summary_only(self) -> None:
        """``summary=True`` is the minimal job; the unset statistics blocks stay off the wire."""
        runner_payload = await self.runner_payload(LocationWiseRunner, self._params(summary=True))
        engine_payload = await self.engine_payload(
            LocationWiseRunner,
            source=self.SOURCE_JSON,
            target=self.TARGET_JSON,
            summary=True,
        )
        self.assertPayloadParity(runner_payload, engine_payload)

    async def test_all_statistics(self) -> None:
        """Quantiles and cutoff lists keep their float element types."""
        runner_payload = await self.runner_payload(
            LocationWiseRunner,
            self._params(
                summary=True,
                quantiles=[0.1, 0.5, 0.9],
                probability_above_cutoff=ProbabilityAboveCutoff(cutoffs=[1.0, 2.0]),
                mean_above_cutoff=MeanAboveCutoff(cutoffs=[1.5]),
            ),
        )
        engine_payload = await self.engine_payload(
            LocationWiseRunner,
            source=self.SOURCE_JSON,
            target=self.TARGET_JSON,
            summary=True,
            quantiles=[0.1, 0.5, 0.9],
            probability_above_cutoff={"cutoffs": [1.0, 2.0]},
            mean_above_cutoff={"cutoffs": [1.5]},
        )
        self.assertPayloadParity(runner_payload, engine_payload)

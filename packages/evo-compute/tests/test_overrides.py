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

"""The override seam, and the kriging runner that uses it (GSTAT-235).

Three things are worth holding an override to, and they are the three sections below.

It has to be reached. The module path is the only registration, so what matters is that
``client.geostatistics.kriging`` is the hand-written runner while every other task --
and the same task through ``arun`` -- is still generic.

It has to earn its place. An override exists to do what the generic path cannot, so each
addition is pinned by a test that fails if the check or the typed result were dropped.

And it has to send the same job. That is the part an override can quietly get wrong, so the
last section runs one set of inputs through all three paths -- the hand-written task runner,
the override, and the generic engine -- and requires ``JobClient.submit`` to have been
handed the same payload by each, equal in value and in concrete type. The
:mod:`test_payload_parity` harness already makes exactly that comparison for the runner and
the engine; this file adds the override as a third path through the same machinery rather
than restating it.
"""

from __future__ import annotations

import inspect
from collections.abc import Iterator
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any
from unittest import mock

from evo.compute import ParameterValidationError, SyncComputeClient, TaskResource
from evo.compute.discovery import DiscoveryClient
from evo.compute.engine import _BlockingRunner, _TaskProxy
from evo.compute.overrides import load_override
from evo.compute.overrides.geostatistics.kriging import KrigingOverride
from evo.compute.tasks import CreateAttribute, Source, Target, UpdateAttribute
from evo.compute.tasks.common import Filter, FilterCondition
from evo.compute.tasks.geostatistics.kriging import KrigingMethod, KrigingResult, KrigingRunner
from test_payload_parity import (
    GRADE_ATTRIBUTE,
    POINTSET_URL,
    TARGET_URL,
    VARIOGRAM_URL,
    PayloadParityTestCase,
    _capture_submit,
    _pending_attribute,
    _search,
    _SubmitCaptured,
    payload_differences,
    task_spec,
)

TASK_RESULT = {
    "message": "Kriging complete.",
    "target": {
        "reference": TARGET_URL,
        "name": "Block model",
        "schema_id": "/objects/block-model/1.2.0/block-model.schema.json",
        "attribute": {"reference": "attributes[?name=='kriged_grade']", "name": "kriged_grade"},
    },
}


def kriging_catalogue(**overrides: Any) -> list[TaskResource]:
    """The kriging schema as the catalogue advertises it.

    The runner, the override and the catalogue all name this task ``kriging``, so the
    comparison is about the payload rather than about which name each path happens to use.
    """
    return [task_spec(KrigingRunner).model_copy(update=overrides)]


class OverrideTestCase(PayloadParityTestCase):
    """A client whose discovery is mocked, and the inputs every case here starts from."""

    def setUp(self) -> None:
        super().setUp()
        self.catalogue = mock.patch.object(
            DiscoveryClient, "get_topic_tasks", mock.AsyncMock(return_value=kriging_catalogue())
        )

    def inputs(self, **overrides: Any) -> dict[str, Any]:
        """One set of inputs, named as the override names them."""
        inputs: dict[str, Any] = dict(
            source=Source(object=POINTSET_URL, attribute=GRADE_ATTRIBUTE),
            target=Target(object=TARGET_URL, attribute=CreateAttribute(name="kriged_grade")),
            variogram=VARIOGRAM_URL,
            search=_search(),
            method=KrigingMethod.ORDINARY,
        )
        inputs.update(overrides)
        return inputs

    @contextmanager
    def completed_job(self, results: dict) -> Iterator[mock.AsyncMock]:
        """Let a submission through and hand back ``results``, so the typed result gets built."""
        job = mock.Mock(wait_for_results=mock.AsyncMock(return_value=results))
        submit = mock.AsyncMock(return_value=job)
        with self.catalogue, mock.patch("evo.compute.engine.JobClient") as job_client:
            job_client.submit = submit
            yield submit

    async def override_payload(self, **inputs: Any) -> dict[str, Any]:
        """The payload the override submits for ``inputs``."""
        with self.catalogue, _capture_submit("evo.compute.engine") as submit:
            with self.assertRaises(_SubmitCaptured):
                await self.client.geostatistics.kriging.run(**inputs)
        return submit.await_args.kwargs["parameters"]


# --------------------------------------------------------------------------- #
# The seam
# --------------------------------------------------------------------------- #


class TestTheSeam(OverrideTestCase):
    def test_an_overridden_task_is_served_by_its_own_runner(self) -> None:
        """The module at ``overrides/geostatistics/kriging.py`` claims the task."""
        self.assertIsInstance(self.client.geostatistics.kriging, KrigingOverride)

    def test_the_blocking_client_reaches_the_same_override(self) -> None:
        """``SyncComputeClient`` wraps the runner rather than getting a generic proxy."""
        runner = SyncComputeClient(self.context).geostatistics.kriging
        self.assertIsInstance(runner, _BlockingRunner)
        self.assertFalse(inspect.iscoroutinefunction(runner.run))
        # The runner's own members are still reachable through the wrapper.
        self.assertEqual("<compute task 'geostatistics'.'kriging' (override)>", repr(runner))

    def test_the_blocking_client_runs_the_override_without_await(self) -> None:
        with self.completed_job(TASK_RESULT) as submit:
            result = SyncComputeClient(self.context).geostatistics.kriging.run(**self.inputs())

        self.assertEqual("kriging", submit.await_args.kwargs["task"])
        self.assertIsInstance(result, KrigingResult)

    def test_the_blocking_client_surfaces_the_overrides_own_refusals(self) -> None:
        """The checks the runner adds are not bypassed by going through the bridge."""
        with self.catalogue:
            with self.assertRaises(ParameterValidationError) as caught:
                SyncComputeClient(self.context).geostatistics.kriging.run(**self.inputs(search=_search(min_samples=99)))
        self.assertIn("exceeds max_samples", str(caught.exception))

    def test_every_other_task_stays_generic(self) -> None:
        """An override is one task opting out, not a change to how tasks are reached."""
        self.assertIsInstance(self.client.geostatistics.declustering, _TaskProxy)
        self.assertIsInstance(self.client.geostatistics.normal_score, _TaskProxy)
        self.assertIsInstance(self.client.converter.obj_import, _TaskProxy)

    def test_reaching_a_task_still_costs_no_discovery(self) -> None:
        """``bind`` is handed the client, not a spec, so the seam keeps attribute access free."""
        get_topic_tasks = mock.AsyncMock(return_value=kriging_catalogue())
        with mock.patch.object(DiscoveryClient, "get_topic_tasks", get_topic_tasks):
            _ = self.client.geostatistics.kriging
        get_topic_tasks.assert_not_awaited()

    def test_a_task_is_keyed_as_the_caller_spells_it(self) -> None:
        """A hyphenated task is reached with underscores, so that is where its module lives."""
        self.assertIsNotNone(load_override("geostatistics", "kriging"))
        self.assertIsNone(load_override("geostatistics", "normal-score"))
        self.assertIsNone(load_override("geostatistics", "declustering"))
        self.assertIsNone(load_override("no-such-topic", "no_such_task"))

    def test_a_module_without_bind_does_not_claim_its_task(self) -> None:
        """``bind`` is the contract; a module that does not expose it is not an override."""
        with mock.patch("importlib.import_module", return_value=SimpleNamespace()):
            self.assertIsNone(load_override.__wrapped__("geostatistics", "declustering"))

    def test_a_broken_override_is_not_silently_skipped(self) -> None:
        """An override whose own imports fail is a bug, not an absent override."""
        error = ModuleNotFoundError("No module named 'somethingelse'", name="somethingelse")
        with mock.patch("importlib.import_module", side_effect=error):
            with self.assertRaises(ModuleNotFoundError):
                load_override.__wrapped__("geostatistics", "kriging")

    async def test_arun_always_takes_the_generic_path(self) -> None:
        """The generic route to an overridden task stays open, and is what parity compares to."""
        with self.catalogue, _capture_submit("evo.compute.engine") as submit:
            with self.assertRaises(_SubmitCaptured):
                await self.client.arun(
                    "geostatistics",
                    "kriging",
                    {
                        "source": {"object": POINTSET_URL, "attribute": GRADE_ATTRIBUTE},
                        "target": {"object": TARGET_URL, "attribute": {"operation": "create", "name": "grade"}},
                        "variogram": VARIOGRAM_URL,
                        "neighborhood": _search().model_dump(),
                        "kriging_method": {"type": "ordinary"},
                    },
                )
        self.assertEqual("kriging", submit.await_args.kwargs["task"])


# --------------------------------------------------------------------------- #
# What the override adds
# --------------------------------------------------------------------------- #


class TestWhatTheOverrideAdds(OverrideTestCase):
    async def test_valid_parameters_are_accepted(self) -> None:
        """Anti-vacuous guard: the checks below must reject their own case, not every case."""
        with self.completed_job(TASK_RESULT) as submit:
            await self.client.geostatistics.kriging.run(**self.inputs())
        submit.assert_awaited_once()

    async def test_a_search_that_can_never_be_satisfied_is_refused(self) -> None:
        """Each bound is legal on its own; the schema has no way to say they must agree."""
        with self.catalogue, _capture_submit("evo.compute.engine") as submit:
            with self.assertRaises(ParameterValidationError) as caught:
                await self.client.geostatistics.kriging.run(**self.inputs(search=_search(min_samples=99)))

        self.assertIn("min_samples (99) exceeds max_samples (20)", str(caught.exception))
        self.assertEqual("geostatistics.kriging", caught.exception.task)
        submit.assert_not_awaited()

    async def test_estimating_an_attribute_from_itself_is_refused(self) -> None:
        """Source and target are each valid references; pointing them at one attribute is not."""
        target = Target(object=POINTSET_URL, attribute=UpdateAttribute(reference=GRADE_ATTRIBUTE))
        with self.catalogue, _capture_submit("evo.compute.engine") as submit:
            with self.assertRaises(ParameterValidationError) as caught:
                await self.client.geostatistics.kriging.run(**self.inputs(target=target))

        self.assertIn("also the source attribute", str(caught.exception))
        submit.assert_not_awaited()

    async def test_updating_a_different_attribute_on_the_source_object_is_allowed(self) -> None:
        """The check is attribute-level: writing an estimate back onto the source object is fine."""
        target = Target(object=POINTSET_URL, attribute=UpdateAttribute(reference="attributes[?name=='estimate']"))
        with self.completed_job(TASK_RESULT) as submit:
            await self.client.geostatistics.kriging.run(**self.inputs(target=target))
        submit.assert_awaited_once()

    async def test_a_versioned_source_url_is_still_the_same_object(self) -> None:
        """``?version=`` names a moment in an object's history, not a different object."""
        source = Source(object=f"{POINTSET_URL}?version=42", attribute=GRADE_ATTRIBUTE)
        target = Target(object=POINTSET_URL, attribute=UpdateAttribute(reference=GRADE_ATTRIBUTE))
        with self.catalogue, _capture_submit("evo.compute.engine") as submit:
            with self.assertRaises(ParameterValidationError):
                await self.client.geostatistics.kriging.run(**self.inputs(source=source, target=target))
        submit.assert_not_awaited()

    async def test_the_same_object_written_a_different_way_is_still_the_same_object(self) -> None:
        """``ObjectReference`` keeps the caller's spelling, so the guard cannot compare text.

        The reference pattern is case-insensitive about the UUIDs it matches, and a fragment
        never reaches the path at all, so both of these are the source object under another name.
        """
        object_id = "0123abcd-ef00-0000-0000-00000000beef"
        canonical = f"{POINTSET_URL.rsplit('/', 1)[0]}/{object_id}"
        for spelling in (canonical.replace(object_id, object_id.upper()), f"{canonical}#section"):
            with self.subTest(spelling=spelling):
                source = Source(object=spelling, attribute=GRADE_ATTRIBUTE)
                target = Target(object=canonical, attribute=UpdateAttribute(reference=GRADE_ATTRIBUTE))
                with self.catalogue, _capture_submit("evo.compute.engine") as submit:
                    with self.assertRaises(ParameterValidationError):
                        await self.client.geostatistics.kriging.run(**self.inputs(source=source, target=target))
                submit.assert_not_awaited()

    async def test_the_same_attribute_on_a_different_object_is_allowed(self) -> None:
        """The other direction: the same expression against another object is not a collision."""
        target = Target(object=TARGET_URL, attribute=UpdateAttribute(reference=GRADE_ATTRIBUTE))
        with self.completed_job(TASK_RESULT) as submit:
            await self.client.geostatistics.kriging.run(**self.inputs(target=target))
        submit.assert_awaited_once()

    async def test_the_result_is_the_typed_one(self) -> None:
        """A hand-curated result with helpers the generic ``TaskResult`` could not synthesise."""
        with self.completed_job(TASK_RESULT):
            result = await self.client.geostatistics.kriging.run(**self.inputs())

        self.assertIsInstance(result, KrigingResult)
        self.assertEqual("Kriging complete.", result.message)
        self.assertEqual("Block model", result.target_name)
        self.assertEqual("kriged_grade", result.attribute_name)
        self.assertEqual("block-model", result.schema.sub_classification)

    def test_the_signature_is_the_hand_written_one(self) -> None:
        """Not synthesised from the schema: the arguments read as the typed SDK names them."""
        parameters = inspect.signature(self.client.geostatistics.kriging.run).parameters
        self.assertIn("search", parameters)  # the schema calls this ``neighborhood``
        self.assertIn("method", parameters)  # the schema calls this ``kriging_method``
        self.assertIn("source_filter", parameters)  # folded into ``source``; not a task parameter
        self.assertNotIn("self", parameters)


# --------------------------------------------------------------------------- #
# Payload fidelity
# --------------------------------------------------------------------------- #


class TestPayloadFidelity(OverrideTestCase):
    """One set of inputs, three paths, one payload."""

    async def assertThreeWayParity(self, **inputs: Any) -> None:
        """Require the runner, the override and the generic engine to submit the same payload."""
        search, method = inputs.pop("search"), inputs.pop("method")
        wire = dict(inputs, neighborhood=search, kriging_method=method)

        runner_payload = await self.runner_payload(KrigingRunner, KrigingRunner.params_type(**wire))
        candidates = {
            "override": await self.override_payload(**inputs, search=search, method=method),
            "engine": await self.engine_payload(KrigingRunner, **wire),
        }
        for label, payload in candidates.items():
            if differences := payload_differences(runner_payload, payload):
                self.fail(f"{label} payload diverges from the runner payload:\n  " + "\n  ".join(differences))

    async def test_the_everyday_call(self) -> None:
        """Source, target, variogram and a search, with the method left at ordinary."""
        await self.assertThreeWayParity(**self.inputs())

    async def test_a_tagged_method_object(self) -> None:
        """Simple kriging carries a mean, and the tag that says which branch of the union it is."""
        await self.assertThreeWayParity(**self.inputs(method=KrigingMethod.simple(mean=12.5)))

    async def test_a_typed_attribute_target_becomes_a_create_operation(self) -> None:
        """The override takes the typed inputs the runner takes, and expands them the same way."""
        await self.assertThreeWayParity(**self.inputs(target=_pending_attribute("kriged_grade", TARGET_URL)))

    async def test_a_folded_filter_lands_where_the_wire_carries_it(self) -> None:
        """``source_filter`` is a model input the serializer writes into ``source``.

        It is an argument the generic engine does not have -- the catalogue never advertises
        it -- which is one of the things a hand-written surface is for.
        """
        source_filter = Filter(where=FilterCondition(attribute=GRADE_ATTRIBUTE, operator="greater_than", threshold=0.5))
        payload = await self.override_payload(**self.inputs(source_filter=source_filter))
        self.assertNotIn("source_filter", payload)
        self.assertEqual(source_filter.model_dump(), payload["source"]["filter"])

    async def test_an_unset_optional_is_not_sent(self) -> None:
        """The override sends what it was given, so the platform's own defaults still apply."""
        payload = await self.override_payload(**self.inputs())
        self.assertNotIn("block_discretisation", payload)

    async def test_preview_is_still_the_engines_to_decide(self) -> None:
        """Unset means whatever the task's feature flag says, exactly as for a generic task."""
        catalogue = kriging_catalogue(feature_flag="geostatistics-preview")
        with mock.patch.object(DiscoveryClient, "get_topic_tasks", mock.AsyncMock(return_value=catalogue)):
            with _capture_submit("evo.compute.engine") as submit:
                with self.assertRaises(_SubmitCaptured):
                    await self.client.geostatistics.kriging.run(**self.inputs())
                self.assertTrue(submit.await_args.kwargs["preview"])

                with self.assertRaises(_SubmitCaptured):
                    await self.client.geostatistics.kriging.run(**self.inputs(), preview=False)
                self.assertFalse(submit.await_args.kwargs["preview"])
                self.assertNotIn("preview", submit.await_args.kwargs["parameters"])

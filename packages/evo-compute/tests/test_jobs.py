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

"""The job handle: submitting without waiting, and what the wait is worth once it is separate.

Three claims are being pinned. ``submit`` must do everything ``run`` does except the
waiting -- same discovery, same validation, same payload -- so that ``run`` being defined
as submit-then-wait is a fact rather than a comment. The handle must carry the parts of the
job the engine used to discard: the identifiers, the status, and cancellation. And progress
must actually arrive at an :class:`~evo.common.interfaces.IFeedback`, which the last class
here checks against a real :class:`~evo.compute.client.JobClient` rather than a mock, since
a mocked job would report whatever it was told to.
"""

from __future__ import annotations

import inspect
import json
from collections.abc import Iterator
from contextlib import contextmanager
from uuid import UUID

from evo.common.interfaces import IFeedback
from evo.common.test_tools import ORG as TEST_ORG
from evo.common.test_tools import MockResponse, TestWithConnector
from evo.common.utils import NoFeedback, get_header_metadata

from data import load_test_data
from evo.compute import (
    ComputeClient,
    JobClient,
    JobStatusEnum,
    ParameterValidationError,
    SyncComputeClient,
    SyncResultNode,
    SyncTaskJob,
    SyncTaskResult,
    TaskJob,
    TaskResult,
)
from test_engine import SOURCE_URL, ComputeClientTestCase, FakeContext

TEST_TOPIC = "test"
TEST_TASK = "job-handle"
TEST_JOB_ID = UUID(int=4321)

RESULT_PAYLOAD = {
    "message": "done",
    "target": {"reference": "https://example.com/objects/1", "name": "grid"},
}


class RecordingFeedback(IFeedback):
    """An ``IFeedback`` that keeps what it was told, so a test can assert on it."""

    def __init__(self) -> None:
        self.reports: list[tuple[float, str | None]] = []

    def progress(self, progress: float, message: str | None = None) -> None:
        self.reports.append((progress, message))


class TestSubmitMatchesRun(ComputeClientTestCase):
    """``submit`` is ``run`` minus the wait, and the payload proves it."""

    async def test_submit_hands_back_a_handle_without_waiting(self) -> None:
        with self.catalogue_response(), self.mock_job_client() as submit:
            job = await self.client.geostatistics.declustering.submit(source=SOURCE_URL)

        self.assertIsInstance(job, TaskJob)
        submit.assert_awaited_once()
        submit.return_value.wait_for_results.assert_not_awaited()

    async def test_both_paths_submit_the_same_payload(self) -> None:
        """The anti-vacuous half of defining ``run`` as submit-then-wait."""
        with self.catalogue_response(), self.mock_job_client() as submit:
            await self.client.geostatistics.declustering.run(source=SOURCE_URL, power=2.0)
        ran = submit.await_args.kwargs

        with self.catalogue_response(), self.mock_job_client() as submit:
            await self.client.geostatistics.declustering.submit(source=SOURCE_URL, power=2.0)

        self.assertEqual(ran, submit.await_args.kwargs)

    async def test_the_results_are_hydrated_the_way_run_hydrates_them(self) -> None:
        with self.catalogue_response(), self.mock_job_client(results=RESULT_PAYLOAD):
            from_run = await self.client.geostatistics.declustering.run(source=SOURCE_URL)

        with self.catalogue_response(), self.mock_job_client(results=RESULT_PAYLOAD):
            job = await self.client.geostatistics.declustering.submit(source=SOURCE_URL)
            from_job = await job.results()

        self.assertEqual(from_run, from_job)
        self.assertIs(type(from_run), type(from_job))
        self.assertIsInstance(from_job, TaskResult)
        self.assertEqual("grid", from_job.target.name)

    async def test_a_bad_parameter_is_refused_before_anything_is_submitted(self) -> None:
        """Validation belongs to ``submit``, so skipping the wait never skips the checks."""
        with self.catalogue_response(), self.mock_job_client() as submit:
            with self.assertRaises(ParameterValidationError) as caught:
                await self.client.geostatistics.declustering.submit(source=SOURCE_URL, sourse=SOURCE_URL)

        self.assertIn("sourse", str(caught.exception))
        submit.assert_not_awaited()

    async def test_a_binding_error_names_the_call_that_was_made(self) -> None:
        """``run`` and ``submit`` bind through one path, so the message has to say which was called."""
        for call in ("run", "submit"):
            with self.subTest(call=call), self.catalogue_response(), self.mock_job_client():
                with self.assertRaises(ParameterValidationError) as caught:
                    await getattr(self.client.geostatistics.declustering, call)(source=SOURCE_URL, nonsense=1)
                self.assertIn(f"geostatistics.declustering.{call}(): ", str(caught.exception))

    async def test_the_validation_overrides_reach_asubmit(self) -> None:
        with self.catalogue_response(), self.mock_job_client() as submit:
            await self.client.asubmit("geostatistics", "declustering", {"source": SOURCE_URL}, validate=False)
        submit.assert_awaited_once()

    async def test_submit_is_described_from_the_schema_like_run_is(self) -> None:
        """Both callables are shaped from the same spec; only the return type differs."""
        with self.catalogue_response(), self.mock_job_client():
            await self.client.geostatistics.declustering.submit(source=SOURCE_URL)

        proxy = self.client.geostatistics.declustering
        run = inspect.signature(proxy.run)
        submit = inspect.signature(proxy.submit)

        self.assertEqual(list(run.parameters), list(submit.parameters))
        self.assertIs(TaskResult, run.return_annotation)
        self.assertIs(TaskJob, submit.return_annotation)

    def test_both_callables_are_offered_for_completion(self) -> None:
        listed = dir(self.client.geostatistics.declustering)
        self.assertIn("run", listed)
        self.assertIn("submit", listed)


class TestTheHandle(ComputeClientTestCase):
    """What the engine used to throw away, and now hands over."""

    async def _submitted(self) -> TaskJob:
        with self.catalogue_response(), self.mock_job_client(results=RESULT_PAYLOAD) as submit:
            submit.return_value.id = TEST_JOB_ID
            submit.return_value.url = "https://unittest.localhost/compute/jobs/1"
            submit.return_value.topic = "geostatistics"
            submit.return_value.task = "declustering"
            return await self.client.geostatistics.declustering.submit(source=SOURCE_URL)

    async def test_it_carries_the_platforms_identifiers(self) -> None:
        job = await self._submitted()
        self.assertEqual(TEST_JOB_ID, job.id)
        self.assertEqual("https://unittest.localhost/compute/jobs/1", job.url)
        self.assertEqual("geostatistics", job.topic)
        self.assertEqual("declustering", job.task)
        self.assertIn(str(TEST_JOB_ID), repr(job))

    async def test_status_asks_the_platform_without_waiting_for_the_job(self) -> None:
        job = await self._submitted()
        await job.status()
        job._job.get_status.assert_awaited_once()
        job._job.wait_for_results.assert_not_awaited()

    async def test_cancel_reaches_the_job(self) -> None:
        """There was no way to reach this at all while ``arun`` owned the handle."""
        job = await self._submitted()
        await job.cancel()
        job._job.cancel.assert_awaited_once()

    async def test_the_wait_is_the_callers_to_configure(self) -> None:
        job = await self._submitted()
        await job.results(polling_interval_seconds=0.0)
        self.assertEqual(0.0, job._job.wait_for_results.await_args.kwargs["polling_interval_seconds"])


class TestFeedback(ComputeClientTestCase):
    """Progress is a property of the client, overridable for one wait."""

    async def test_a_client_without_one_stays_silent(self) -> None:
        with self.catalogue_response(), self.mock_job_client() as submit:
            await self.client.geostatistics.declustering.run(source=SOURCE_URL)
        self.assertIs(NoFeedback, submit.return_value.wait_for_results.await_args.kwargs["fb"])

    async def test_the_clients_feedback_reaches_the_wait_run_does(self) -> None:
        fb = RecordingFeedback()
        client = ComputeClient(self.context, fb=fb)
        with self.catalogue_response(), self.mock_job_client() as submit:
            await client.geostatistics.declustering.run(source=SOURCE_URL)
        self.assertIs(fb, submit.return_value.wait_for_results.await_args.kwargs["fb"])

    async def test_it_is_inherited_by_a_job_submitted_separately(self) -> None:
        fb = RecordingFeedback()
        client = ComputeClient(self.context, fb=fb)
        with self.catalogue_response(), self.mock_job_client() as submit:
            job = await client.geostatistics.declustering.submit(source=SOURCE_URL)
            await job.results()
        self.assertIs(fb, submit.return_value.wait_for_results.await_args.kwargs["fb"])

    async def test_one_named_on_the_wait_wins(self) -> None:
        """What a caller fanning out needs, so each job can take its own slice."""
        client = ComputeClient(self.context, fb=RecordingFeedback())
        mine = RecordingFeedback()
        with self.catalogue_response(), self.mock_job_client() as submit:
            job = await client.geostatistics.declustering.submit(source=SOURCE_URL)
            await job.results(fb=mine)
        self.assertIs(mine, submit.return_value.wait_for_results.await_args.kwargs["fb"])


class TestProgressArrives(TestWithConnector):
    """End to end against a real ``JobClient``: the platform's percentages reach the feedback.

    Everything above this point mocks the job, which can only show that an ``fb`` is handed
    over. This drives the actual polling loop off recorded status responses, so what is
    asserted is the number the platform published arriving at a caller's own object.
    """

    def setUp(self) -> None:
        super().setUp()
        self.setup_universal_headers(get_header_metadata(JobClient.__module__))
        self.job = TaskJob(
            JobClient(
                connector=self.connector,
                org_id=TEST_ORG.id,
                topic=TEST_TOPIC,
                task=TEST_TASK,
                job_id=TEST_JOB_ID,
            ),
            results_schema={
                "type": "object",
                "properties": {"target": {"type": "object", "output": "geoscience-object"}},
            },
            context=FakeContext(self.connector, TEST_ORG.id),
            fb=RecordingFeedback(),
        )

    @contextmanager
    def job_states(self, *data_files: str) -> Iterator[None]:
        responses = [
            MockResponse(
                status_code=202,
                headers={"Content-Type": "application/json"},
                content=json.dumps(load_test_data(name)),
            )
            for name in data_files
        ]
        responses.append(
            MockResponse(
                status_code=200,
                headers={"Content-Type": "application/json"},
                content=json.dumps(load_test_data(data_files[-1])),
            )
        )
        previous = self.transport.request.side_effect
        self.transport.request.side_effect = responses
        try:
            yield
        finally:
            self.transport.request.side_effect = previous

    async def test_the_published_percentage_reaches_the_feedback(self) -> None:
        fb: RecordingFeedback = self.job._fb  # type: ignore[assignment]
        with self.job_states(
            "job-response-requested.json",
            "job-response-in-progress.json",
            "job-response-succeeded.json",
        ):
            await self.job.results(polling_interval_seconds=0.0)

        # The platform publishes 0-100; the SDK's feedback contract is 0-1.
        self.assertIn((0.5, "Job in progress"), fb.reports)
        self.assertEqual((1.0, "Fetching results..."), fb.reports[-1])

    async def test_a_silent_client_reports_nothing_and_still_finishes(self) -> None:
        """The anti-vacuous half: the reports above come from the feedback, not the loop."""
        self.job._fb = NoFeedback
        with self.job_states(
            "job-response-requested.json",
            "job-response-in-progress.json",
            "job-response-succeeded.json",
        ):
            result = await self.job.results(polling_interval_seconds=0.0)
        self.assertIsInstance(result, TaskResult)

    async def test_status_answers_without_running_the_loop(self) -> None:
        with self.job_states("job-response-in-progress.json"):
            status = await self.job.status()
        self.assertEqual(JobStatusEnum.in_progress, status.status)
        self.assertEqual(50, status.progress)


class TestTheBlockingHandle(ComputeClientTestCase):
    """The same handle through ``SyncComputeClient``, with the ``await`` removed."""

    def setUp(self) -> None:
        super().setUp()
        self.sync_client = SyncComputeClient(self.context)

    def test_submit_hands_back_a_blocking_handle(self) -> None:
        with self.catalogue_response(), self.mock_job_client():
            job = self.sync_client.geostatistics.declustering.submit(source=SOURCE_URL)

        self.assertIsInstance(job, SyncTaskJob)
        # Code written for a ``TaskJob`` awaits these, which a blocking handle cannot honour.
        self.assertNotIsInstance(job, TaskJob)
        for call in (job.status, job.cancel, job.results):
            with self.subTest(call=call.__name__):
                self.assertFalse(inspect.iscoroutinefunction(call))

    def test_the_results_are_the_blocking_tree(self) -> None:
        with self.catalogue_response(), self.mock_job_client(results=RESULT_PAYLOAD):
            job = self.sync_client.geostatistics.declustering.submit(source=SOURCE_URL)
            result = job.results()

        self.assertIsInstance(result, SyncTaskResult)
        self.assertIsInstance(result.target, SyncResultNode)
        self.assertEqual("grid", result.target.name)

    def test_blocking_run_and_submit_send_the_same_payload(self) -> None:
        with self.catalogue_response(), self.mock_job_client() as submit:
            self.sync_client.geostatistics.declustering.run(source=SOURCE_URL)
        ran = submit.await_args.kwargs

        with self.catalogue_response(), self.mock_job_client() as submit:
            self.sync_client.geostatistics.declustering.submit(source=SOURCE_URL)

        self.assertEqual(ran, submit.await_args.kwargs)

    def test_a_binding_error_names_the_call_that_was_made(self) -> None:
        for call in ("run", "submit"):
            with self.subTest(call=call), self.catalogue_response(), self.mock_job_client():
                with self.assertRaises(ParameterValidationError) as caught:
                    getattr(self.sync_client.geostatistics.declustering, call)(source=SOURCE_URL, nonsense=1)
                self.assertIn(f"geostatistics.declustering.{call}(): ", str(caught.exception))

    def test_status_blocks_and_answers(self) -> None:
        with self.catalogue_response(), self.mock_job_client() as submit:
            submit.return_value.get_status.return_value = "asked"
            job = self.sync_client.geostatistics.declustering.submit(source=SOURCE_URL)
            self.assertEqual("asked", job.status())

    def test_the_two_namespaces_offer_the_same_callables(self) -> None:
        awaited = self.client.geostatistics.declustering
        blocking = self.sync_client.geostatistics.declustering
        self.assertEqual(dir(awaited), dir(blocking))

    def test_the_blocking_submit_mirrors_asubmit(self) -> None:
        awaited = inspect.signature(ComputeClient.asubmit)
        blocking = inspect.signature(SyncComputeClient.submit)
        self.assertEqual(list(awaited.parameters), list(blocking.parameters))
        self.assertTrue(inspect.iscoroutinefunction(ComputeClient.asubmit))
        self.assertFalse(inspect.iscoroutinefunction(SyncComputeClient.submit))

    def test_the_feedback_reaches_the_client_that_does_the_work(self) -> None:
        fb = RecordingFeedback()
        self.assertIs(fb, SyncComputeClient(self.context, fb=fb)._async._fb)

"""Job lifecycle and shared execution/submission regression tests."""

from __future__ import annotations

import inspect
import json
from collections.abc import Iterator
from contextlib import contextmanager
from unittest import IsolatedAsyncioTestCase, mock
from uuid import UUID

from evo.common import IContext
from evo.common.interfaces import IFeedback
from evo.common.test_tools import ORG as TEST_ORG
from evo.common.test_tools import MockResponse, TestWithConnector
from evo.common.utils import NoFeedback, Retry, get_header_metadata

from data import load_test_data
from evo.compute import ComputeClient, JobClient, JobStatusEnum, ParameterValidationError, TaskJob, TaskResult
from test_engine import SOURCE_URL, ComputeClientTestCase, FakeContext

TEST_JOB_ID = UUID(int=4321)
RESULT_PAYLOAD = {
    "message": "done",
    "target": {"reference": "https://example.com/objects/1", "name": "grid"},
}
RESULT_SCHEMA = {
    "type": "object",
    "properties": {"target": {"type": "object", "output": "geoscience-object"}},
}


class TestTaskJob(IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.backend = mock.MagicMock(spec=JobClient)
        self.backend.id = TEST_JOB_ID
        self.backend.url = "https://example.com/compute/jobs/1"
        self.backend.topic = "geostatistics"
        self.backend.task = "declustering"
        self.backend.wait_for_results.return_value = RESULT_PAYLOAD
        self.context = mock.MagicMock(spec=IContext)
        self.job = TaskJob(self.backend, RESULT_SCHEMA, self.context)

    def test_identifiers_are_preserved(self) -> None:
        self.assertEqual(TEST_JOB_ID, self.job.id)
        self.assertEqual(self.backend.url, self.job.url)
        self.assertEqual("geostatistics", self.job.topic)
        self.assertEqual("declustering", self.job.task)
        self.assertIn(str(TEST_JOB_ID), repr(self.job))

    async def test_status_does_not_wait_for_results(self) -> None:
        self.backend.get_status.return_value = mock.sentinel.progress
        self.assertIs(mock.sentinel.progress, await self.job.status())
        self.backend.get_status.assert_awaited_once_with()
        self.backend.wait_for_results.assert_not_awaited()

    async def test_cancel_delegates_without_waiting(self) -> None:
        await self.job.cancel()
        self.backend.cancel.assert_awaited_once_with()
        self.backend.wait_for_results.assert_not_awaited()

    async def test_results_are_hydrated_with_async_loaders(self) -> None:
        result = await self.job.results()
        self.assertIsInstance(result, TaskResult)
        self.assertEqual(RESULT_PAYLOAD, result)
        self.assertEqual("grid", result.target.name)
        self.assertTrue(inspect.iscoroutinefunction(result.target.load))
        self.backend.wait_for_results.assert_awaited_once_with(polling_interval_seconds=0.5, retry=None, fb=NoFeedback)

    async def test_results_inherit_client_feedback(self) -> None:
        feedback = mock.MagicMock(spec=IFeedback)
        job = TaskJob(self.backend, RESULT_SCHEMA, self.context, feedback)
        await job.results()
        self.assertIs(feedback, self.backend.wait_for_results.await_args.kwargs["fb"])

    async def test_results_forward_feedback_polling_and_retry_overrides(self) -> None:
        feedback = mock.MagicMock(spec=IFeedback)
        retry = mock.MagicMock(spec=Retry)
        await self.job.results(fb=feedback, polling_interval_seconds=0.0, retry=retry)
        self.backend.wait_for_results.assert_awaited_once_with(polling_interval_seconds=0.0, retry=retry, fb=feedback)


class RecordingFeedback(IFeedback):
    def __init__(self) -> None:
        self.reports: list[tuple[float, str | None]] = []

    def progress(self, progress: float, message: str | None = None) -> None:
        self.reports.append((progress, message))


class TestSubmitMatchesRun(ComputeClientTestCase):
    async def test_submission_does_not_wait(self) -> None:
        with self.catalogue_response(), self.mock_job_client() as submit:
            job = await self.client.geostatistics.declustering.submit(source=SOURCE_URL)
        self.assertIsInstance(job, TaskJob)
        submit.assert_awaited_once()
        submit.return_value.wait_for_results.assert_not_awaited()

    async def test_run_and_submit_have_identical_payloads(self) -> None:
        with self.catalogue_response(), self.mock_job_client() as submit:
            await self.client.geostatistics.declustering.run(source=SOURCE_URL, power=2.0)
        ran = submit.await_args.kwargs
        with self.catalogue_response(), self.mock_job_client() as submit:
            await self.client.geostatistics.declustering.submit(source=SOURCE_URL, power=2.0)
        self.assertEqual(ran, submit.await_args.kwargs)

    async def test_both_paths_hydrate_the_same_results(self) -> None:
        with self.catalogue_response(), self.mock_job_client(results=RESULT_PAYLOAD):
            from_run = await self.client.geostatistics.declustering.run(source=SOURCE_URL)
        with self.catalogue_response(), self.mock_job_client(results=RESULT_PAYLOAD):
            job = await self.client.geostatistics.declustering.submit(source=SOURCE_URL)
            from_job = await job.results()
        self.assertEqual(from_run, from_job)
        self.assertIs(type(from_run), type(from_job))
        self.assertEqual("grid", from_job.target.name)

    async def test_invalid_parameters_do_not_submit(self) -> None:
        with self.catalogue_response(), self.mock_job_client() as submit:
            with self.assertRaisesRegex(ParameterValidationError, "source"):
                await self.client.geostatistics.declustering.submit(sourse=SOURCE_URL)
        submit.assert_not_awaited()

    async def test_binding_errors_name_the_operation(self) -> None:
        for operation in ("run", "submit"):
            with self.subTest(operation=operation), self.catalogue_response(), self.mock_job_client():
                with self.assertRaises(ParameterValidationError) as caught:
                    await getattr(self.client.geostatistics.declustering, operation)(nonsense=1)
                self.assertIn(f"geostatistics.declustering.{operation}(): ", str(caught.exception))

    async def test_unknown_fields_warn_and_are_preserved_in_both_modes(self) -> None:
        spec = next(task for task in self.catalogue["results"] if task["name"] == "declustering")
        spec["parameters"]["additionalProperties"] = False
        spec["parameters"]["properties"]["settings"] = {
            "type": "object",
            "properties": {"count": {"type": "integer", "minimum": 1}},
            "required": ["count"],
            "additionalProperties": False,
        }
        parameters = {
            "source": SOURCE_URL,
            "settings": {"count": 2, "new_option": False, 0: None},
            "future-parameter": {"unset": None, "secret": "do-not-log-this-value"},
        }
        for validate, deep in ((True, False), (True, True), (False, False)):
            client = ComputeClient(self.context, validate=validate, deep_validation=deep)
            for operation in ("run", "submit"):
                with self.subTest(validate=validate, deep=deep, operation=operation):
                    with (
                        self.catalogue_response(),
                        self.mock_job_client() as submit,
                        self.assertLogs("compute.validation", level="WARNING") as logged,
                    ):
                        await getattr(client.geostatistics.declustering, operation)(**parameters)
                    self.assertEqual(parameters, submit.await_args.kwargs["parameters"])
                    self.assertEqual(2, len(logged.output))
                    messages = "\n".join(logged.output)
                    self.assertIn("future-parameter", messages)
                    self.assertIn("settings", messages)
                    self.assertIn("new_option", messages)
                    self.assertNotIn("do-not-log-this-value", messages)
                    submit.assert_awaited_once()

    async def test_unknown_fields_do_not_hide_known_value_errors(self) -> None:
        client = ComputeClient(self.context, deep_validation=True)
        with self.catalogue_response(), self.mock_job_client() as submit:
            with self.assertRaisesRegex(ParameterValidationError, "power"):
                await client.asubmit(
                    "geostatistics",
                    "declustering",
                    {"source": SOURCE_URL, "power": "invalid", "future_parameter": True},
                )
        submit.assert_not_awaited()

    async def test_asubmit_preserves_the_callers_dictionary(self) -> None:
        parameters = {"source": SOURCE_URL, "preview": True}
        with self.catalogue_response(), self.mock_job_client() as submit:
            await self.client.asubmit("geostatistics", "declustering", parameters)
        self.assertEqual({"source": SOURCE_URL, "preview": True}, parameters)
        self.assertTrue(submit.await_args.kwargs["preview"])
        self.assertEqual({"source": SOURCE_URL}, submit.await_args.kwargs["parameters"])

    async def test_validation_options_reach_submission(self) -> None:
        with self.catalogue_response(), self.mock_job_client() as submit:
            with mock.patch("evo.compute.engine.validate_parameters") as validate:
                await self.client.asubmit(
                    "geostatistics",
                    "declustering",
                    {"source": SOURCE_URL},
                    validate=False,
                    deep_validation=True,
                    check_schemas=False,
                )
        validate.assert_not_called()
        submit.assert_awaited_once()

    async def test_submit_keeps_platform_topic_and_parameter_names(self) -> None:
        with self.catalogue_response(), self.mock_job_client() as submit:
            await self.client.vis_service.tiling.submit(object_url=SOURCE_URL, s2s_user_info="{}")
        self.assertEqual("vis-service", submit.await_args.kwargs["topic"])
        self.assertEqual({"object_url": SOURCE_URL, "s2s-user-info": "{}"}, submit.await_args.kwargs["parameters"])

    async def test_signatures_only_differ_in_return_type(self) -> None:
        with self.catalogue_response(), self.mock_job_client():
            await self.client.geostatistics.declustering.submit(source=SOURCE_URL)
        proxy = self.client.geostatistics.declustering
        run = inspect.signature(proxy.run)
        submit = inspect.signature(proxy.submit)
        self.assertEqual(run.parameters, submit.parameters)
        self.assertIs(TaskResult, run.return_annotation)
        self.assertIs(TaskJob, submit.return_annotation)

    def test_completion_lists_both_operations_without_discovery(self) -> None:
        proxy = self.client.geostatistics.declustering
        self.assertIn("run", dir(proxy))
        self.assertIn("submit", dir(proxy))
        self.transport.request.assert_not_called()

    async def test_run_and_submit_share_discovery(self) -> None:
        with self.catalogue_response(), self.mock_job_client():
            await self.client.geostatistics.declustering.run(source=SOURCE_URL)
            await self.client.geostatistics.declustering.submit(source=SOURCE_URL)
        self.assertEqual(1, self.transport.request.call_count)

    async def test_default_feedback_is_silent(self) -> None:
        with self.catalogue_response(), self.mock_job_client() as submit:
            await self.client.geostatistics.declustering.run(source=SOURCE_URL)
        self.assertIs(NoFeedback, submit.return_value.wait_for_results.await_args.kwargs["fb"])

    async def test_client_feedback_reaches_run_and_separate_submission(self) -> None:
        feedback = RecordingFeedback()
        client = ComputeClient(self.context, fb=feedback)
        for operation in ("run", "submit"):
            with self.subTest(operation=operation), self.catalogue_response(), self.mock_job_client() as submit:
                result = await getattr(client.geostatistics.declustering, operation)(source=SOURCE_URL)
                if operation == "submit":
                    await result.results()
                self.assertIs(feedback, submit.return_value.wait_for_results.await_args.kwargs["fb"])

    async def test_wait_feedback_overrides_client_feedback(self) -> None:
        client = ComputeClient(self.context, fb=RecordingFeedback())
        feedback = RecordingFeedback()
        with self.catalogue_response(), self.mock_job_client() as submit:
            job = await client.geostatistics.declustering.submit(source=SOURCE_URL)
            await job.results(fb=feedback)
        self.assertIs(feedback, submit.return_value.wait_for_results.await_args.kwargs["fb"])


class TestProgressArrives(TestWithConnector):
    def setUp(self) -> None:
        super().setUp()
        self.setup_universal_headers(get_header_metadata(JobClient.__module__))
        self.feedback = RecordingFeedback()
        self.job = TaskJob(
            JobClient(
                connector=self.connector,
                org_id=TEST_ORG.id,
                topic="test",
                task="job-handle",
                job_id=TEST_JOB_ID,
            ),
            results_schema=RESULT_SCHEMA,
            context=FakeContext(self.connector, TEST_ORG.id),
            fb=self.feedback,
        )

    @contextmanager
    def job_states(self, *data_files: str) -> Iterator[None]:
        responses = [
            MockResponse(
                status_code=202, headers={"Content-Type": "application/json"}, content=json.dumps(load_test_data(name))
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

    async def test_platform_progress_reaches_feedback(self) -> None:
        with self.job_states(
            "job-response-requested.json", "job-response-in-progress.json", "job-response-succeeded.json"
        ):
            await self.job.results(polling_interval_seconds=0.0)
        self.assertIn((0.5, "Job in progress"), self.feedback.reports)
        self.assertEqual((1.0, "Fetching results..."), self.feedback.reports[-1])

    async def test_no_feedback_still_collects_results(self) -> None:
        self.job._fb = NoFeedback
        with self.job_states(
            "job-response-requested.json", "job-response-in-progress.json", "job-response-succeeded.json"
        ):
            result = await self.job.results(polling_interval_seconds=0.0)
        self.assertIsInstance(result, TaskResult)
        self.assertEqual([], self.feedback.reports)

    async def test_status_does_not_start_polling(self) -> None:
        with self.job_states("job-response-in-progress.json"):
            progress = await self.job.status()
        self.assertEqual(JobStatusEnum.in_progress, progress.status)
        self.assertEqual(50, progress.progress)
        self.assertEqual(1, self.transport.request.call_count)

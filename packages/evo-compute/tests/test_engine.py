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

from __future__ import annotations

import inspect
import json
from collections.abc import Iterator
from contextlib import contextmanager
from unittest import mock

from evo.common.test_tools import ORG as TEST_ORG
from evo.common.test_tools import TestWithConnector

from data import load_test_data
from evo.compute import ComputeClient


class FakeContext:
    """Minimal duck-typed IContext exposing just what ComputeClient uses."""

    def __init__(self, connector, org_id) -> None:
        self._connector = connector
        self._org_id = org_id

    def get_connector(self):
        return self._connector

    def get_org_id(self):
        return self._org_id


class TestComputeClient(TestWithConnector):
    def setUp(self) -> None:
        super().setUp()
        self.context = FakeContext(self.connector, TEST_ORG.id)
        self.catalogue = load_test_data("discovery-tasks.json")
        self.client = ComputeClient(self.context)

    @contextmanager
    def catalogue_response(self) -> Iterator[None]:
        """Serve the discovery catalogue for any GET /tasks issued while active."""
        with self.transport.set_http_response(
            status_code=200,
            content=json.dumps(self.catalogue),
            headers={"Content-Type": "application/json"},
        ):
            yield

    @contextmanager
    def mock_job_client(self, results: dict | None = None) -> Iterator[mock.AsyncMock]:
        """Patch JobClient.submit so tests exercise the engine, not the job lifecycle."""
        job = mock.Mock()
        job.wait_for_results = mock.AsyncMock(return_value={"ok": True} if results is None else results)
        submit = mock.AsyncMock(return_value=job)
        with mock.patch("evo.compute.engine.JobClient") as mock_job_client:
            mock_job_client.submit = submit
            yield submit

    # -- namespace / discovery timing -------------------------------------- #

    def test_attribute_access_does_not_trigger_discovery(self) -> None:
        """Reaching a topic/task never hits the network; only run() does."""
        _ = self.client.geostatistics.declustering
        self.transport.request.assert_not_called()

    async def test_run_discovers_then_submits(self) -> None:
        """run() fetches the catalogue, then submits the resolved task."""
        with self.catalogue_response(), self.mock_job_client() as submit:
            result = await self.client.geostatistics.declustering.run(source="obj-1")

        submit.assert_awaited_once()
        kwargs = submit.await_args.kwargs
        self.assertEqual("geostatistics", kwargs["topic"])
        self.assertEqual("declustering", kwargs["task"])
        self.assertEqual({"source": "obj-1"}, kwargs["parameters"])
        self.assertFalse(kwargs["preview"])
        self.assertEqual({"ok": True}, result)

    async def test_hyphenated_task_names_are_reachable_with_underscores(self) -> None:
        """``normal_score_gcp`` resolves to the platform's ``normal-score-gcp``."""
        with self.catalogue_response(), self.mock_job_client() as submit:
            await self.client.geostatistics.normal_score_gcp.run(distribution="dist-1")
        self.assertEqual("normal-score-gcp", submit.await_args.kwargs["task"])

    async def test_catalogue_is_cached_across_runs(self) -> None:
        """The catalogue is fetched once and reused for later runs (any task/topic)."""
        with self.catalogue_response(), self.mock_job_client():
            await self.client.geostatistics.declustering.run(source="obj-1")
            await self.client.geostatistics.kriging_gcp.run(source="s", target="t", variogram="v")
        self.assertEqual(1, self.transport.request.call_count)

    # -- preview flag ------------------------------------------------------ #

    async def test_preview_defaults_to_feature_flag(self) -> None:
        """A feature-flagged task opts into preview by default."""
        with self.catalogue_response(), self.mock_job_client() as submit:
            await self.client.geostatistics.kriging_gcp.run(source="s", target="t", variogram="v")
        self.assertTrue(submit.await_args.kwargs["preview"])

    async def test_preview_can_be_overridden(self) -> None:
        """An explicit preview flag overrides the feature-flag default."""
        with self.catalogue_response(), self.mock_job_client() as submit:
            await self.client.geostatistics.kriging_gcp.run(source="s", target="t", variogram="v", preview=False)
        self.assertFalse(submit.await_args.kwargs["preview"])
        self.assertNotIn("preview", submit.await_args.kwargs["parameters"])

    # -- errors ------------------------------------------------------------ #

    async def test_unknown_task_raises_attribute_error(self) -> None:
        """Running a task the topic doesn't advertise reports what is available."""
        with self.catalogue_response(), self.mock_job_client():
            with self.assertRaises(AttributeError) as ctx:
                await self.client.geostatistics.does_not_exist.run()
        self.assertIn("declustering", str(ctx.exception))

    async def test_missing_required_parameter_raises_type_error(self) -> None:
        """A missing required parameter is rejected before submission."""
        with self.catalogue_response(), self.mock_job_client() as submit:
            with self.assertRaises(TypeError):
                await self.client.geostatistics.declustering.run()
        submit.assert_not_awaited()

    # -- progressive ergonomics (cache-backed, no forced discovery) -------- #

    async def test_signature_is_synthesised_after_caching(self) -> None:
        """Once a task is cached, run() advertises a schema-shaped signature."""
        with self.catalogue_response(), self.mock_job_client():
            await self.client.geostatistics.declustering.run(source="obj-1")

        signature = inspect.signature(self.client.geostatistics.declustering.run)
        self.assertIn("source", signature.parameters)
        self.assertIn("power", signature.parameters)
        self.assertIn("preview", signature.parameters)
        self.assertIs(inspect.Parameter.empty, signature.parameters["source"].default)
        self.assertIsNone(signature.parameters["power"].default)

    async def test_dir_lists_cached_topics_and_tasks(self) -> None:
        """After a run, tab-completion surfaces the cached topics/tasks."""
        with self.catalogue_response(), self.mock_job_client():
            await self.client.geostatistics.declustering.run(source="obj-1")

        self.assertIn("geostatistics", dir(self.client))
        topic_tasks = dir(self.client.geostatistics)
        self.assertIn("declustering", topic_tasks)
        self.assertIn("normal_score_gcp", topic_tasks)
        self.assertIn("kriging_gcp", topic_tasks)

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

"""The blocking entry point: the bridge it runs on, and the client that uses it.

Two things are being pinned here. The bridge has to behave like a piece of infrastructure --
one thread however many calls are made, exceptions arriving as themselves, and a refusal
rather than a hang where a hang is possible. The client has to add nothing: the same
payload reaches ``JobClient.submit``, the same errors come back, and the only difference is
that the caller never writes ``await``.
"""

from __future__ import annotations

import asyncio
import inspect
import threading
import traceback
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest import mock

from evo.common.exceptions import RetryError, TransportError
from evo.common.test_tools import ORG as TEST_ORG

from evo.compute import (
    ComputeClient,
    ParameterValidationError,
    ResultNode,
    SyncComputeClient,
    SyncResultNode,
    SyncTaskResult,
    TaskResult,
    run_sync,
)
from evo.compute._sync import _BRIDGE, _Bridge, _from_another_loop
from evo.compute.exceptions import SyncBridgeError
from test_engine import SOURCE_URL, ComputeClientTestCase, FakeContext

_FOREIGN_LOOP_MESSAGE = (
    "Task <Task pending name='Task-1' coro=<request()>> got Future <Future pending> attached to a different loop"
)


def _transport_error_from_another_loop() -> TransportError:
    """The shape aiohttp's loop complaint really arrives in, three wrappers deep.

    Reproduced from a notebook that logged in with ``await`` and then called the blocking
    client: every retry attempt fails the same way, the retry wrapper groups them, and the
    transport wraps that.
    """
    return TransportError(
        "Reached maximum number of retries",
        RetryError("Retry failed", [RuntimeError(_FOREIGN_LOOP_MESSAGE) for _ in range(3)]),
    )


class TestBridge(unittest.TestCase):
    """:func:`run_sync` on its own, with no compute client in the way."""

    async def _answer(self, value: int = 42) -> int:
        await asyncio.sleep(0)
        return value

    def _bridge_threads(self) -> list[threading.Thread]:
        return [thread for thread in threading.enumerate() if thread.name == "evo-compute-sync"]

    def test_a_coroutine_runs_to_completion_and_returns_its_value(self) -> None:
        self.assertEqual(42, run_sync(self._answer()))

    def test_repeated_calls_share_one_thread(self) -> None:
        """``asyncio.run`` would close its loop each time; this keeps one for the process."""
        results = [run_sync(self._answer(index)) for index in range(5)]
        self.assertEqual([0, 1, 2, 3, 4], results)
        self.assertEqual(1, len(self._bridge_threads()))

    def test_concurrent_callers_are_served_by_the_same_thread(self) -> None:
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda index: run_sync(self._answer(index)), range(32)))
        self.assertEqual(list(range(32)), results)
        self.assertEqual(1, len(self._bridge_threads()))

    def test_the_thread_is_started_on_first_use_and_stopped_without_being_asked(self) -> None:
        """The bridge is a daemon and is registered with :mod:`atexit`; nothing is left running."""
        _BRIDGE.stop()
        self.assertEqual([], self._bridge_threads())
        run_sync(self._answer())
        (thread,) = self._bridge_threads()
        self.assertTrue(thread.daemon)
        _BRIDGE.stop()
        self.assertFalse(thread.is_alive())
        # A later call simply starts a fresh one.
        self.assertEqual(42, run_sync(self._answer()))

    def test_a_loop_still_busy_after_the_join_is_left_to_its_daemon_rather_than_closed(self) -> None:
        """Closing a loop that is still turning raises, which at exit would end in a traceback."""
        bridge = _Bridge()
        loop, busy = bridge.loop(), threading.Event()
        thread = bridge.thread
        self.assertIsNotNone(thread)
        loop.call_soon_threadsafe(busy.wait)
        self.addCleanup(loop.close)
        self.addCleanup(thread.join)
        self.addCleanup(busy.set)

        bridge.stop(timeout=0.01)

        self.assertTrue(thread.is_alive())
        self.assertFalse(loop.is_closed())

    def test_an_exception_arrives_as_itself_with_the_frames_that_raised_it(self) -> None:
        async def fails() -> None:
            raise ValueError("the task was rejected")

        # Not ``assertRaises``: it clears the traceback, which is the thing under test.
        try:
            run_sync(fails())
        except ValueError as error:
            raised = error
        else:
            self.fail("the coroutine's exception did not reach the caller")

        self.assertEqual("the task was rejected", str(raised))
        frames = [frame.name for frame in traceback.extract_tb(raised.__traceback__)]
        self.assertIn("fails", frames, "the frame that raised is missing from the traceback")
        self.assertIn("run_sync", frames, "the caller's side of the bridge is missing")

    def test_calling_it_from_inside_its_own_loop_is_refused_rather_than_deadlocking(self) -> None:
        """The one call that really would hang: nothing else can produce the result.

        A notebook cell is also inside a running loop, but a different one on a different
        thread, so it is not refused -- see :class:`TestFromInsideARunningLoop`.
        """

        async def blocks_on_itself() -> None:
            run_sync(self._answer())

        with self.assertRaises(SyncBridgeError) as caught:
            run_sync(blocks_on_itself())
        self.assertIn("would wait for a result that only this thread can produce", str(caught.exception))

    def test_a_connection_belonging_to_another_loop_is_named_as_the_problem(self) -> None:
        async def touches_a_foreign_connection() -> None:
            raise _transport_error_from_another_loop()

        with self.assertRaises(SyncBridgeError) as caught:
            run_sync(touches_a_foreign_connection())

        message = str(caught.exception)
        self.assertIn("first used on a different event loop", message)
        self.assertIn("run_sync(manager.login())", message)
        self.assertIn("ComputeClient", message)
        self.assertIsInstance(caught.exception.__cause__, TransportError)

    def test_an_ordinary_failure_is_not_mistaken_for_a_loop_problem(self) -> None:
        async def fails() -> None:
            raise TransportError("Reached maximum number of retries", RetryError("Retry failed", [OSError("refused")]))

        with self.assertRaises(TransportError):
            run_sync(fails())


class TestForeignLoopDetection(unittest.TestCase):
    """The complaint is buried, so :func:`_from_another_loop` has to dig for it."""

    def test_it_is_found_through_the_retry_group_the_transport_wraps(self) -> None:
        self.assertTrue(_from_another_loop(_transport_error_from_another_loop()))

    def test_it_is_found_through_a_cause(self) -> None:
        error = ValueError("submitting the job failed")
        error.__cause__ = RuntimeError("<asyncio.Lock> is bound to a different event loop")
        self.assertTrue(_from_another_loop(error))

    def test_an_unrelated_error_is_left_alone(self) -> None:
        self.assertFalse(_from_another_loop(TransportError("Connection reset", OSError("reset"))))

    def test_a_cycle_between_wrapped_errors_terminates(self) -> None:
        first = ValueError("first")
        second = ValueError("second")
        first.__cause__ = second
        second.__cause__ = first
        self.assertFalse(_from_another_loop(first))


class TestSyncComputeClient(ComputeClientTestCase):
    """The blocking client against the same fixtures the asynchronous one is tested with.

    ``self.client`` is the asynchronous engine and ``self.sync_client`` wraps a second one,
    so the two can be compared on the same catalogue within a single test.
    """

    def setUp(self) -> None:
        super().setUp()
        self.sync_client = SyncComputeClient(self.context)

    # -- namespace --------------------------------------------------------- #

    def test_attribute_access_does_not_trigger_discovery(self) -> None:
        _ = self.sync_client.geostatistics.declustering
        self.transport.request.assert_not_called()

    def test_the_namespace_reads_the_same_as_the_asynchronous_one(self) -> None:
        with self.catalogue_response(), self.mock_job_client():
            self.sync_client.geostatistics.declustering.run(source=SOURCE_URL)

        self.assertIn("geostatistics", dir(self.sync_client))
        self.assertIn("declustering", dir(self.sync_client.geostatistics))
        self.assertIn("run", dir(self.sync_client.geostatistics.declustering))
        self.assertEqual("<compute topic 'geostatistics'>", repr(self.sync_client.geostatistics))
        self.assertEqual(f"SyncComputeClient(org_id={str(TEST_ORG.id)!r})", repr(self.sync_client))

    def test_run_advertises_the_schema_signature_without_being_a_coroutine(self) -> None:
        with self.catalogue_response(), self.mock_job_client():
            self.sync_client.geostatistics.declustering.run(source=SOURCE_URL)

        run = self.sync_client.geostatistics.declustering.run
        self.assertFalse(inspect.iscoroutinefunction(run))
        signature = inspect.signature(run)
        self.assertEqual("source", next(iter(signature.parameters)))
        self.assertIs(SyncTaskResult, signature.return_annotation)

    def test_it_is_not_a_context_manager(self) -> None:
        """Consistent with ``ComputeClient``: there is nothing to enter or leave."""
        self.assertFalse(hasattr(self.sync_client, "__enter__"))
        self.assertFalse(hasattr(self.sync_client, "__aenter__"))

    # -- execution --------------------------------------------------------- #

    def test_it_submits_exactly_what_the_asynchronous_client_submits(self) -> None:
        """The blocking client is a wrapper, so the payload cannot differ."""
        with self.catalogue_response(), self.mock_job_client() as submit:
            self.sync_client.geostatistics.declustering.run(source=SOURCE_URL)
        blocking = submit.await_args.kwargs

        with self.catalogue_response(), self.mock_job_client() as submit:
            run_sync(self.client.geostatistics.declustering.run(source=SOURCE_URL))
        awaited = submit.await_args.kwargs

        self.assertEqual(awaited, blocking)

    def test_run_by_name_mirrors_arun(self) -> None:
        with self.catalogue_response(), self.mock_job_client() as submit:
            self.sync_client.run("geostatistics", "declustering", {"source": SOURCE_URL})

        self.assertEqual({"source": SOURCE_URL}, submit.await_args.kwargs["parameters"])

    def test_the_validation_overrides_are_passed_straight_through(self) -> None:
        with self.catalogue_response(), self.mock_job_client():
            with self.assertRaises(ParameterValidationError):
                self.sync_client.run("geostatistics", "declustering", {"source": None})
            # The same call goes through once validation is off for it.
            self.sync_client.run("geostatistics", "declustering", {"source": None}, validate=False)

    def test_a_validation_failure_surfaces_as_the_error_the_engine_raised(self) -> None:
        with self.catalogue_response(), self.mock_job_client():
            with self.assertRaises(ParameterValidationError) as caught:
                self.sync_client.geostatistics.declustering.run(source=SOURCE_URL, nonsense=1)

        self.assertIn("unexpected keyword argument 'nonsense'", str(caught.exception))

    def test_repeated_runs_reuse_the_discovered_catalogue(self) -> None:
        with self.catalogue_response(), self.mock_job_client() as submit:
            self.sync_client.geostatistics.declustering.run(source=SOURCE_URL)
            self.sync_client.geostatistics.declustering.run(source=SOURCE_URL)

        self.assertEqual(2, submit.await_count)
        self.assertEqual(1, self.transport.request.await_count)

    def test_concurrent_runs_from_several_threads_all_complete(self) -> None:
        with self.catalogue_response(), self.mock_job_client() as submit:
            with ThreadPoolExecutor(max_workers=4) as pool:
                results = list(
                    pool.map(
                        lambda _: self.sync_client.geostatistics.declustering.run(source=SOURCE_URL),
                        range(8),
                    )
                )

        self.assertEqual(8, submit.await_count)
        self.assertTrue(all(result == {"ok": True} for result in results))

    # -- results ----------------------------------------------------------- #

    def test_the_result_is_the_payload_with_blocking_loaders_all_the_way_down(self) -> None:
        payload = {"message": "done", "target": {"reference": "https://example.com/objects/1", "name": "grid"}}
        with self.catalogue_response(), self.mock_job_client(results=payload):
            result = self.sync_client.geostatistics.declustering.run(source=SOURCE_URL)

        self.assertIsInstance(result, SyncTaskResult)
        self.assertEqual(payload, result)
        self.assertEqual("grid", result.target.name)
        self.assertIsInstance(result.target, SyncResultNode)
        for loader in (result.target.load, result.target.to_dataframe):
            with self.subTest(loader=loader.__name__):
                self.assertFalse(inspect.iscoroutinefunction(loader))

    def test_loading_a_result_blocks_instead_of_returning_a_coroutine(self) -> None:
        payload = {"target": {"reference": "https://example.com/objects/1"}}
        results_schema = {
            "type": "object",
            "properties": {"target": {"type": "object", "output": "geoscience-object"}},
        }
        node = SyncTaskResult(payload, results_schema, self.context)

        async def loaded(context, reference):
            return f"object at {reference}"

        with mock.patch("evo.compute.outputs.object_from_reference", loaded):
            self.assertEqual("object at https://example.com/objects/1", node.target.load())

    def test_a_result_that_refers_to_nothing_loadable_still_says_so(self) -> None:
        node = SyncTaskResult({"target": {"reference": "x"}}, {}, self.context)
        with self.assertRaises(TypeError):
            node.target.load()


class TestFromInsideARunningLoop(unittest.IsolatedAsyncioTestCase):
    """A notebook cell runs inside a running loop, and must not be refused for it.

    ``asyncio.get_running_loop()`` succeeds in a Jupyter cell -- which is why
    ``asyncio.run`` fails there -- so a blanket refusal would rule out the case the
    blocking client exists for. It is safe because the bridge turns its own loop on its own
    thread: blocking the caller's loop cannot starve the work.
    """

    async def test_a_blocking_call_from_a_running_loop_completes(self) -> None:
        self.assertIsNotNone(asyncio.get_running_loop())

        async def work() -> str:
            await asyncio.sleep(0)
            return "finished"

        self.assertEqual("finished", run_sync(work()))

    async def test_the_client_runs_from_a_running_loop_too(self) -> None:
        test = TestSyncComputeClient("test_run_by_name_mirrors_arun")
        test.setUp()
        with test.catalogue_response(), test.mock_job_client() as submit:
            test.sync_client.geostatistics.declustering.run(source=SOURCE_URL)
        self.assertEqual(1, submit.await_count)


class TestALoopBoundContext(unittest.IsolatedAsyncioTestCase):
    """What happens when the context was authenticated with ``await`` instead.

    ``aiohttp`` binds its session to the loop it was opened on, so anything it is still
    waiting on belongs to that loop. Awaiting one of those from the bridge is what produces
    ``Task ... got Future ... attached to a different loop`` -- a ``RuntimeError`` the
    bridge can do nothing about, so the blocking client turns it into advice.
    """

    async def test_a_context_bound_elsewhere_is_refused_with_both_ways_out(self) -> None:
        # Stands in for the session `await manager.login()` opens on the kernel's loop.
        elsewhere = asyncio.get_running_loop().create_future()
        self.addCleanup(elsewhere.cancel)

        async def waits_on_it() -> None:
            await elsewhere

        with self.assertRaises(SyncBridgeError) as caught:
            run_sync(waits_on_it())

        message = str(caught.exception)
        self.assertIn("first used on a different event loop", message)
        self.assertIn("run_sync(manager.login())", message)
        self.assertIn("attached to a different loop", str(caught.exception.__cause__))

    def test_the_same_wait_created_on_the_bridge_is_fine(self) -> None:
        """The anti-vacuous half: the refusal is about where it was opened, not the bridge."""

        async def creates_and_waits() -> str:
            loop = asyncio.get_running_loop()
            here = loop.create_future()
            loop.call_soon(here.set_result, "used")
            return await here

        self.assertEqual("used", run_sync(creates_and_waits()))
        self.assertEqual("used", run_sync(creates_and_waits()))


class TestTheClientsAreSiblings(unittest.TestCase):
    """``ComputeClient`` is untouched, and the two constructors stay in step."""

    def test_the_constructors_take_the_same_arguments(self) -> None:
        self.assertEqual(
            list(inspect.signature(ComputeClient.__init__).parameters),
            list(inspect.signature(SyncComputeClient.__init__).parameters),
        )

    def test_the_blocking_run_mirrors_arun(self) -> None:
        awaited = inspect.signature(ComputeClient.arun)
        blocking = inspect.signature(SyncComputeClient.run)
        self.assertEqual(list(awaited.parameters), list(blocking.parameters))
        self.assertTrue(inspect.iscoroutinefunction(ComputeClient.arun))
        self.assertFalse(inspect.iscoroutinefunction(SyncComputeClient.run))

    def test_the_options_reach_the_client_that_does_the_work(self) -> None:
        context = FakeContext(None, TEST_ORG.id)
        client = SyncComputeClient(context, validate=False, deep_validation=True, check_schemas=True)
        self.assertFalse(client._async._validate)
        self.assertTrue(client._async._deep_validation)
        self.assertTrue(client._async._check_schemas)

    def test_the_blocking_results_are_not_subtypes_of_the_awaited_ones(self) -> None:
        """Code written for a ``ResultNode`` awaits its loaders, which a blocking node cannot honour."""
        self.assertFalse(issubclass(SyncResultNode, ResultNode))
        self.assertFalse(issubclass(SyncTaskResult, TaskResult))

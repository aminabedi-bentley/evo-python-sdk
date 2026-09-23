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

"""A submitted task, between the submission and the results.

The design has the engine "delegate execution to the existing
:class:`~evo.compute.client.JobClient`", and ``arun`` did -- but it built one, awaited it
and dropped it, so everything the platform says about a job *while it runs* went with it.
:class:`TaskJob` is that handle kept::

    job = await client.geostatistics.idw.submit(source=..., target=..., ...)
    print(await job.status())
    result = await job.results()

``arun`` is now submit-then-wait over exactly this, so the one-line path is unchanged and
both share every check. What the handle adds is the rest of the job's life: how far along
it is, stopping it, the id and URL that outlive the process that started it, and control
over how the wait itself is performed.

**Progress belongs to the client, not the call.** The namespace forwards every keyword it
is given to the task's own schema, so a call-level ``fb`` would either be unreachable
through ``client.<topic>.<task>.run(...)`` or have to occupy the name ``fb`` in all of the
catalogue's signatures and both stubs. Setting it once -- ``ComputeClient(context,
fb=...)`` -- costs neither, and is inherited by every job the client submits.
:meth:`TaskJob.results` still takes one, for the caller fanning out across several jobs
who wants :func:`~evo.common.utils.split_feedback` to give each its own slice.

**Where the callbacks run.** On the blocking path :meth:`SyncTaskJob.results` waits on the
bridge, so ``fb.progress`` is called from the bridge's thread rather than the caller's. A
caller who needs the updates on their own thread -- a notebook widget, say -- polls
:meth:`SyncTaskJob.status` instead, which is the reason for handing back a handle rather
than only a feedback hook.
"""

from __future__ import annotations

from typing import Any, Generic, TypeVar, cast
from uuid import UUID

from evo.common import IContext
from evo.common.interfaces import IFeedback
from evo.common.utils import NoFeedback, Retry

from ._sync import run_sync
from .client import JobClient
from .data import JobProgress
from .outputs import SyncTaskResult, TaskResult

__all__ = [
    "SyncTaskJob",
    "TaskJob",
]

TResult = TypeVar("TResult", bound=TaskResult)
TSyncResult = TypeVar("TSyncResult", bound=SyncTaskResult)


class _JobHandle:
    """The submitted job and what its results are hydrated with, which both handles share.

    The lifecycle calls are each handle's own: :class:`TaskJob` awaits them and
    :class:`SyncTaskJob` blocks on them, so neither can stand in for the other and neither
    subclasses the other.
    """

    def __init__(
        self,
        job: JobClient[dict],
        results_schema: dict[str, Any] | None,
        context: IContext,
        fb: IFeedback = NoFeedback,
    ) -> None:
        """
        :param job: The submitted job this is a handle on.
        :param results_schema: The task's published ``results`` schema, which the payload is
            hydrated against. ``None`` for a task that publishes none.
        :param context: The context the job was submitted through, used to load what it wrote.
        :param fb: Where progress is reported while :meth:`results` waits, unless that call
            names another.
        """
        self._job = job
        self._results_schema = results_schema
        self._context = context
        self._fb = fb

    @property
    def id(self) -> UUID:
        """The platform's id for this job."""
        return self._job.id

    @property
    def url(self) -> str:
        """The job's URL, which :meth:`~evo.compute.client.JobClient.from_url` reopens.

        Worth keeping: it is the only thing that survives the process that submitted the job.
        """
        return self._job.url

    @property
    def topic(self) -> str:
        """The topic the task was published under, as the platform spells it."""
        return self._job.topic

    @property
    def task(self) -> str:
        """The task's name, as the platform spells it."""
        return self._job.task

    def __repr__(self) -> str:
        return f"<compute job {self.topic}.{self.task} {self.id}>"

    async def _wait(self, fb: IFeedback | None, polling_interval_seconds: float, retry: Retry | None) -> dict:
        return await self._job.wait_for_results(
            polling_interval_seconds=polling_interval_seconds,
            retry=retry,
            fb=self._fb if fb is None else fb,
        )


class TaskJob(_JobHandle, Generic[TResult]):
    """A task the platform has accepted, before anyone asks it for results.

    Handed back by :meth:`~evo.compute.engine.ComputeClient.asubmit` and by the namespace's
    ``submit(...)``. The task's ``results`` schema travels with it, so the payload is
    hydrated the same way whether it was awaited through :meth:`results` or through ``arun``.
    It is generic over what :meth:`results` produces, so the stub's ``submit`` promises the
    same task-specific result class that ``run`` does.
    """

    async def status(self) -> JobProgress:
        """Ask the platform how far along the job is, without waiting for it.

        :return: The status, a percentage and the platform's own message.
        """
        return await self._job.get_status()

    async def cancel(self) -> None:
        """Ask the platform to stop the job.

        Cancellation is a request: the job moves to ``cancelling`` and reaches ``cancelled``
        in its own time, so a caller who needs to know polls :meth:`status`.
        """
        await self._job.cancel()

    async def results(
        self,
        *,
        fb: IFeedback | None = None,
        polling_interval_seconds: float = 0.5,
        retry: Retry | None = None,
    ) -> TResult:
        """Wait for the job to finish, and hydrate what it returned.

        :param fb: Where to report progress, overriding the client's for this wait.
        :param polling_interval_seconds: How long to wait between status checks.
        :param retry: The wait strategy for a status check that fails. A default is built
            when none is given.

        :return: The results, hydrated against the task's ``results`` schema.

        :raises JobError: If the job failed.
        """
        payload = await self._wait(fb, polling_interval_seconds, retry)
        # The task-specific result classes exist only in the stub; at runtime each is a TaskResult.
        return cast(TResult, TaskResult(payload, self._results_schema, self._context))


class SyncTaskJob(_JobHandle, Generic[TSyncResult]):
    """A submitted task whose lifecycle calls block instead of being awaited.

    What :class:`~evo.compute.engine.SyncComputeClient` hands back, so a job reads the same
    way the call that submitted it did::

        job = client.geostatistics.idw.submit(source=..., target=..., ...)
        print(job.status())
        result = job.results()

    Nothing else changes: the same job, the same schema, the same hydration -- and the
    results arrive as :class:`~evo.compute.outputs.SyncTaskResult`, whose loaders block too.
    """

    @classmethod
    def from_job(cls, job: TaskJob[Any]) -> SyncTaskJob[Any]:
        """Mirror a submitted job, giving its lifecycle blocking calls.

        :param job: The handle the asynchronous engine produced.

        :return: The same job, reached without ``await``.
        """
        return cls(job._job, job._results_schema, job._context, job._fb)

    def status(self) -> JobProgress:
        """Ask the platform how far along the job is, blocking until it answers."""
        return run_sync(self._job.get_status())

    def cancel(self) -> None:
        """Ask the platform to stop the job, blocking until it has been asked."""
        run_sync(self._job.cancel())

    def results(
        self,
        *,
        fb: IFeedback | None = None,
        polling_interval_seconds: float = 0.5,
        retry: Retry | None = None,
    ) -> TSyncResult:
        """Block until the job finishes, and hydrate what it returned.

        ``fb`` is called from the bridge's thread, not the caller's; poll :meth:`status`
        instead when that matters.
        """
        payload = run_sync(self._wait(fb, polling_interval_seconds, retry))
        return cast(TSyncResult, SyncTaskResult(payload, self._results_schema, self._context))

"""Async lifecycle access to a submitted compute job.

``TaskJob`` keeps the platform job, result schema and context together. Submission
does not wait; callers can inspect or cancel the job before collecting hydrated results.
Feedback supplied by the client is used while waiting unless ``results`` overrides it.
"""

from __future__ import annotations

from typing import Any, Generic, TypeVar, cast
from uuid import UUID

from evo.common import IContext
from evo.common.interfaces import IFeedback
from evo.common.utils import NoFeedback, Retry

from .client import JobClient
from .data import JobProgress
from .outputs import TaskResult

__all__ = [
    "TaskJob",
]

TResult = TypeVar("TResult", bound=TaskResult)


class TaskJob(Generic[TResult]):
    """A task accepted by the platform, with async status, cancellation and results.

    :param job: The submitted platform job.
    :param results_schema: The discovery schema used to hydrate its results.
    :param context: The context used to load the objects and attributes it wrote.
    :param fb: Where progress is reported while waiting for results.
    """

    def __init__(
        self,
        job: JobClient[dict],
        results_schema: dict[str, Any] | None,
        context: IContext,
        fb: IFeedback = NoFeedback,
    ) -> None:
        self._job = job
        self._results_schema = results_schema
        self._context = context
        self._fb = fb

    @property
    def id(self) -> UUID:
        """The platform's job identifier."""
        return self._job.id

    @property
    def url(self) -> str:
        """The URL that ``JobClient.from_url`` can reopen."""
        return self._job.url

    @property
    def topic(self) -> str:
        """The topic, as the platform spells it."""
        return self._job.topic

    @property
    def task(self) -> str:
        """The task name, as the platform spells it."""
        return self._job.task

    def __repr__(self) -> str:
        return f"<compute job {self.topic}.{self.task} {self.id}>"

    async def status(self) -> JobProgress:
        """Ask the platform for the current status without waiting for completion."""
        return await self._job.get_status()

    async def cancel(self) -> None:
        """Request cancellation; poll ``status`` to observe its completion."""
        await self._job.cancel()

    async def results(
        self,
        *,
        fb: IFeedback | None = None,
        polling_interval_seconds: float = 0.5,
        retry: Retry | None = None,
    ) -> TResult:
        """Wait for completion and hydrate the payload against the result schema.

        :param fb: Override the client's feedback for this wait.
        :param polling_interval_seconds: Delay between status checks.
        :param retry: Retry strategy for failed status checks; ``None`` uses the default.

        :raises JobError: If the platform job failed.
        """
        payload = await self._job.wait_for_results(
            polling_interval_seconds=polling_interval_seconds,
            retry=retry,
            fb=self._fb if fb is None else fb,
        )
        return cast(TResult, TaskResult(payload, self._results_schema, self._context))

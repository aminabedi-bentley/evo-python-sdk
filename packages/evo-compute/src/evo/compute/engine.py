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

"""Discovery-driven generic compute engine.

:class:`ComputeClient` is an instance-bound entry point to every compute task an
organization can run. It synthesises a topic/task namespace on the fly and, when
a task is run, reads that task's schema from the live discovery catalogue to shape
and submit the job::

    client = ComputeClient(context)
    result = await client.geostatistics.kriging.run(source=..., target=..., ...)

A call is checked, resolved, checked again and submitted: required parameters first
(before anything reaches the network), then :mod:`~evo.compute.resolution` turns the
caller's objects and attributes into the references the schema declares, then optional
deep validation runs on that resolved payload -- the form the platform actually receives.
What comes back is hydrated against the task's ``results`` schema by
:mod:`~evo.compute.outputs`, so the objects a task wrote can be loaded straight back.

:class:`SyncComputeClient` is the same thing without the ``await``, for a plain script or
a notebook cell. It wraps a :class:`ComputeClient` and blocks on the bridge in
:mod:`evo.compute._sync`; which of the two you use is an explicit choice, not a mode.

Discovery is performed the first time a task within a topic is ``run(...)``. The
catalogue is then held by the underlying :class:`~evo.compute.discovery.DiscoveryClient`,
so repeated runs are served from there until its cache expires.

``run(...)`` waits for the task to finish. ``submit(...)`` does not: it hands back the
:class:`~evo.compute.jobs.TaskJob` the run would otherwise have waited on, which is what
progress reporting, cancellation and fanning out are all reached through. ``run`` is
defined as ``submit`` followed by :meth:`~evo.compute.jobs.TaskJob.results`, so neither
path can drift from the other.

A task can opt out of the synthesised surface entirely -- see :mod:`evo.compute.overrides`.
``arun`` and ``asubmit`` always take the generic path, whether or not the task has an
override; an override publishes whatever surface it chooses, and the one in the tree today
publishes ``run`` alone.
"""

from __future__ import annotations

import inspect
import keyword
from typing import Any, ClassVar, Literal, Optional, Union
from uuid import UUID

from evo.common import APIConnector, IContext
from evo.common.interfaces import IFeedback
from evo.common.utils import NoFeedback

from ._sync import run_sync
from .client import JobClient
from .discovery import DEFAULT_CACHE_TTL_SECONDS, DiscoveryClient
from .endpoints.models import TaskResource
from .exceptions import ParameterValidationError
from .jobs import SyncTaskJob, TaskJob
from .outputs import SyncTaskResult, TaskResult
from .overrides import load_override
from .resolution import ReferenceResolver
from .validation import validate_parameters

__all__ = [
    "ComputeClient",
    "SyncComputeClient",
]


_JSON_SCHEMA_TO_PYTHON: dict[str, type] = {
    "string": str,
    "integer": int,
    "number": float,
    "boolean": bool,
    "object": dict,
    "array": list,
}


def _normalise(name: str) -> str:
    """Map a platform topic or task name to a Python identifier (``normal-score`` -> ``normal_score``).

    Applied to topics as well as tasks: ``cpt-to-borehole`` is not reachable as an attribute
    otherwise, and the wire still gets the platform's own spelling from the discovered spec.
    """
    return name.replace("-", "_")


def _python_annotation(prop: dict[str, Any]) -> Any:
    """Best-effort Python annotation for a JSON-Schema property."""
    if "enum" in prop:
        return Literal[tuple(prop["enum"])]  # type: ignore[misc]
    json_type = prop.get("type", "")
    if isinstance(json_type, list):
        members = [_JSON_SCHEMA_TO_PYTHON[t] for t in json_type if t != "null" and t in _JSON_SCHEMA_TO_PYTHON]
        base = members[0] if len(members) == 1 else (Union[tuple(members)] if members else Any)
        return Optional[base] if "null" in json_type else base
    return _JSON_SCHEMA_TO_PYTHON.get(json_type, Any)


def _wire_names(spec: TaskResource) -> dict[str, str]:
    """Map the keyword a caller types to the property name the platform published.

    Parameters get the same treatment as topics and tasks: ``vis-service`` is reachable as
    ``vis_service`` and its ``s2s-user-info`` parameter as ``s2s_user_info``, while the wire
    keeps the platform's own spelling.

    Empty when the schema cannot be expressed as keyword-only parameters at all -- because two
    properties normalise onto one name, or one normalises onto a Python keyword. ``run`` then
    takes ``**parameters`` and forwards the published names verbatim, which is what the stub
    already promises for these tasks.
    """
    schema = spec.parameters or {}
    properties: dict[str, Any] = schema.get("properties") or {}
    required = list(schema.get("required") or [])
    published = [*required, *(name for name in properties if name not in required)]

    names = {_normalise(name): name for name in published}
    if len(names) != len(published) or not all(name.isidentifier() and not keyword.iskeyword(name) for name in names):
        return {}
    return names


def _signature_from_schema(spec: TaskResource, returns: type = TaskResult) -> inspect.Signature:
    """Synthesise a keyword-only ``run(...)`` signature from a task's parameter schema.

    Required parameters come first, then optional ones (defaulting to their schema
    default or ``None``), then the engine's own ``preview`` flag.

    :param returns: What the call hands back: a result class for ``run``, a job class for
        ``submit``, each differing again between the awaited and the blocking client.
    """
    schema = spec.parameters or {}
    properties: dict[str, Any] = schema.get("properties", {})
    required = list(schema.get("required", []))
    published = {wire: name for name, wire in _wire_names(spec).items()}

    parameters: list[inspect.Parameter] = []
    if published:
        for name in required:
            parameters.append(
                inspect.Parameter(
                    published[name],
                    inspect.Parameter.KEYWORD_ONLY,
                    annotation=_python_annotation(properties.get(name, {})),
                )
            )
        for name, prop in properties.items():
            if name in required:
                continue
            parameters.append(
                inspect.Parameter(
                    published[name],
                    inspect.Parameter.KEYWORD_ONLY,
                    default=prop.get("default", None),
                    annotation=_python_annotation(prop),
                )
            )
    # ``preview`` defaults to opting in for tasks gated behind a feature flag.
    parameters.append(
        inspect.Parameter("preview", inspect.Parameter.KEYWORD_ONLY, default=bool(spec.feature_flag), annotation=bool)
    )
    if not published and properties:
        # Nothing nameable to bind against, so take the published spellings as they come.
        # Last, because ``inspect`` rejects any parameter after a var-keyword.
        parameters.append(inspect.Parameter("parameters", inspect.Parameter.VAR_KEYWORD, annotation=Any))
    return inspect.Signature(parameters, return_annotation=returns)


class ComputeClient:
    """Instance-bound async entry point to the compute task catalogue.

    :param context: An authenticated Evo context.
    :param cache_ttl_seconds: How long a discovered task catalogue is cached.
    :param validate: Check the parameters against the task's JSON Schema before submitting
        (required-field presence, and, under ``deep_validation``, the whole payload).
        Defaults to ``True``. ``False`` turns off deep validation too, whatever
        ``deep_validation`` says. It does *not* make the call unchecked: the parameters are
        still bound to the signature synthesised from the schema, so an unknown or missing
        required argument is still rejected. That is how the payload is built, not a
        validation pass, and there is no useful call to make without it.
    :param deep_validation: Additionally run full JSON Schema Draft 2020-12 validation.
        Defaults to ``False``. Only consulted when ``validate`` is ``True``.
    :param check_schemas: Check that each referenced geoscience object is of a schema the
        task declares support for. Defaults to following ``validate``. Independent of it
        because the cost and the question differ: schema validation is local and free,
        while this loads each referenced object's metadata. Pass ``False`` to keep
        validation but skip the requests, or ``True`` alongside ``validate=False`` to keep
        the guard that catches an object the task cannot read.
    :param fb: Where every job this client submits reports its progress while it is waited
        on. Set here rather than per call because the namespace forwards its keywords to
        the task's own schema, so there is nowhere in ``run(...)`` for an engine-level
        argument to live that would not also take a name away from every task.
        :meth:`~evo.compute.jobs.TaskJob.results` overrides it for one wait.
    """

    _result_type: ClassVar[type[TaskResult]] = TaskResult
    _job_type: ClassVar[type[TaskJob]] = TaskJob

    def __init__(
        self,
        context: IContext,
        *,
        cache_ttl_seconds: float = DEFAULT_CACHE_TTL_SECONDS,
        validate: bool = True,
        deep_validation: bool = False,
        check_schemas: bool | None = None,
        fb: IFeedback = NoFeedback,
    ) -> None:
        self._context = context
        self._org_id: UUID = context.get_org_id()
        self._connector: APIConnector = context.get_connector()
        self._discovery = DiscoveryClient(self._connector, self._org_id, cache_ttl_seconds=cache_ttl_seconds)
        self._resolver = ReferenceResolver(context)
        self._validate = validate
        self._deep_validation = deep_validation
        self._check_schemas = check_schemas
        self._fb = fb

    # -- dynamic namespace ------------------------------------------------- #

    def __getattr__(self, name: str) -> _TopicProxy:
        # Only fires for names not found normally. Private/dunder probes must raise.
        if name.startswith("_"):
            raise AttributeError(name)
        return _TopicProxy(self, name)

    def __dir__(self) -> list[str]:
        return sorted(
            set(super().__dir__()) | {_normalise(resource.topic) for resource in self._discovery.peek_tasks()}
        )

    def __repr__(self) -> str:
        return f"ComputeClient(org_id={str(self._org_id)!r})"

    @property
    def context(self) -> IContext:
        """The context this client runs against, for overrides that build their own results."""
        return self._context

    # -- non-blocking reads of the discovery cache -------------------------- #

    def _peek_spec(self, topic: str, task: str) -> TaskResource | None:
        """Return an already-discovered spec, or ``None`` while the catalogue is unfetched or stale."""
        normalised = _normalise(task)
        for resource in self._discovery.peek_tasks():
            if _normalise(resource.topic) == _normalise(topic) and _normalise(resource.name) == normalised:
                return resource
        return None

    # -- execution --------------------------------------------------------- #

    async def arun(
        self,
        topic: str,
        task: str,
        parameters: dict[str, Any],
        *,
        validate: bool | None = None,
        deep_validation: bool | None = None,
        check_schemas: bool | None = None,
    ) -> TaskResult:
        """Submit the task and wait for it, hydrating the results it returned.

        :meth:`asubmit` does the work; this waits on what it hands back, with the client's
        own feedback and the default polling. Take the job instead when the wait itself
        matters -- to report progress elsewhere, to poll, or to cancel.

        :param validate: Override the client's schema-validation setting for this call.
            ``False`` skips deep validation too, but never the signature binding that
            builds the payload.
        :param deep_validation: Override the client's deep-validation setting for this call.
            Only consulted when validation is enabled.
        :param check_schemas: Override the client's supported-schema setting for this call.
            Falls back to ``validate`` when neither is set.
        """
        job = await self._submit(
            topic,
            task,
            parameters,
            "run",
            validate=validate,
            deep_validation=deep_validation,
            check_schemas=check_schemas,
        )
        return await job.results()

    async def asubmit(
        self,
        topic: str,
        task: str,
        parameters: dict[str, Any],
        *,
        validate: bool | None = None,
        deep_validation: bool | None = None,
        check_schemas: bool | None = None,
    ) -> TaskJob[TaskResult]:
        """Discover the task (cached), resolve and validate the parameters, and submit.

        Everything :meth:`arun` does except the waiting, which is left to the caller
        through the returned handle.

        :param validate: Override the client's schema-validation setting for this call.
            ``False`` skips deep validation too, but never the signature binding that
            builds the payload.
        :param deep_validation: Override the client's deep-validation setting for this call.
            Only consulted when validation is enabled.
        :param check_schemas: Override the client's supported-schema setting for this call.
            Falls back to ``validate`` when neither is set.

        :return: A handle on the accepted job, carrying the task's ``results`` schema so
            the payload is hydrated the same way either path reaches it.
        """
        return await self._submit(
            topic,
            task,
            parameters,
            "submit",
            validate=validate,
            deep_validation=deep_validation,
            check_schemas=check_schemas,
        )

    async def _submit(
        self,
        topic: str,
        task: str,
        parameters: dict[str, Any],
        operation: str,
        *,
        validate: bool | None,
        deep_validation: bool | None,
        check_schemas: bool | None,
    ) -> TaskJob[TaskResult]:
        """The work :meth:`arun` and :meth:`asubmit` share; ``operation`` names the call in a binding error."""
        spec = await self._resolve_spec(topic, task)
        label = f"{topic}.{_normalise(task)}"
        signature = _signature_from_schema(spec)
        try:
            bound = signature.bind(**parameters)
        except TypeError as error:
            raise ParameterValidationError(f"{label}.{operation}(): {error}", task=label, errors=[str(error)]) from None

        # Forward only what the caller actually passed, so unset optionals fall back to the
        # platform's own defaults while an explicit ``None`` still reaches the wire.
        arguments = dict(bound.arguments)
        preview = bool(arguments.pop("preview", signature.parameters["preview"].default))
        # Back to the platform's own spelling, which is all the wire has ever known.
        published = _wire_names(spec)
        wire_parameters = (
            {published[name]: value for name, value in arguments.items()}
            if published
            else dict(arguments.pop("parameters", {}))
        )

        if validate is None:
            validate = self._validate
        if deep_validation is None:
            deep_validation = self._deep_validation
        if check_schemas is None:
            check_schemas = self._check_schemas if self._check_schemas is not None else validate
        # ``validate`` masters the schema checks; ``check_schemas`` stands on its own.
        if validate:
            # Required fields first: a missing parameter is worth reporting before resolution
            # spends a request loading the objects the other parameters name.
            validate_parameters(spec, wire_parameters, task_label=label)
        wire_parameters = await self._resolver.resolve(
            spec, wire_parameters, check_schemas=check_schemas, task_label=label
        )
        if validate and deep_validation:
            validate_parameters(spec, wire_parameters, deep=True, task_label=label)

        job: JobClient[dict] = await JobClient.submit(
            connector=self._connector,
            org_id=self._org_id,
            topic=spec.topic,
            task=spec.name,
            parameters=wire_parameters,
            result_type=dict,
            preview=preview,
        )
        return TaskJob(job, spec.results, self._context, self._fb)

    async def _resolve_spec(self, topic: str, task: str) -> TaskResource:
        """Return the discovery spec for ``topic``/``task``.

        The catalogue lives in :class:`~evo.compute.discovery.DiscoveryClient`, which fetches
        it once and serves it from there until its TTL expires.
        """
        normalised = _normalise(task)
        normalised_topic = _normalise(topic)
        topic_tasks = await self._discovery.get_topic_tasks(topic)
        if not topic_tasks:
            # `vis-service` is not spellable as an attribute, so `vis_service` has to find it.
            # Same cached catalogue either way: this costs a scan, not a request.
            topic_tasks = [
                resource
                for resource in await self._discovery.list_tasks()
                if _normalise(resource.topic) == normalised_topic
            ]
        for resource in topic_tasks:
            if _normalise(resource.name) == normalised:
                return resource

        available = ", ".join(sorted(_normalise(resource.name) for resource in topic_tasks)) or "(none)"
        raise AttributeError(f"no task {task!r} in topic {topic!r}. Available: {available}")

    def _make_run(self, topic: str, task: str) -> Any:
        """Build the awaitable ``run`` callable for a task proxy.

        If the task's schema has already been discovered, the callable advertises a synthesised
        signature for editor tab-completion. Otherwise it accepts generic keyword arguments and the
        schema is fetched on first call, after which the callable re-describes itself. Either way
        discovery is never triggered by attribute access alone.
        """

        async def run(**parameters: Any) -> TaskResult:
            try:
                return await self.arun(topic, task, parameters)
            finally:
                # The first call populates the catalogue, so this callable can now describe itself.
                # Safe to repeat: it recomputes the same shape, or picks up a newer spec after a refresh.
                _describe_from_schema(run, self, topic, task, self._result_type)

        run.__name__ = "run"
        run.__qualname__ = f"{_normalise(task)}.run"
        _describe_from_schema(run, self, topic, task, self._result_type)
        return run

    def _make_submit(self, topic: str, task: str) -> Any:
        """Build the awaitable ``submit`` callable for a task proxy.

        The same parameters as ``run``, described from the same schema, differing only in
        what comes back: the job rather than what it eventually produces.
        """

        async def submit(**parameters: Any) -> TaskJob[TaskResult]:
            try:
                return await self.asubmit(topic, task, parameters)
            finally:
                _describe_from_schema(submit, self, topic, task, self._job_type)

        submit.__name__ = "submit"
        submit.__qualname__ = f"{_normalise(task)}.submit"
        _describe_from_schema(submit, self, topic, task, self._job_type)
        return submit

    def _bind_override(self, override: Any, topic: str, task: str) -> Any:
        """Hand a task to its override. The runner awaits, as every other call here does."""
        return override.bind(self, topic, task)


class _BlockingRunner:
    """A hand-written runner with its ``run`` blocked, for :class:`SyncComputeClient`.

    An override answers with its own runner, which awaits like everything else in the engine.
    Only the call itself is wrapped: what it returns is the override's own typed result, whose
    members stay awaitable on both paths. :func:`~evo.compute.run_sync` is there for those.
    """

    def __init__(self, runner: Any) -> None:
        self._runner = runner

    def run(self, **parameters: Any) -> Any:
        return run_sync(self._runner.run(**parameters))

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        return getattr(self._runner, name)

    def __dir__(self) -> list[str]:
        return sorted(set(super().__dir__()) | set(dir(self._runner)))

    def __repr__(self) -> str:
        return repr(self._runner)


class SyncComputeClient:
    """Blocking sibling of :class:`ComputeClient`, for scripts and notebooks.

    The same catalogue, the same namespace, the same checks -- with the ``await`` removed::

        client = SyncComputeClient(context)
        result = client.geostatistics.kriging.run(source=..., target=..., ...)
        grid = result.target.load()

    It wraps a :class:`ComputeClient` and runs its coroutines on the bridge described in
    :mod:`evo.compute._sync`, so nothing about validation, resolution or hydration differs
    between the two paths. The results come back as :class:`~evo.compute.outputs.SyncTaskResult`,
    whose loaders block in turn.

    The context must be authenticated on the same bridge -- :func:`~evo.compute.run_sync`
    around ``manager.login()`` rather than ``await`` -- because a connection is bound to the
    event loop it was opened on. A context opened elsewhere raises
    :class:`~evo.compute.exceptions.SyncBridgeError` saying so on the first call.

    Not a context manager, consistent with :class:`ComputeClient`. The constructor takes the
    same arguments and passes them straight through.
    """

    _result_type: ClassVar[type[SyncTaskResult]] = SyncTaskResult
    _job_type: ClassVar[type[SyncTaskJob]] = SyncTaskJob

    def __init__(
        self,
        context: IContext,
        *,
        cache_ttl_seconds: float = DEFAULT_CACHE_TTL_SECONDS,
        validate: bool = True,
        deep_validation: bool = False,
        check_schemas: bool | None = None,
        fb: IFeedback = NoFeedback,
    ) -> None:
        self._async = ComputeClient(
            context,
            cache_ttl_seconds=cache_ttl_seconds,
            validate=validate,
            deep_validation=deep_validation,
            check_schemas=check_schemas,
            fb=fb,
        )

    # -- dynamic namespace ------------------------------------------------- #

    def __getattr__(self, name: str) -> _TopicProxy:
        # Only fires for names not found normally. Private/dunder probes must raise.
        if name.startswith("_"):
            raise AttributeError(name)
        return _TopicProxy(self, name)

    def __dir__(self) -> list[str]:
        return sorted(
            set(super().__dir__()) | {_normalise(resource.topic) for resource in self._discovery.peek_tasks()}
        )

    def __repr__(self) -> str:
        return f"SyncComputeClient(org_id={str(self._async._org_id)!r})"

    # -- what the shared proxies read -------------------------------------- #

    @property
    def _discovery(self) -> DiscoveryClient:
        return self._async._discovery

    def _peek_spec(self, topic: str, task: str) -> TaskResource | None:
        return self._async._peek_spec(topic, task)

    # -- execution --------------------------------------------------------- #

    def run(
        self,
        topic: str,
        task: str,
        parameters: dict[str, Any],
        *,
        validate: bool | None = None,
        deep_validation: bool | None = None,
        check_schemas: bool | None = None,
    ) -> SyncTaskResult:
        """Run a task by name and block until it finishes.

        The blocking counterpart of :meth:`ComputeClient.arun`, and defined the same way:
        :meth:`submit`, then wait on what it hands back.

        :return: The task's results, with blocking loaders.

        :raises SyncBridgeError: If called from inside the bridge's own event loop, or if
            the context belongs to a different one.
        """
        job = self._submit(
            topic,
            task,
            parameters,
            "run",
            validate=validate,
            deep_validation=deep_validation,
            check_schemas=check_schemas,
        )
        return job.results()

    def submit(
        self,
        topic: str,
        task: str,
        parameters: dict[str, Any],
        *,
        validate: bool | None = None,
        deep_validation: bool | None = None,
        check_schemas: bool | None = None,
    ) -> SyncTaskJob[SyncTaskResult]:
        """Submit a task by name without waiting for it.

        The blocking counterpart of :meth:`ComputeClient.asubmit`, which does the work.

        :return: A handle on the accepted job, whose lifecycle calls block in turn.

        :raises SyncBridgeError: If called from inside the bridge's own event loop, or if
            the context belongs to a different one.
        """
        return self._submit(
            topic,
            task,
            parameters,
            "submit",
            validate=validate,
            deep_validation=deep_validation,
            check_schemas=check_schemas,
        )

    def _submit(
        self,
        topic: str,
        task: str,
        parameters: dict[str, Any],
        operation: str,
        *,
        validate: bool | None,
        deep_validation: bool | None,
        check_schemas: bool | None,
    ) -> SyncTaskJob[SyncTaskResult]:
        return SyncTaskJob.from_job(
            run_sync(
                self._async._submit(
                    topic,
                    task,
                    parameters,
                    operation,
                    validate=validate,
                    deep_validation=deep_validation,
                    check_schemas=check_schemas,
                )
            )
        )

    def _make_run(self, topic: str, task: str) -> Any:
        """Build the blocking ``run`` callable for a task proxy.

        Described from the schema exactly as the awaited one is, so the two namespaces read
        the same in an editor.
        """

        def run(**parameters: Any) -> SyncTaskResult:
            try:
                return self.run(topic, task, parameters)
            finally:
                _describe_from_schema(run, self, topic, task, self._result_type)

        run.__name__ = "run"
        run.__qualname__ = f"{_normalise(task)}.run"
        _describe_from_schema(run, self, topic, task, self._result_type)
        return run

    def _make_submit(self, topic: str, task: str) -> Any:
        """Build the blocking ``submit`` callable for a task proxy."""

        def submit(**parameters: Any) -> SyncTaskJob[SyncTaskResult]:
            try:
                return self.submit(topic, task, parameters)
            finally:
                _describe_from_schema(submit, self, topic, task, self._job_type)

        submit.__name__ = "submit"
        submit.__qualname__ = f"{_normalise(task)}.submit"
        _describe_from_schema(submit, self, topic, task, self._job_type)
        return submit

    def _bind_override(self, override: Any, topic: str, task: str) -> _BlockingRunner:
        """Hand a task to its override, bound to the engine underneath and blocked on the bridge."""
        return _BlockingRunner(override.bind(self._async, topic, task))


class _TopicProxy:
    """A single topic within the catalogue; resolves attribute access to task proxies."""

    def __init__(self, client: ComputeClient | SyncComputeClient, topic: str) -> None:
        self._client = client
        self._topic = topic

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        # A task with an override is handed to it whole; the rest stay generic.
        if (override := load_override(self._topic, _normalise(name))) is not None:
            return self._client._bind_override(override, self._topic, name)
        return _TaskProxy(self._client, self._topic, name)

    def __dir__(self) -> list[str]:
        return sorted(
            set(super().__dir__())
            | {
                _normalise(resource.name)
                for resource in self._client._discovery.peek_tasks()
                if _normalise(resource.topic) == _normalise(self._topic)
            }
        )

    def __repr__(self) -> str:
        return f"<compute topic {self._topic!r}>"


class _TaskProxy:
    """A single task; exposes a schema-shaped ``run(...)`` and ``submit(...)``, awaitable or blocking."""

    def __init__(self, client: ComputeClient | SyncComputeClient, topic: str, task: str) -> None:
        self._client = client
        self._topic = topic
        self._task = task
        self.run = client._make_run(topic, task)
        self.submit = client._make_submit(topic, task)

    def __dir__(self) -> list[str]:
        return sorted(set(super().__dir__()) | {"run", "submit"})

    def __repr__(self) -> str:
        return f"<compute task {self._topic!r}.{_normalise(self._task)!r}>"


def _describe_from_schema(
    call: Any, client: ComputeClient | SyncComputeClient, topic: str, task: str, returns: type
) -> None:
    """Shape ``call``'s signature and docstring from the task schema, if it has been discovered."""
    if (spec := client._peek_spec(topic, task)) is not None:
        call.__signature__ = _signature_from_schema(spec, returns)
        call.__doc__ = spec.description or call.__doc__

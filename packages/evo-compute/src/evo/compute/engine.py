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

"""Discovery-driven generic compute engine (async).

:class:`ComputeClient` is an instance-bound entry point to every compute task an
organization can run. It synthesises a topic/task namespace on the fly and, when
a task is run, reads that task's schema from the live discovery catalogue to shape
and submit the job::

    client = ComputeClient(context)
    result = await client.geostatistics.kriging_gcp.run(source=..., target=..., ...)

Discovery is performed the first time a task within a topic is ``run(...)``; the
catalogue is then cached in memory so repeated runs don't re-fetch it.
"""

from __future__ import annotations

import inspect
from typing import Any, Literal, Optional, Union
from uuid import UUID

from evo.common import APIConnector, IContext

from .client import JobClient
from .discovery import DEFAULT_CACHE_TTL_SECONDS, DiscoveryClient
from .endpoints.models import TaskResource

__all__ = [
    "ComputeClient",
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
    """Map a platform task name to a Python identifier (``kriging-gcp`` -> ``kriging_gcp``)."""
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


def _signature_from_schema(spec: TaskResource) -> inspect.Signature:
    """Synthesise a keyword-only ``run(...)`` signature from a task's parameter schema.

    Required parameters come first, then optional ones (defaulting to their schema
    default or ``None``), then the engine's own ``preview`` flag.
    """
    schema = spec.parameters or {}
    properties: dict[str, Any] = schema.get("properties", {})
    required = list(schema.get("required", []))

    parameters: list[inspect.Parameter] = []
    for name in required:
        parameters.append(
            inspect.Parameter(
                name, inspect.Parameter.KEYWORD_ONLY, annotation=_python_annotation(properties.get(name, {}))
            )
        )
    for name, prop in properties.items():
        if name in required:
            continue
        parameters.append(
            inspect.Parameter(
                name,
                inspect.Parameter.KEYWORD_ONLY,
                default=prop.get("default", None),
                annotation=_python_annotation(prop),
            )
        )
    # ``preview`` defaults to opting in for tasks gated behind a feature flag.
    parameters.append(
        inspect.Parameter("preview", inspect.Parameter.KEYWORD_ONLY, default=bool(spec.feature_flag), annotation=bool)
    )
    return inspect.Signature(parameters, return_annotation=dict)


class ComputeClient:
    """Instance-bound async entry point to the compute task catalogue.

    :param context: An authenticated Evo context.
    :param cache_ttl_seconds: How long a discovered task catalogue is cached.
    """

    def __init__(self, context: IContext, *, cache_ttl_seconds: float = DEFAULT_CACHE_TTL_SECONDS) -> None:
        self._context = context
        self._org_id: UUID = context.get_org_id()
        self._connector: APIConnector = context.get_connector()
        self._discovery = DiscoveryClient(self._connector, self._org_id, cache_ttl_seconds=cache_ttl_seconds)
        self._spec_cache: dict[tuple[str, str], TaskResource] = {}

    # -- dynamic namespace ------------------------------------------------- #

    def __getattr__(self, name: str) -> _TopicProxy:
        # Only fires for names not found normally. Private/dunder probes must raise.
        if name.startswith("_"):
            raise AttributeError(name)
        return _TopicProxy(self, name)

    def __dir__(self) -> list[str]:
        return sorted(set(super().__dir__()) | set(self._cached_topics()))

    def __repr__(self) -> str:
        return f"ComputeClient(org_id={str(self._org_id)!r})"

    # -- in-memory catalogue cache ----------------------------------------- #

    def _cached_spec(self, topic: str, task: str) -> TaskResource | None:
        return self._spec_cache.get((topic, _normalise(task)))

    def _cached_topics(self) -> list[str]:
        return sorted({topic for topic, _ in self._spec_cache})

    def _cached_tasks(self, topic: str) -> list[str]:
        return sorted(task for cached_topic, task in self._spec_cache if cached_topic == topic)

    # -- execution --------------------------------------------------------- #

    async def arun(self, topic: str, task: str, parameters: dict[str, Any]) -> dict:
        """Discover the task (cached), submit it, and return the results."""
        spec = await self._resolve_spec(topic, task)
        try:
            bound = _signature_from_schema(spec).bind(**parameters)
        except TypeError as error:
            raise TypeError(f"{topic}.{_normalise(task)}.run(): {error}") from None
        bound.apply_defaults()

        arguments = dict(bound.arguments)
        preview = bool(arguments.pop("preview"))
        # Omit unset optional parameters so the platform applies its own defaults.
        wire_parameters = {name: value for name, value in arguments.items() if value is not None}

        job: JobClient[dict] = await JobClient.submit(
            connector=self._connector,
            org_id=self._org_id,
            topic=spec.topic,
            task=spec.name,
            parameters=wire_parameters,
            result_type=dict,
            preview=preview,
        )
        return await job.wait_for_results()

    async def _resolve_spec(self, topic: str, task: str) -> TaskResource:
        """Return the discovery spec for ``topic``/``task``, fetching once and caching it."""
        normalised = _normalise(task)
        if (cached := self._cached_spec(topic, task)) is not None:
            return cached

        topic_tasks = await self._discovery.get_topic_tasks(topic)
        for resource in topic_tasks:
            self._spec_cache[(topic, _normalise(resource.name))] = resource
        spec = self._spec_cache.get((topic, normalised))

        if spec is None:
            available = ", ".join(sorted(_normalise(resource.name) for resource in topic_tasks)) or "(none)"
            raise AttributeError(f"no task {task!r} in topic {topic!r}. Available: {available}")
        return spec


class _TopicProxy:
    """A single topic within the catalogue; resolves attribute access to task proxies."""

    def __init__(self, client: ComputeClient, topic: str) -> None:
        self._client = client
        self._topic = topic

    def __getattr__(self, name: str) -> _TaskProxy:
        if name.startswith("_"):
            raise AttributeError(name)
        return _TaskProxy(self._client, self._topic, name)

    def __dir__(self) -> list[str]:
        return sorted(set(super().__dir__()) | set(self._client._cached_tasks(self._topic)))

    def __repr__(self) -> str:
        return f"<compute topic {self._topic!r}>"


class _TaskProxy:
    """A single task; exposes a schema-shaped async ``run(...)``."""

    def __init__(self, client: ComputeClient, topic: str, task: str) -> None:
        self._client = client
        self._topic = topic
        self._task = task
        self.run = _make_run(client, topic, task)

    def __dir__(self) -> list[str]:
        return sorted(set(super().__dir__()) | {"run"})

    def __repr__(self) -> str:
        return f"<compute task {self._topic!r}.{_normalise(self._task)!r}>"


def _make_run(client: ComputeClient, topic: str, task: str):
    """Build the async ``run`` callable for a task proxy.

    If the task's schema is already cached, the callable advertises a synthesised
    signature for editor tab-completion. Otherwise it accepts generic keyword arguments
    and the schema is fetched on first call. Either way discovery is never triggered
    by attribute access alone.
    """

    async def run(**parameters: Any) -> dict:
        return await client.arun(topic, task, parameters)

    run.__name__ = "run"
    run.__qualname__ = f"{_normalise(task)}.run"
    if (spec := client._cached_spec(topic, task)) is not None:
        run.__signature__ = _signature_from_schema(spec)  # type: ignore[attr-defined]
        run.__doc__ = spec.description or run.__doc__
    return run

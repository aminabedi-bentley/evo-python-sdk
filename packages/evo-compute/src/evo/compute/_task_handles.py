from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import Mapping
from dataclasses import dataclass, field
from importlib.resources import files
from typing import Any, Generic, TypeVar, cast
from uuid import UUID

from evo.common import APIConnector, IContext
from pydantic import BaseModel

_CLIENTS_ATTRIBUTE = "_evo_compute_task_clients"
_Run = TypeVar("_Run")
_Arun = TypeVar("_Arun")


def _registered_exports() -> dict[str, dict[str, str]]:
    from .tasks.common.runner import TaskRegistry

    exports: dict[str, dict[str, str]] = {}
    for runner in TaskRegistry().registered_runners():
        if runner.__module__.startswith("evo.compute.tasks."):
            exports.setdefault(runner.topic, {})[runner.__name__.removesuffix("Runner")] = runner.task
    return exports


def _catalogue() -> dict[str, dict[str, str]]:
    catalogue = json.loads(files(__package__).joinpath("_task_catalogue.json").read_text(encoding="utf-8"))
    for topic, names in _registered_exports().items():
        target = catalogue.setdefault(topic, {})
        for name, identity in names.items():
            target.setdefault(name, identity)
    return catalogue


def _export_names(topic: str | None = None) -> list[str]:
    catalogue = _catalogue()
    if topic is not None:
        return sorted(catalogue.get(topic, {}))
    locations: dict[str, set[tuple[str, str]]] = {}
    for topic_name, names in catalogue.items():
        for name, identity in names.items():
            locations.setdefault(name, set()).add((topic_name, identity))
    return sorted(name for name, identities in locations.items() if len(identities) == 1)


def _import_task(name: str, topic: str | None = None) -> TaskHandle[Any, Any]:
    if name.startswith("_"):
        raise AttributeError(name)
    locations = [
        (topic_name, names[name])
        for topic_name, names in _catalogue().items()
        if name in names and (topic is None or topic_name == topic)
    ]
    if len(locations) == 1:
        return task(*locations[0])
    if locations:
        raise AttributeError(f"Task {name!r} is ambiguous across topics; use task(topic, name)")
    raise AttributeError(f"No imported task named {name!r}; use task(topic, name) for a newly published task")


def task(topic: str, name: str) -> TaskHandle[Any, Any]:
    """Identify any live task, including one absent from the shipped import index."""
    if not isinstance(topic, str) or not topic or not isinstance(name, str) or not name:
        raise ValueError("topic and name must be non-empty strings")
    return TaskHandle(topic, name)


@dataclass
class _Clients:
    connector: APIConnector
    org_id: UUID
    lock: threading.RLock = field(default_factory=threading.RLock)
    blocking: Any = None
    asynchronous: Any = None
    loop: asyncio.AbstractEventLoop | None = None


def _client_for(context: IContext, *, blocking: bool) -> Any:
    from .engine import ComputeClient, SyncComputeClient

    connector, org_id = context.get_connector(), context.get_org_id()
    try:
        namespace = vars(context)
    except TypeError:
        clients = _Clients(connector, org_id)
    else:
        clients = namespace.setdefault(_CLIENTS_ATTRIBUTE, _Clients(connector, org_id))
        if not isinstance(clients, _Clients):
            clients = namespace[_CLIENTS_ATTRIBUTE] = _Clients(connector, org_id)
    with clients.lock:
        if (
            clients.connector.base_url != connector.base_url
            or clients.connector.transport is not connector.transport
            or clients.connector._authorizer is not connector._authorizer
            or clients.connector._additional_headers != connector._additional_headers
            or clients.org_id != org_id
        ):
            clients.connector, clients.org_id = connector, org_id
            clients.blocking = clients.asynchronous = clients.loop = None
        if blocking:
            if clients.blocking is None:
                clients.blocking = SyncComputeClient(context)
            return clients.blocking
        loop = asyncio.get_running_loop()
        if clients.asynchronous is None or clients.loop is not loop:
            clients.asynchronous = ComputeClient(context)
            clients.loop = loop
        return clients.asynchronous


def _parameters(
    parameters: Mapping[str, Any] | BaseModel | None,
    named: dict[str, Any],
    preview: bool | None,
    *,
    native: bool = False,
) -> dict[str, Any]:
    if parameters is not None and named:
        raise TypeError("Pass a parameter model or mapping, or named task arguments, not both")
    if parameters is None:
        payload = dict(named)
    elif isinstance(parameters, BaseModel):
        if native:
            payload = {
                name: value
                for name in type(parameters).model_fields
                if (value := getattr(parameters, name)) is not None
            }
        else:
            payload = parameters.model_dump(mode="python", by_alias=True, exclude_none=True)
    elif isinstance(parameters, Mapping):
        payload = dict(parameters)
    else:
        raise TypeError("parameters must be a Pydantic model or a mapping")
    if preview is not None:
        if "preview" in payload:
            raise TypeError("Execution option supplied twice: preview")
        payload["preview"] = preview
    return payload


@dataclass(frozen=True)
class TaskHandle(Generic[_Run, _Arun]):
    """A context-free task identity with blocking and asynchronous execution."""

    topic: str
    task: str

    def run(
        self,
        context: IContext,
        parameters: Mapping[str, Any] | BaseModel | None = None,
        /,
        *,
        preview: bool | None = None,
        **named: Any,
    ) -> _Run:
        """Run with a compatible context and preserve the blocking client's result contract."""
        client = _client_for(context, blocking=True)
        runner = getattr(getattr(client, self.topic), self.task.replace("-", "_"))
        payload = _parameters(parameters, named, preview, native=getattr(runner, "params_type", None) is not None)
        return cast(_Run, runner.run(**payload))

    async def arun(
        self,
        context: IContext,
        parameters: Mapping[str, Any] | BaseModel | None = None,
        /,
        *,
        preview: bool | None = None,
        **named: Any,
    ) -> _Arun:
        """Run on the caller's event loop with the asynchronous client's result contract."""
        client = _client_for(context, blocking=False)
        runner = getattr(getattr(client, self.topic), self.task.replace("-", "_"))
        payload = _parameters(parameters, named, preview, native=getattr(runner, "params_type", None) is not None)
        return cast(_Arun, await runner.run(**payload))

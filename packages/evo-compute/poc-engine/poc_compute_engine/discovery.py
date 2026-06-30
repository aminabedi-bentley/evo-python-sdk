"""Discovery client — lists the live compute task catalogue.

``evo.compute`` ships a ``JobClient`` for *executing* a task, but its ``TasksApi`` only
exposes ``execute_task`` — there is **no discovery / ``list_tasks`` client** in the SDK.
Listing the live task catalogue is the capability the generic engine relies on, and the
natural thing to upstream into ``evo-compute`` as a real ``DiscoveryApi``. This is the
smallest version of that.

It is a thin, connector-backed client — the *same shape* as ``evo.compute.JobClient``: the
engine constructs it with its own authenticated ``APIConnector`` and **delegates discovery
to it**, exactly as it already delegates EXECUTION to ``JobClient``. There is no inline
HTTP in the engine; discovery and execution are both dedicated clients over one connector.

Standalone callers (e.g. a notebook showing the *raw* catalogue, including api-preview
``feature_flag`` tasks the engine never stubbed) build one with ``from_context``.

Endpoint (verified against the live OpenAPI):
    GET /compute/orgs/{org_id}/tasks   ->   DiscoveryAPIResponse
    DiscoveryAPIResponse = {limit, offset, total, count, results: [TaskResource, ...]}
    TaskResource         = {key, topic, name, display_name, version, feature_flag?,
                            description?, parameters, results?}
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from evo.common import APIConnector, IContext
from evo.common.data import RequestMethod

# "200" -> None tells ``call_api`` the status has no response model, so it returns the raw
# decoded JSON (see connector ``_deserialize``: ``response_type is None``). The SDK supports
# that ``None`` value at runtime but types the map as ``Mapping[str, type[T]]`` (no
# ``None``), so the call site needs a ``# type: ignore[arg-type]``.
_GET_OK: dict[str, type[Any] | None] = {"200": None}


class DiscoveryClient:
    """List the live compute task catalogue for an org over an authenticated connector.

    Connector-backed and lifecycle-light: it reuses the connector it is given (the
    engine's, or the context's) and only ref-counts ``open``/``close`` around each call,
    so it is safe to use standalone *or* alongside a live engine sharing the same connector.
    """

    def __init__(self, connector: APIConnector, org_id: UUID | str) -> None:
        self._connector = connector
        self._org_id = str(org_id)

    @classmethod
    def from_context(cls, context: IContext) -> "DiscoveryClient":
        """Build from any real ``IContext`` (e.g. the notebook's ``ServiceManagerWidget``)."""
        return cls(context.get_connector(), context.get_org_id())

    async def list_tasks(self) -> list[dict]:
        """Return the raw list of ``TaskResource`` dicts advertised by the platform."""
        async with self._connector:  # ref-counted: a no-op tear-down if already open
            payload = await self._connector.call_api(
                RequestMethod.GET,
                f"/compute/orgs/{self._org_id}/tasks",
                response_types_map=_GET_OK,  # type: ignore[arg-type]  # None == "raw JSON", supported at runtime
            )
        return payload["results"]

    async def topics(self) -> list[str]:
        return sorted({t["topic"] for t in await self.list_tasks()})

    async def task_keys(self) -> set[tuple[str, str]]:
        """``{(topic, name), ...}`` — handy for diffing live breadth against the stubs."""
        return {(t["topic"], t["name"]) for t in await self.list_tasks()}

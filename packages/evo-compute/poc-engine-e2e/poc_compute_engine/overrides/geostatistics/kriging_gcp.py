"""Hand-written override for ``geostatistics/kriging-gcp`` — a *specialized typed runner*.

This co-exists with the generic engine: ``client.geostatistics.kriging_gcp`` resolves to
``KrigingGcpRunner`` (this module) instead of the schema-synthesised ``_TaskProxy``, while
the other geostatistics tasks (declustering, normal_score_gcp) stay generic. The DX is
identical — ``await client.geostatistics.kriging_gcp.run(...)`` — but here the surface is
hand-curated.

What an override buys over the generic engine (the reason to write one):

* **task-specific validation the JSON Schema can't express** — ``max_samples`` must be
  positive; ``simple`` kriging requires a declared ``mean`` (a parameter that isn't even
  in the discovery schema, added here to show an override can extend the surface);
* **a richer, hand-curated result type** with helpers the generic ``TaskResult`` could
  never synthesise (``KrigingGcpResult.summary()`` / ``.portal_url()``);
* explicit, reviewable control over the signature, defaults, and docstring.

It is **not** a parallel runtime: execution still goes through the same authenticated
``ComputeClient`` plumbing (``client._submit`` -> real ``evo.compute.JobClient``) and the
override reuses the engine's shared helpers (``_mock_resolve``/``_load_object``). It adds
only the typed shell and the bespoke behaviour.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from ...engine import GeoscienceObject, Table, _load_object, _mock_resolve

if TYPE_CHECKING:
    from ...engine import ComputeClient


class KrigingGcpResultAttribute:
    """The kriged output attribute."""

    reference: str
    name: str

    def __init__(self, payload: dict) -> None:
        self.reference = payload.get("reference", "")
        self.name = payload.get("name", "")

    def __repr__(self) -> str:
        return f"KrigingGcpResultAttribute(name={self.name!r})"


class KrigingGcpResultTarget:
    """The geoscience object the estimate was written to."""

    reference: str
    name: str
    description: str | None
    schema_id: str
    attribute: KrigingGcpResultAttribute

    def __init__(self, payload: dict) -> None:
        self.reference = payload.get("reference", "")
        self.name = payload.get("name", "")
        self.description = payload.get("description")
        self.schema_id = payload.get("schema_id", "")
        self.attribute = KrigingGcpResultAttribute(payload.get("attribute", {}))

    def get_object(self) -> GeoscienceObject:
        return _load_object(self.schema_id, self.reference, self.name)

    def to_dataframe(self) -> Table:
        return self.get_object().to_dataframe()

    def __repr__(self) -> str:
        return f"KrigingGcpResultTarget(name={self.name!r}, schema_id={self.schema_id!r})"


class KrigingGcpResult:
    """Typed kriging result with task-specific helpers (override-only)."""

    message: str
    target: KrigingGcpResultTarget

    def __init__(self, payload: dict, *, kriging_type: str) -> None:
        self.message = payload.get("message", "")
        self.target = KrigingGcpResultTarget(payload.get("target", {}))
        self._kriging_type = kriging_type

    def portal_url(self) -> str:
        """Deep-link to the output object in the Evo portal (override-only helper)."""
        object_id = self.target.reference.rsplit("/", 1)[-1]
        return f"https://portal.mock.evo/objects/{object_id}"

    def summary(self) -> str:
        """One-line human summary (override-only helper)."""
        return (
            f"{self._kriging_type} kriging wrote attribute "
            f"{self.target.attribute.name!r} to {self.target.name!r}"
        )

    def __repr__(self) -> str:
        return f"KrigingGcpResult(message={self.message!r})"


class KrigingGcpRunner:
    """Specialized, fully-typed runner for ``geostatistics/kriging-gcp``.

    Replaces the generic ``_TaskProxy`` for this one task; everything else stays generic.
    """

    def __init__(self, client: "ComputeClient", spec: dict) -> None:
        self._client = client
        self._spec = spec

    async def run(
        self,
        *,
        source: str,
        target: str,
        variogram: str,
        kriging_type: Literal["simple", "ordinary"] = "ordinary",
        max_samples: int = 20,
        mean: float | None = None,
        preview: bool = True,
    ) -> KrigingGcpResult:
        """Estimate a target attribute on a grid using kriging.

        Hand-written override: adds validation the schema can't express and returns a
        richer typed result, while still executing through the real ``JobClient``.
        """
        # Task-specific validation the JSON Schema cannot express.
        if max_samples <= 0:
            raise ValueError("max_samples must be a positive integer")
        if kriging_type == "simple" and mean is None:
            raise ValueError("simple kriging requires `mean` to be specified")

        # ``mean`` is an override-only convenience (not in the discovery schema), so it is
        # used purely for client-side validation and NOT sent to the platform. A real
        # override would map it to whatever parameter the task actually accepts.
        provided = {
            "source": source,
            "target": target,
            "variogram": variogram,
            "kriging_type": kriging_type,
            "max_samples": max_samples,
        }
        resolved = _mock_resolve(self._spec, provided)
        # AUTHENTICATED execution via the SAME path the generic engine uses.
        payload = await self._client._submit(
            self._spec["topic"], self._spec["name"], resolved, preview=preview
        )
        return KrigingGcpResult(payload or {}, kriging_type=kriging_type)

    def __dir__(self):
        return ["run"]

    def __repr__(self) -> str:
        return f"<task {self._spec['topic']}/{self._spec['name']} (specialized override)>"


def bind(client: "ComputeClient", spec: dict) -> KrigingGcpRunner:
    """Convention factory the engine calls to hand this task to the override."""
    return KrigingGcpRunner(client, spec)

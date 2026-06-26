"""Hand-written override for ``geostatistics/kriging``.

This module demonstrates the per-task override seam. It co-exists with the
generated stub for kriging — but instead of the stub re-exporting a
schema-derived surface, it re-exports THIS module's hand-written, fully-typed
``run`` and ``KrigingResult``.

What an override buys over the generic engine (the reason to write one):

* **task-specific validation** the schema can't express (``max_samples`` must be
  positive; ``simple`` kriging needs a declared mean — checked here);
* **richer, hand-curated result types** with extra helpers
  (``KrigingResult.portal_url()`` / ``.summary()``) the generic ``TaskResult``
  has no way to synthesise;
* explicit control over the surface for debugging / docs.

It still REUSES the generic engine plumbing (``_mock_resolve``,
``_fabricate_output``, ``_load_object``) so the override stays small — it adds
the typed shell and the task-specific behaviour, not a parallel runtime.
"""

from __future__ import annotations

from typing import Literal

from ..._engine import GeoscienceObject, Table, _fabricate_output, _load_object, _mock_resolve
from ..._schemas import get_spec

_loaded = get_spec("geostatistics", "kriging")
assert _loaded is not None, "kriging override requires the bundled kriging schema"
_SPEC: dict = _loaded


class KrigingResultAttribute:
    """The kriged output attribute."""

    reference: str
    name: str

    def __init__(self, payload: dict) -> None:
        self.reference = payload["reference"]
        self.name = payload["name"]


class KrigingResultTarget:
    """The geoscience object the estimate was written to."""

    reference: str
    name: str
    description: str | None
    schema_id: str
    attribute: KrigingResultAttribute

    def __init__(self, payload: dict) -> None:
        self.reference = payload["reference"]
        self.name = payload["name"]
        self.description = payload.get("description")
        self.schema_id = payload["schema_id"]
        self.attribute = KrigingResultAttribute(payload["attribute"])

    def get_object(self) -> GeoscienceObject:
        return _load_object(self.schema_id, self.reference, self.name)

    def to_dataframe(self) -> Table:
        return self.get_object().to_dataframe()


class KrigingResult:
    """Typed kriging result with task-specific helpers."""

    message: str
    target: KrigingResultTarget

    def __init__(self, payload: dict, *, kriging_type: str) -> None:
        self.message = payload["message"]
        self.target = KrigingResultTarget(payload["target"])
        self._kriging_type = kriging_type

    def portal_url(self) -> str:
        """Deep-link to the output object in the Evo portal (override-only helper)."""
        object_id = self.target.reference.rsplit("/", 1)[-1]
        return f"https://portal.mock.evo/objects/{object_id}"

    def summary(self) -> str:
        """One-line human summary (override-only helper)."""
        return f"{self._kriging_type} kriging wrote attribute {self.target.attribute.name!r} to {self.target.name!r}"

    def __repr__(self) -> str:
        return f"KrigingResult(message={self.message!r})"


def run(
    *,
    source: str,
    target: str,
    variogram: str,
    kriging_type: Literal["simple", "ordinary"] = "ordinary",
    max_samples: int = 20,
    mean: float | None = None,
    preview: bool = True,
) -> KrigingResult:
    """Estimate a target attribute on a grid using kriging.

    Hand-written override: adds validation and a richer typed result on top of
    the generic engine.
    """
    # Task-specific validation the JSON Schema cannot express.
    if max_samples <= 0:
        raise ValueError("max_samples must be a positive integer")
    if kriging_type == "simple" and mean is None:
        raise ValueError("simple kriging requires `mean` to be specified")

    provided = {
        "source": source,
        "target": target,
        "variogram": variogram,
        "kriging_type": kriging_type,
        "max_samples": max_samples,
    }
    _mock_resolve(_SPEC, provided)
    payload = _fabricate_output(_SPEC, provided)
    assert payload is not None
    return KrigingResult(payload, kriging_type=kriging_type)

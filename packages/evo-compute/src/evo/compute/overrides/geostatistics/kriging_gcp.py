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

"""Hand-written runner for ``geostatistics/kriging-gcp`` -- the worked example of an override.

Kriging is the task with the most behind it: a parameter model that has been through
review, a result type with helpers a synthesised one could not have, and two mistakes the
published schema has no way to describe. So it is the one that earns a hand-written
surface, and ``client.geostatistics.kriging_gcp`` is served from here instead of from the
generic proxy.

What the override adds, and nothing more:

* **Checks the JSON Schema cannot express.** The schema bounds each parameter on its own --
  ``min_samples`` and ``max_samples`` are both integers of at least 1 -- but it cannot say
  that one must not exceed the other, and it cannot say that a task must not estimate an
  attribute from itself. Both are silent failures otherwise: the first finds no
  neighbourhood anywhere, the second overwrites its own input.
* **The typed parameter model.** :class:`~evo.compute.tasks.geostatistics.kriging.KrigingParameters`
  accepts typed objects and attributes, folds ``source_filter`` and ``target_filter`` into
  the source and target the wire expects, and names the arguments as the SDK names them
  rather than as the wire spells them.
* **The typed result.** :class:`~evo.compute.tasks.geostatistics.kriging.KrigingResult` --
  ``target_name``, ``attribute_name``, ``schema``, ``get_target_object()``,
  ``to_dataframe()`` -- in place of the generic :class:`~evo.compute.outputs.TaskResult`.

What it does not add is a second way to reach the platform. The payload still goes through
:meth:`~evo.compute.engine.ComputeClient.arun`, so the job is discovered, validated,
resolved and submitted exactly as any other task is, and the generic path to this same task
stays open for anyone who wants it::

    result = await client.arun("geostatistics", "kriging-gcp", {...})
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from evo.objects import DownloadedObject, ObjectMetadata, ObjectReference
from evo.objects.typed import Attribute, BlockModelAttribute
from evo.objects.typed.base import BaseObject

from ...exceptions import ParameterValidationError
from ...tasks.common import Filter, SearchNeighborhood, Source, Target
from ...tasks.common.source_target import AnyTypedAttribute, UpdateAttribute
from ...tasks.geostatistics.kriging import (
    BlockDiscretisation,
    KrigingParameters,
    KrigingResult,
    KrigingResultModel,
    OrdinaryKriging,
    SimpleKriging,
)

if TYPE_CHECKING:
    from ...engine import ComputeClient

__all__ = [
    "KrigingGcpRunner",
    "bind",
]

# What each reference parameter takes. The model fields are annotated with their *validated*
# type, which a checker reads as the narrow one; a signature has to say what it accepts.
_SourceInput = Source | Attribute | BlockModelAttribute
_TargetInput = Target | AnyTypedAttribute
_ObjectInput = str | ObjectReference | BaseObject | DownloadedObject | ObjectMetadata


def _same_object(left: str, right: str) -> bool:
    """Whether two references name the same object, whatever version or spelling each uses.

    Comparing the URLs as text would miss it. :class:`~evo.objects.ObjectReference` is a ``str``
    subclass that keeps whatever the caller wrote, and its path pattern is case-insensitive, so
    an uppercase UUID on one side and a lowercase one on the other are one object written two
    ways. A trailing fragment does the same. The parsed components are what identify an object;
    ``version_id`` is not one of them, since a version names a moment in the object's history.

    A path reference and a UUID reference cannot be told apart without loading the object, so
    they never match here -- this guard would rather miss a case than invent one.
    """
    first, second = ObjectReference(left), ObjectReference(right)
    return (first.environment, first.object_id, first.object_path) == (
        second.environment,
        second.object_id,
        second.object_path,
    )


class KrigingGcpRunner:
    """The override's stand-in for the generic task proxy: same ``run(...)``, typed by hand."""

    def __init__(self, client: ComputeClient, topic: str, task: str) -> None:
        self._client = client
        self._topic = topic
        self._task = task

    async def run(
        self,
        *,
        source: _SourceInput,
        target: _TargetInput,
        variogram: _ObjectInput,
        search: SearchNeighborhood,
        method: SimpleKriging | OrdinaryKriging | None = None,
        source_filter: Filter | None = None,
        target_filter: Filter | None = None,
        block_discretisation: BlockDiscretisation | None = None,
        preview: bool | None = None,
    ) -> KrigingResult:
        """Estimate an attribute on a target object by kriging from a source attribute.

        :param source: The object and attribute holding the known values, or the typed
            attribute itself.
        :param target: Where to write the estimate -- a :class:`~evo.compute.tasks.common.Target`,
            or the typed attribute to create or update.
        :param variogram: The variogram object, or a reference to it.
        :param search: The search neighbourhood: the ellipsoid to look in, and how many
            samples to use.
        :param method: Ordinary kriging by default; :meth:`KrigingMethod.simple` when the
            mean is known.
        :param source_filter: Restrict the estimate to a subset of the source data.
        :param target_filter: Restrict the estimate to a subset of the target object.
        :param block_discretisation: Sub-block discretisation for block kriging. Omitted
            means point kriging.
        :param preview: Opt in or out of the preview API. Defaults to what the task's
            feature flag says, like every other task.

        :raises ParameterValidationError: If the parameters are inconsistent in a way the
            task schema cannot describe, or fail the engine's own checks.
        """
        parameters = KrigingParameters(
            source=source,
            target=target,
            variogram=variogram,
            search=search,
            method=method if method is not None else OrdinaryKriging(),
            source_filter=source_filter,
            target_filter=target_filter,
            block_discretisation=block_discretisation,
        )
        self._check_parameters(parameters)

        payload = parameters.model_dump(mode="json", by_alias=True, exclude_none=True)
        if preview is not None:
            payload["preview"] = preview

        result = await self._client.arun(self._topic, self._task, payload)
        return KrigingResult(self._client.context, KrigingResultModel.model_validate(dict(result)))

    def _check_parameters(self, parameters: KrigingParameters) -> None:
        """Reject the two inconsistencies the task's JSON Schema has no way to state."""
        label = f"{self._topic}.{self._task.replace('-', '_')}"
        errors: list[str] = []

        search = parameters.search
        if search.min_samples is not None and search.min_samples > search.max_samples:
            errors.append(
                f"search: min_samples ({search.min_samples}) exceeds max_samples "
                f"({search.max_samples}), so no location can ever be estimated"
            )

        attribute = parameters.target.attribute
        if (
            isinstance(attribute, UpdateAttribute)
            and _same_object(parameters.source.object, parameters.target.object)
            and attribute.reference == parameters.source.attribute
        ):
            errors.append(
                f"target: {attribute.reference!r} is also the source attribute, "
                "so the estimate would overwrite its own input"
            )

        if errors:
            raise ParameterValidationError(
                f"{label}.run(): " + "; ".join(errors),
                task=label,
                errors=errors,
            )

    def __repr__(self) -> str:
        return f"<compute task {self._topic!r}.{self._task.replace('-', '_')!r} (override)>"


def bind(client: ComputeClient, topic: str, task: str) -> KrigingGcpRunner:
    """Hand this task to the override. Called by :mod:`evo.compute.overrides`."""
    return KrigingGcpRunner(client, topic, task)

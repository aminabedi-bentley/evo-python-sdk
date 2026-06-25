"""A stand-in for the Evo compute Discovery API.

In the real system this data comes from
``GET /compute/orgs/{org_id}/tasks?details=true``. Here it is a hardcoded
payload so the POC depends on no Seequent repo. The shapes (topic / name /
version / feature_flag / parameters JSON Schema, plus the ``reference_to``
annotation) mirror the real discovery response and the core-compute-tasks
schemas.
"""

from __future__ import annotations

import copy

# One entry per runnable task. ``parameters`` is a (trimmed) JSON Schema.
_BASE_TASKS = [
    {
        "topic": "geostatistics",
        "name": "kriging",
        "version": "1.0.0",
        "feature_flag": "preview",  # non-empty => API preview => submit opt-in
        "description": "Estimate a target attribute on a grid using kriging.",
        "parameters": {
            "type": "object",
            "properties": {
                "source": {
                    "type": "string",
                    "reference_to": "attribute",
                    "description": "Source attribute to estimate from.",
                },
                "target": {
                    "type": "string",
                    "reference_to": "attribute",
                    "description": "Target attribute to write the estimate to.",
                },
                "variogram": {
                    "type": "string",
                    "reference_to": "geoscience-object",
                    "description": "Variogram model object.",
                },
                "kriging_type": {
                    "type": "string",
                    "enum": ["simple", "ordinary"],
                    "default": "ordinary",
                    "description": "Kriging variant to run.",
                },
                "max_samples": {
                    "type": "integer",
                    "default": 20,
                    "description": "Maximum samples in the search neighbourhood.",
                },
            },
            "required": ["source", "target", "variogram"],
        },
        # The discovery response ALSO describes the OUTPUT, with the same semantic
        # vocabulary as the inputs (`output`, `reference_to`, `supported_schemas`,
        # `attribute_path`). A generic engine can hydrate the result from this block
        # alone — no per-task output code required.
        "results": {
            "type": "object",
            "title": "KrigingResult",
            "description": "Result of the kriging task.",
            "required": ["message", "target"],
            "properties": {
                "message": {
                    "type": "string",
                    "description": "A message that says what happened in the task.",
                },
                "target": {
                    "type": "object",
                    "output": "geoscience-object",
                    "description": "The target that was created or updated.",
                    "required": ["reference", "name", "description", "schema_id", "attribute"],
                    "properties": {
                        "reference": {
                            "type": "string",
                            "format": "uri",
                            "reference_to": "geoscience-object",
                            "supported_schemas": [
                                "regular-3d-grid/[>=1.2,<2]",
                                "regular-masked-3d-grid/[>=1.2,<2]",
                                "tensor-3d-grid/[>=1.2,<2]",
                                "block-model/[>=1.0,<2]",
                                "pointset/[>=1.2,<2]",
                                "downhole-intervals/[>=1.2,<2]",
                            ],
                            "description": "Reference to a geoscience object.",
                        },
                        "name": {"type": "string", "description": "The name of the geoscience object."},
                        "description": {
                            "type": ["string", "null"],
                            "description": "The description of the geoscience object.",
                        },
                        "schema_id": {"type": "string", "description": "The ID of the Geoscience Object schema."},
                        "attribute": {
                            "type": "object",
                            "output": "attribute",
                            "description": "Attribute containing the kriging result.",
                            "required": ["reference", "name"],
                            "properties": {
                                "reference": {
                                    "type": "string",
                                    "reference_to": "attribute",
                                    "attribute_from": "2/reference",
                                    # Resolution rule carried IN the schema, keyed by the
                                    # runtime object schema_id -> the engine self-heals.
                                    "attribute_path": {
                                        "block-model/[>=1.0,<2]": ["attributes[?attribute_type=='Float64']"],
                                        "downhole-intervals/[>=1.2,<2]": ["attributes[?attribute_type=='scalar']"],
                                        "pointset/[>=1.2,<2]": ["locations.attributes[?attribute_type=='scalar']"],
                                        "regular-3d-grid/[>=1.2,<2]": ["cell_attributes[?attribute_type=='scalar']"],
                                        "regular-masked-3d-grid/[>=1.2,<2]": ["cell_attributes[?attribute_type=='scalar']"],
                                        "tensor-3d-grid/[>=1.2,<2]": ["cell_attributes[?attribute_type=='scalar']"],
                                    },
                                    "description": "Reference to the attribute in the geoscience object.",
                                },
                                "name": {"type": "string", "description": "The name of the output attribute."},
                            },
                        },
                    },
                },
            },
        },
    },
    {
        "topic": "geostatistics",
        "name": "declustering",
        "version": "1.0.0",
        "feature_flag": "",
        "description": "Compute declustering weights for a sample attribute.",
        "parameters": {
            "type": "object",
            "properties": {
                "source": {
                    "type": "string",
                    "reference_to": "attribute",
                    "description": "Sample attribute to decluster.",
                },
                "target": {
                    "type": "string",
                    "reference_to": "attribute",
                    "description": "Attribute to write the declustering weights to.",
                },
                "cell_size": {
                    "type": "number",
                    "default": 100.0,
                    "description": "Declustering cell size.",
                },
            },
            "required": ["source", "target"],
        },
        "results": {
            "type": "object",
            "title": "DeclusteringResult",
            "description": "Result of the declustering task.",
            "required": ["message", "target"],
            "properties": {
                "message": {
                    "type": "string",
                    "description": "A message that says what happened in the task.",
                },
                "target": {
                    "type": "object",
                    "output": "geoscience-object",
                    "description": "The object the declustering weights were written to.",
                    "required": ["reference", "name", "schema_id", "attribute"],
                    "properties": {
                        "reference": {
                            "type": "string",
                            "reference_to": "geoscience-object",
                            "supported_schemas": ["pointset/[>=1.2,<2]"],
                            "description": "Reference to the geoscience object.",
                        },
                        "name": {"type": "string", "description": "The name of the geoscience object."},
                        "schema_id": {"type": "string", "description": "The ID of the Geoscience Object schema."},
                        "attribute": {
                            "type": "object",
                            "output": "attribute",
                            "required": ["reference", "name"],
                            "properties": {
                                "reference": {
                                    "type": "string",
                                    "reference_to": "attribute",
                                    "attribute_path": {
                                        "pointset/[>=1.2,<2]": ["locations.attributes[?attribute_type=='scalar']"],
                                    },
                                    "description": "Reference to the weights attribute.",
                                },
                                "name": {"type": "string", "description": "The name of the weights attribute."},
                            },
                        },
                    },
                },
            },
        },
    },
]

# Runtime-added tasks (used by the demo to show *live* breadth that has no stub).
_RUNTIME_TASKS: list[dict] = []


def fetch_discovery() -> dict:
    """Return a discovery payload, mirroring the real API envelope."""
    results = copy.deepcopy(_BASE_TASKS) + copy.deepcopy(_RUNTIME_TASKS)
    return {"total": len(results), "count": len(results), "results": results}


def register_runtime_task(spec: dict) -> None:
    """Simulate the platform advertising a *new* task after stubs were generated."""
    _RUNTIME_TASKS.append(spec)

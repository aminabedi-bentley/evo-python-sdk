"""Offline tests for generic result hydration (`_ResultNode` / `TaskResult`).

Covers every RFC 168 *output* property type — `geoscience-object`, `file`, and
`attribute` — plus arrays of output objects and nullable outputs that came back
`null`. Shapes mirror the real geostatistics/{conditioned-simulator,simulation-report}
`results` blocks. Run: `uv run python test_result.py`.
"""

from __future__ import annotations

from poc_compute_engine.engine import File, GeoscienceObject, Table, TaskResult

# A results schema exercising all three output kinds + array + nullable, modelled on
# conditioned-simulator/simulation-report.
RESULTS_SCHEMA = {
    "type": "object",
    "properties": {
        "target": {
            "type": "object",
            "output": "geoscience-object",
            "properties": {
                "reference": {
                    "type": "string",
                    "reference_to": "geoscience-object",
                    "supported_schemas": ["regular-3d-grid/[>=1.2,<2]"],
                },
                "name": {"type": "string"},
                "summary_attributes": {
                    "type": "object",
                    "properties": {
                        "mean": {
                            "type": "object",
                            "output": "attribute",
                            "properties": {"reference": {"type": "string", "reference_to": "attribute"}},
                        },
                    },
                },
                "quantile_attributes": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "output": "attribute",
                        "properties": {"reference": {"type": "string", "reference_to": "attribute"}},
                    },
                },
                "simulations": {
                    "type": ["object", "null"],
                    "output": "attribute",
                    "properties": {"reference": {"type": "string", "reference_to": "attribute"}},
                },
            },
        },
        "validation_report": {
            "type": ["object", "null"],
            "output": "file",
            "properties": {
                "reference": {"type": "string", "reference_to": "file", "format": "uri"},
            },
        },
        "links": {
            "type": "object",
            "properties": {"dashboard": {"type": ["string", "null"], "format": "uri"}},
        },
    },
}

PAYLOAD = {
    "target": {
        "reference": "https://hub/objects/grid-out",
        "schema_id": "regular-3d-grid/1.2.0",
        "name": "Simulated Grid",
        "summary_attributes": {"mean": {"reference": "attributes[?name=='mean']"}},
        "quantile_attributes": [
            {"reference": "attributes[?name=='q10']"},
            {"reference": "attributes[?name=='q90']"},
        ],
        "simulations": None,  # nullable output that came back null
    },
    "validation_report": {"reference": "https://hub/files/report.json"},
    "links": {"dashboard": None},
}


def test_geoscience_object_output() -> None:
    r = TaskResult(RESULTS_SCHEMA, PAYLOAD)
    obj = r.target.get_object()
    assert isinstance(obj, GeoscienceObject)
    assert obj.reference == "https://hub/objects/grid-out"
    assert obj.schema_id == "regular-3d-grid/1.2.0"
    assert isinstance(r.target.to_dataframe(), Table)


def test_file_output() -> None:
    r = TaskResult(RESULTS_SCHEMA, PAYLOAD)
    f = r.validation_report.get_file()
    assert isinstance(f, File)
    assert f.reference == "https://hub/files/report.json"
    # It is NOT a geoscience object.
    try:
        r.validation_report.get_object()
    except AttributeError:
        pass
    else:  # pragma: no cover
        raise AssertionError("file output should not resolve as a geoscience object")


def test_attribute_output() -> None:
    r = TaskResult(RESULTS_SCHEMA, PAYLOAD)
    assert r.target.summary_attributes.mean.get_attribute() == "attributes[?name=='mean']"


def test_array_of_attribute_outputs() -> None:
    r = TaskResult(RESULTS_SCHEMA, PAYLOAD)
    quants = r.target.quantile_attributes
    assert isinstance(quants, list) and len(quants) == 2
    assert [q.get_attribute() for q in quants] == [
        "attributes[?name=='q10']",
        "attributes[?name=='q90']",
    ]


def test_nullable_output_returned_null() -> None:
    r = TaskResult(RESULTS_SCHEMA, PAYLOAD)
    assert r.target.simulations is None  # nullable attribute output, came back null
    assert r.links.dashboard is None  # nullable scalar passthrough


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
        print(f"ok  {t.__name__}")
    print(f"\n{len(tests)}/{len(tests)} passed")

"""Offline tests for the DX *type surface*: that `run(...)` advertises the friendly
input unions (loaded objects, ObjectHandles, URLs, attribute/target refs) instead of a
bare `str`/`dict`. Run: `uv run python test_dx.py`.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Union, get_args, get_origin

from poc_compute_engine.engine import _py_annotation, _signature_from_schema
from poc_compute_engine.resolver import (
    AttributeInput,
    CreateAttr,
    FileInput,
    LoadedObject,
    ObjectHandle,
    ObjectInput,
    TargetAttrInput,
    UpdateAttr,
)

HERE = Path(__file__).resolve().parent
NS_GCP = json.load(open(HERE / "poc_compute_engine/schemas/geostatistics/normal-score-gcp/schema.json"))
NS_GCP.setdefault("topic", "geostatistics")
NS_GCP.setdefault("name", "normal-score-gcp")


def test_object_input_advertises_handle_and_loaded_object() -> None:
    # The whole point: a geoscience-object reference accepts a URL str, an ObjectHandle,
    # AND a loaded object.
    members = set(get_args(ObjectInput))
    assert str in members
    assert ObjectHandle in members
    assert LoadedObject in members


def test_py_annotation_is_reference_aware() -> None:
    assert _py_annotation({"reference_to": "geoscience-object"}) is ObjectInput
    assert _py_annotation({"reference_to": "file"}) is FileInput
    assert _py_annotation({"reference_to": "attribute"}) is AttributeInput
    assert _py_annotation({"target": "attribute"}) is TargetAttrInput
    # Non-reference scalars are unchanged.
    assert _py_annotation({"type": "string"}) is str


def test_runtime_signature_advertises_object_input() -> None:
    sig = _signature_from_schema(NS_GCP)
    dist = sig.parameters["distribution"].annotation
    assert dist is ObjectInput
    assert get_origin(dist) is Union and ObjectHandle in get_args(dist)


def test_target_attr_input_accepts_create_and_update() -> None:
    members = set(get_args(TargetAttrInput))
    assert {str, CreateAttr, UpdateAttr} <= members


def test_generated_stub_shows_exact_inputs() -> None:
    pyi = (HERE / "poc_compute_engine/__init__.pyi").read_text()
    # Object groups become per-task TypedDicts with reference-aware fields.
    assert "class NormalScoreGcpSourceInput(TypedDict" in pyi
    assert "object: ObjectInput" in pyi
    assert "attribute: AttributeInput" in pyi
    assert "attribute: TargetAttrInput" in pyi
    # Bare geoscience-object reference params advertise ObjectInput, not str.
    assert "distribution: ObjectInput" in pyi
    # The friendly aliases are imported from the resolver (the authoritative source).
    assert "from poc_compute_engine.resolver import (" in pyi


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
        print(f"ok  {t.__name__}")
    print(f"\n{len(tests)}/{len(tests)} passed")

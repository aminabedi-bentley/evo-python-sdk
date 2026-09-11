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

"""Tests for the offline type-stub generator.

Three layers:

* The committed ``engine.pyi`` is exactly what the generator produces from the committed
  snapshot, so the artifact can never silently drift from its source.
* The stub's ``run(...)`` signatures agree with the signatures the engine synthesises at
  runtime, so static help does not promise something the engine will reject.
* A real type checker accepts the "good" usage file and rejects the "bad" one. ``mypy`` is a
  test dependency so this runs in CI; ``pyright`` is checked too when it is on the ``PATH``.
"""

from __future__ import annotations

import ast
import inspect
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from evo.compute import ComputeClient, ParameterValidationError, _stubgen
from evo.compute.endpoints.models import TaskResource
from evo.compute.engine import _signature_from_schema
from evo.compute.validation import validate_parameters

_CHECKS_DIR = _stubgen.DEFAULT_SNAPSHOT_DIR.parent / "checks"


def _spec(**overrides) -> TaskResource:
    payload = {"topic": "demo", "name": "widget", "parameters": {"type": "object", "properties": {}}}
    payload.update(overrides)
    return TaskResource.model_validate(payload)


def _render(**overrides) -> str:
    """Render one task's generated types as a single blob, for substring assertions."""
    renderer = _stubgen._TaskRenderer(_spec(**overrides))
    stub = renderer.render()
    return "\n\n".join(generated.body for generated in stub.types)


class TestGeneratedArtifact(unittest.TestCase):
    def test_stub_is_up_to_date(self) -> None:
        """The committed stub must be reproducible from the committed snapshot."""
        expected = _stubgen.generate_stub()
        actual = _stubgen.DEFAULT_OUTPUT.read_text()
        self.assertEqual(
            expected,
            actual,
            "engine.pyi is out of date; run `python -m evo.compute._stubgen generate`",
        )

    def test_generation_is_deterministic(self) -> None:
        self.assertEqual(_stubgen.generate_stub(), _stubgen.generate_stub())

    def test_a_file_reference_brings_its_structural_handles(self) -> None:
        """No snapshotted task takes a file yet, so the aliases are only reachable this way.

        ``resolution._resolve_file`` reads a handle structurally -- ``evo-files`` is not a
        dependency here -- so the stub describes it structurally too.
        """
        spec = _spec(parameters={"type": "object", "properties": {"report": {"reference_to": "file"}}})
        with tempfile.TemporaryDirectory() as directory:
            snapshot = Path(directory) / "demo"
            snapshot.mkdir()
            (snapshot / "widget.json").write_text(json.dumps(spec.model_dump(mode="json")))
            rendered = _stubgen._render(_stubgen.load_snapshot(Path(directory)), Path(directory))
        self.assertIn("report: FileInput", rendered)
        self.assertIn("FileInput = str | _FileUrl | _FileWithMetadata", rendered)
        self.assertIn("class _FileUrl(Protocol):", rendered)
        self.assertIn("class _FileWithMetadata(Protocol):", rendered)
        self.assertIn("Protocol", rendered.split("__all__")[0])
        ast.parse(rendered)

    def test_stub_is_valid_python(self) -> None:
        ast.parse(_stubgen.DEFAULT_OUTPUT.read_text(), filename=str(_stubgen.DEFAULT_OUTPUT))

    def test_snapshot_matches_its_manifest(self) -> None:
        manifest = json.loads((_stubgen.DEFAULT_SNAPSHOT_DIR / "manifest.json").read_text())
        recorded = {(entry["topic"], entry["name"], entry["version"]) for entry in manifest["tasks"]}
        on_disk = {
            (task.topic, task.name, task.version) for task in _stubgen.load_snapshot(_stubgen.DEFAULT_SNAPSHOT_DIR)
        }
        self.assertEqual(recorded, on_disk)

    def test_every_snapshotted_task_is_reachable(self) -> None:
        stub = _stubgen.DEFAULT_OUTPUT.read_text()
        for task in _stubgen.load_snapshot(_stubgen.DEFAULT_SNAPSHOT_DIR):
            with self.subTest(task=f"{task.topic}.{task.name}"):
                self.assertIn(f"    {task.name.replace('-', '_')}: _", stub)

    def test_stub_signatures_match_the_runtime_signatures(self) -> None:
        """What the stub advertises is what :func:`_signature_from_schema` will bind."""
        for task in _stubgen.load_snapshot(_stubgen.DEFAULT_SNAPSHOT_DIR):
            with self.subTest(task=f"{task.topic}.{task.name}"):
                stub = _stubgen._TaskRenderer(task).render()
                declared = [parameter.split(":")[0] for parameter in stub.run_parameters]
                self.assertEqual(list(_signature_from_schema(task).parameters), declared)

    def test_the_client_signatures_are_not_hand_maintained_into_drift(self) -> None:
        """The task surface is generated, but ``ComputeClient``'s own methods are written out.

        Nothing else notices when the engine gains a keyword, so the stub silently stops
        describing the class it stands for -- which is how ``check_schemas`` went missing.
        """
        rendered = "\n".join(_stubgen._render_client([]))
        for method in (ComputeClient.__init__, ComputeClient.arun):
            with self.subTest(method=method.__name__):
                expected = [name for name in inspect.signature(method).parameters if name != "self"]
                block = rendered.split(f"def {method.__name__}(")[1].split(") ->")[0]
                declared = [line.strip().split(":")[0] for line in block.splitlines() if ":" in line]
                self.assertEqual(expected, declared)


class TestAnnotations(unittest.TestCase):
    def test_scalars_map_to_python_types(self) -> None:
        spec = _spec(
            parameters={
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "count": {"type": "integer"},
                    "ratio": {"type": "number"},
                    "flag": {"type": "boolean"},
                },
                "required": ["name", "count", "ratio", "flag"],
            }
        )
        parameters = _stubgen._TaskRenderer(spec).render().run_parameters
        self.assertEqual(
            ["name: str", "count: int", "ratio: float", "flag: bool", "preview: bool = False"],
            parameters,
        )

    def test_optional_fields_are_not_required(self) -> None:
        rendered = _render(
            parameters={
                "type": "object",
                "properties": {
                    "config": {
                        "type": "object",
                        "properties": {"seed": {"type": "integer"}, "note": {"type": "string"}},
                        "required": ["seed"],
                    }
                },
            }
        )
        self.assertIn("seed: int", rendered)
        self.assertIn("note: NotRequired[str]", rendered)

    def test_nullable_type_becomes_a_union_with_none(self) -> None:
        rendered = _render(
            parameters={
                "type": "object",
                "properties": {"group": {"type": "object", "properties": {"power": {"type": ["number", "null"]}}}},
            }
        )
        self.assertIn("power: NotRequired[float | None]", rendered)

    def test_enum_and_const_become_literals(self) -> None:
        rendered = _render(
            parameters={
                "type": "object",
                "properties": {
                    "group": {
                        "type": "object",
                        "properties": {
                            "mode": {"enum": ["fast", "slow"]},
                            "kind": {"const": "widget"},
                            "maybe": {"enum": ["on", None]},
                        },
                    }
                },
            }
        )
        self.assertIn('mode: NotRequired[Literal["fast", "slow"]]', rendered)
        self.assertIn('kind: NotRequired[Literal["widget"]]', rendered)
        self.assertIn('maybe: NotRequired[Literal["on"] | None]', rendered)

    def test_arrays_and_free_form_objects(self) -> None:
        rendered = _render(
            parameters={
                "type": "object",
                "properties": {
                    "group": {
                        "type": "object",
                        "properties": {
                            "tags": {"type": "array", "items": {"type": "string"}},
                            "anything": {"type": "array"},
                            "extras": {"type": "object"},
                            "counts": {"type": "object", "additionalProperties": {"type": "integer"}},
                        },
                    }
                },
            }
        )
        self.assertIn("tags: NotRequired[list[str]]", rendered)
        self.assertIn("anything: NotRequired[list[Any]]", rendered)
        self.assertIn("extras: NotRequired[dict[str, Any]]", rendered)
        self.assertIn("counts: NotRequired[dict[str, int]]", rendered)

    def test_self_referential_ref_terminates(self) -> None:
        rendered = _render(
            parameters={
                "type": "object",
                "$defs": {
                    "Node": {
                        "type": "object",
                        "title": "Node",
                        "properties": {"children": {"type": "array", "items": {"$ref": "#/$defs/Node"}}},
                    }
                },
                "properties": {"root": {"$ref": "#/$defs/Node"}},
            }
        )
        self.assertIn("class WidgetNode(TypedDict):", rendered)
        self.assertIn("children: NotRequired[list[WidgetNode]]", rendered)

    def test_identical_shapes_collapse_onto_one_type(self) -> None:
        """The published schemas inline the same shape repeatedly; one name per shape."""
        point = {"type": "object", "properties": {"x": {"type": "number"}, "y": {"type": "number"}}}
        rendered = _render(
            parameters={"type": "object", "properties": {"start": dict(point), "end": dict(point)}},
        )
        self.assertEqual(1, rendered.count("(TypedDict):"))
        self.assertIn("class WidgetStart(TypedDict):", rendered)

    def test_union_members_are_named_after_their_discriminating_constant(self) -> None:
        rendered = _render(
            parameters={
                "type": "object",
                "properties": {
                    "action": {
                        "discriminator": "operation",
                        "oneOf": [
                            {"type": "object", "properties": {"operation": {"const": "create"}}},
                            {"type": "object", "properties": {"operation": {"const": "update"}}},
                        ],
                    }
                },
            }
        )
        self.assertIn("class WidgetActionCreate(TypedDict):", rendered)
        self.assertIn("class WidgetActionUpdate(TypedDict):", rendered)


class TestTaskSurface(unittest.TestCase):
    def test_preview_defaults_to_the_feature_flag(self) -> None:
        self.assertIn("preview: bool = False", _stubgen._TaskRenderer(_spec()).render().run_parameters)
        gated = _spec(feature_flag="widgets-preview")
        self.assertIn("preview: bool = True", _stubgen._TaskRenderer(gated).render().run_parameters)

    def test_required_parameters_come_first_and_have_no_default(self) -> None:
        spec = _spec(
            parameters={
                "type": "object",
                "properties": {"optional": {"type": "string"}, "needed": {"type": "string"}},
                "required": ["needed"],
            }
        )
        parameters = _stubgen._TaskRenderer(spec).render().run_parameters
        self.assertEqual(["needed: str", "optional: str = ...", "preview: bool = False"], parameters)

    def test_result_schema_becomes_a_hydrated_node(self) -> None:
        """Results come back as ``TaskResult``, so the stub declares one rather than a dict."""
        spec = _spec(results={"type": "object", "properties": {"message": {"type": "string"}}})
        stub = _stubgen._TaskRenderer(spec).render()
        self.assertEqual("WidgetResult", stub.return_type)
        self.assertIn("class WidgetResult(TaskResult):", stub.types[-1].body)
        # No NotRequired: a declared attribute is what makes ``.load()`` and the nested
        # types reachable, and an attribute cannot be marked absent the way a key can.
        self.assertIn("message: str", stub.types[-1].body)

    def test_nested_results_are_nodes_not_typed_dicts(self) -> None:
        spec = _spec(
            results={
                "type": "object",
                "properties": {"target": {"type": "object", "properties": {"reference": {"type": "string"}}}},
            }
        )
        bodies = "\n".join(generated.body for generated in _stubgen._TaskRenderer(spec).render().types)
        self.assertIn("(ResultNode):", bodies)
        self.assertNotIn("TypedDict", bodies)

    def test_task_without_results_still_returns_a_task_result(self) -> None:
        self.assertEqual("TaskResult", _stubgen._TaskRenderer(_spec()).render().return_type)

    def test_input_and_output_shapes_do_not_collide(self) -> None:
        """A task whose parameters and results both title a shape ``Target``."""
        target = {"type": "object", "title": "Target", "properties": {"name": {"type": "string"}}}
        spec = _spec(
            parameters={"type": "object", "properties": {"target": dict(target)}},
            results={"type": "object", "properties": {"target": {**target, "properties": {"url": {"type": "string"}}}}},
        )
        names = [generated.name for generated in _stubgen._TaskRenderer(spec).render().types]
        self.assertIn("WidgetTarget", names)
        self.assertIn("WidgetResultTarget", names)

    def test_a_reference_parameter_accepts_every_handle_the_resolver_takes(self) -> None:
        """The schema declares the resolved URL, but resolution runs first, so the stub
        describes what may be passed rather than what goes on the wire."""
        spec = _spec(
            parameters={
                "type": "object",
                "properties": {
                    "source": {"type": "string", "reference_to": "geoscience-object"},
                    "attribute": {"type": "string", "reference_to": "attribute"},
                    "report": {"type": ["string", "null"], "reference_to": "file"},
                },
                "required": ["source", "attribute"],
            }
        )
        parameters = _stubgen._TaskRenderer(spec).render().run_parameters
        self.assertIn("source: ObjectInput", parameters)
        self.assertIn("attribute: AttributeInput", parameters)
        self.assertIn("report: FileInput | None = ...", parameters)

    def test_a_reference_in_the_results_stays_the_url_the_platform_sent(self) -> None:
        """``reference_to`` appears on both sides; only the input side is widened."""
        spec = _spec(
            results={
                "type": "object",
                "properties": {"target": {"type": "string", "reference_to": "geoscience-object"}},
            }
        )
        self.assertIn("target: str", _stubgen._TaskRenderer(spec).render().types[-1].body)

    def test_a_one_reference_frame_also_takes_the_object_itself(self) -> None:
        """``_frame_from_object``: ``grid={\"object\": x}`` and ``grid=x`` both resolve."""
        spec = _spec(
            parameters={
                "type": "object",
                "properties": {
                    "grid": {
                        "type": "object",
                        "properties": {"object": {"type": "string", "reference_to": "geoscience-object"}},
                    }
                },
            }
        )
        self.assertIn("grid: WidgetGrid | ObjectInput = ...", _stubgen._TaskRenderer(spec).render().run_parameters)

    def test_an_object_and_attribute_frame_also_takes_a_typed_attribute(self) -> None:
        """``_frame_from_attribute``: the attribute already knows the object it belongs to."""
        spec = _spec(
            parameters={
                "type": "object",
                "properties": {
                    "source": {
                        "type": "object",
                        "properties": {
                            "object": {"type": "string", "reference_to": "geoscience-object"},
                            "attribute": {"type": "string", "reference_to": "attribute"},
                        },
                    }
                },
            }
        )
        parameters = _stubgen._TaskRenderer(spec).render().run_parameters
        self.assertIn("source: WidgetSource | AnyTypedAttribute = ...", parameters)

    def test_a_target_attribute_slot_also_takes_a_name(self) -> None:
        """``_resolve_target_attribute`` turns a bare string into a create operation."""
        spec = _spec(
            parameters={
                "type": "object",
                "properties": {
                    "attribute": {
                        "target": "attribute",
                        "type": "object",
                        "properties": {"operation": {"const": "create"}, "name": {"type": "string"}},
                    }
                },
            }
        )
        parameters = _stubgen._TaskRenderer(spec).render().run_parameters
        self.assertIn("attribute: WidgetAttribute | AttributeInput = ...", parameters)

    def test_a_shorthand_is_not_offered_for_a_result(self) -> None:
        """Results are what came back, not something a caller may abbreviate."""
        spec = _spec(
            results={
                "type": "object",
                "properties": {
                    "target": {
                        "type": "object",
                        "properties": {"object": {"type": "string", "reference_to": "geoscience-object"}},
                    }
                },
            }
        )
        bodies = "\n".join(generated.body for generated in _stubgen._TaskRenderer(spec).render().types)
        self.assertNotIn("ObjectInput", bodies)

    def test_an_input_and_a_result_shape_are_not_deduplicated_onto_each_other(self) -> None:
        """Identical bodies, different bases: collapsing them would drop the hydrated half."""
        shape = {"type": "object", "properties": {"name": {"type": "string"}}}
        spec = _spec(
            parameters={"type": "object", "properties": {"thing": dict(shape)}},
            results={"type": "object", "properties": {"thing": dict(shape)}},
        )
        bodies = [generated.body for generated in _stubgen._TaskRenderer(spec).render().types]
        bases = [body.split("(", 1)[1].split(")")[0] for body in bodies]
        self.assertEqual(["TypedDict", "ResultNode", "TaskResult"], bases)

    def test_parameters_that_are_not_identifiers_fall_back_to_kwargs(self) -> None:
        spec = _spec(parameters={"type": "object", "properties": {"class": {"type": "string"}}})
        self.assertEqual(["**parameters: Any"], _stubgen._TaskRenderer(spec).render().run_parameters)

    def test_nested_keys_that_are_not_identifiers_use_the_functional_form(self) -> None:
        """And the functional form still says which keys are required -- ``total=False`` would not."""
        rendered = _render(
            parameters={
                "type": "object",
                "properties": {
                    "group": {
                        "type": "object",
                        "title": "Group",
                        "properties": {"class": {"type": "string"}, "import": {"type": "integer"}},
                        "required": ["class"],
                    }
                },
            }
        )
        self.assertIn("WidgetGroup = TypedDict(", rendered)
        self.assertIn('"class": str,', rendered)
        self.assertIn('"import": NotRequired[int],', rendered)
        self.assertNotIn("total=False", rendered)


class TestSnapshotLoading(unittest.TestCase):
    def test_identity_comes_from_the_path(self) -> None:
        tasks = {(task.topic, task.name) for task in _stubgen.load_snapshot(_stubgen.DEFAULT_SNAPSHOT_DIR)}
        self.assertIn(("geostatistics", "kriging-gcp"), tasks)

    def test_empty_snapshot_is_rejected(self) -> None:
        with self.assertRaises(FileNotFoundError):
            _stubgen.generate_stub(Path(__file__).parent / "does-not-exist")

    def test_the_committed_snapshot_is_in_capture_format(self) -> None:
        """So the next refresh diffs the catalogue rather than reformatting every line."""
        for task in _stubgen.load_snapshot(_stubgen.DEFAULT_SNAPSHOT_DIR):
            path = _stubgen.DEFAULT_SNAPSHOT_DIR / task.topic / f"{task.name}.json"
            with self.subTest(task=task.name):
                self.assertEqual(_stubgen._snapshot_json(_stubgen._snapshot_payload(task)), path.read_text())


# The payload from stubs/checks/usage_ok.py, which pyright and mypy accept.
_DECLUSTERING_PAYLOAD: dict = {
    "source": {"object": "https://example.com/objects/samples"},
    "grid": {"object": "https://example.com/objects/grid"},
    "target": {
        "object": "https://example.com/objects/samples",
        "attribute": {"operation": "create", "name": "declustering_weight"},
    },
    "neighborhood": {
        "ellipsoid": {
            "ellipsoid_ranges": {"major": 100.0, "semi_major": 100.0, "minor": 50.0},
            "rotation": {"dip_azimuth": 0.0, "dip": 0.0, "pitch": 0.0},
        },
        "max_samples": 20,
    },
    "power": 2.0,
}


class TestDeepValidationAgreement(unittest.TestCase):
    """The stub and deep validation are the static and runtime halves of one contract.

    Both are derived from the same task schema, so a payload a type checker accepts has to
    pass deep validation, and a mistake the checker catches has to be caught at runtime too.

    They describe two different moments, though, and only for references does that show. The
    stub types a reference by what may be *passed*, because ``ReferenceResolver`` runs before
    submission; deep validation judges the payload *after* it, where a reference is the URL
    string the schema declares. So the agreement to check is that a value which is neither --
    an int where a reference belongs -- is rejected on both sides.
    """

    def _task(self, name: str) -> TaskResource:
        return next(task for task in _stubgen.load_snapshot(_stubgen.DEFAULT_SNAPSHOT_DIR) if task.name == name)

    def test_a_payload_the_stub_accepts_passes_deep_validation(self) -> None:
        validate_parameters(self._task("declustering"), _DECLUSTERING_PAYLOAD, deep=True)  # does not raise

    def test_a_reference_no_handle_could_produce_is_type_checked(self) -> None:
        payload = {**_DECLUSTERING_PAYLOAD, "source": {"object": 12345}}
        with self.assertRaises(ParameterValidationError) as ctx:
            validate_parameters(self._task("declustering"), payload, deep=True)
        self.assertIn("source.object: expected type string", str(ctx.exception))

    def test_a_value_outside_the_stubs_literal_union_is_rejected(self) -> None:
        target = {**_DECLUSTERING_PAYLOAD["target"], "attribute": {"operation": "delete", "name": "weight"}}
        with self.assertRaises(ParameterValidationError) as ctx:
            validate_parameters(self._task("declustering"), {**_DECLUSTERING_PAYLOAD, "target": target}, deep=True)
        self.assertIn("target.attribute", str(ctx.exception))

    def test_every_scalar_reference_parameter_is_type_checked(self) -> None:
        """Generic over the snapshot: every reference is still held to its wire type."""
        checked = 0
        for task in _stubgen.load_snapshot(_stubgen.DEFAULT_SNAPSHOT_DIR):
            schema = task.parameters
            required = list(schema.get("required") or [])
            for name in required:
                prop = schema.get("properties", {}).get(name, {})
                if "reference_to" not in prop or prop.get("type") != "string":
                    continue
                checked += 1
                payload: dict = dict.fromkeys(required, {})
                payload[name] = 12345
                with self.subTest(task=task.name, parameter=name):
                    with self.assertRaises(ParameterValidationError) as ctx:
                        validate_parameters(task, payload, deep=True)
                    self.assertIn(f"{name}: expected type string", str(ctx.exception))
        self.assertTrue(checked, "no scalar reference parameters found in the snapshot")


def _run_type_checker(executable: str, fixture: str) -> tuple[int, str]:
    command = [executable, "--python-executable" if executable == "mypy" else "--pythonpath", sys.executable]
    result = subprocess.run(
        [*command, str(_CHECKS_DIR / fixture)],
        capture_output=True,
        text=True,
        cwd=_CHECKS_DIR.parent.parent,
    )
    return result.returncode, result.stdout + result.stderr


class TestTypeCheckers(unittest.TestCase):
    """End-to-end proof that a real checker reads the stub."""

    def _check(self, executable: str) -> None:
        code, report = _run_type_checker(executable, "usage_ok.py")
        self.assertEqual(0, code, report)

        code, report = _run_type_checker(executable, "usage_bad.py")
        self.assertNotEqual(0, code, "the deliberately broken usage file type-checked cleanly")
        expected = ast.literal_eval(
            next(
                node.value
                for node in ast.parse((_CHECKS_DIR / "usage_bad.py").read_text()).body
                if isinstance(node, ast.Assign) and node.targets[0].id == "EXPECTED_ERRORS"  # type: ignore[attr-defined]
            )
        )
        for name in expected:
            with self.subTest(error=name):
                self.assertIn(name, report)

    def test_pyright(self) -> None:
        """Optional: pyright needs a node runtime, so it is not a test dependency."""
        if shutil.which("pyright") is None:
            self.skipTest("pyright is not installed")
        self._check("pyright")

    def test_mypy(self) -> None:
        """Not optional: mypy is a test dependency so that this runs in CI."""
        self._check("mypy")

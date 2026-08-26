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

"""Offline generator for the compute engine's type stub.

:class:`~evo.compute.engine.ComputeClient` synthesises its topic/task namespace at
runtime from the live discovery catalogue, so a type checker has nothing to read. This
module closes that gap by emitting ``evo/compute/engine.pyi`` from a **snapshot** of the
catalogue that is checked into the repository, giving editors completion, signature help
and hover documentation for every snapshotted task::

    python -m evo.compute._stubgen generate          # rewrite the stub
    python -m evo.compute._stubgen generate --check  # fail if the stub is stale (CI)
    python -m evo.compute._stubgen capture           # refresh the snapshot (needs auth)

``generate`` never authenticates and never touches the network, so the artifact is
reproducible in CI. ``capture`` is the only online step, and it is run by hand when the
snapshot should be brought forward to a newer catalogue.

The runtime stays fully generic: a task published after the snapshot still runs, it is
just not statically known until the snapshot is refreshed. That is the deliberate trade —
total runtime breadth, point-in-time static breadth. :meth:`ComputeClient.arun` is the
typed escape hatch for anything the stub does not know about.
"""

from __future__ import annotations

import argparse
import ast
import json
import keyword
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .endpoints.models import TaskResource

__all__ = [
    "capture_snapshot",
    "generate_stub",
    "load_snapshot",
]

_PACKAGE_DIR = Path(__file__).resolve().parent
DEFAULT_SNAPSHOT_DIR = _PACKAGE_DIR.parents[2] / "stubs" / "snapshot"
"""The catalogue snapshot checked into the repository, next to the package sources."""

DEFAULT_OUTPUT = _PACKAGE_DIR / "engine.pyi"
"""The generated stub, which shadows ``engine.py`` for type checkers."""

_MANIFEST_NAME = "manifest.json"

_LINE_LENGTH = 120

_JSON_SCALAR_TO_PYTHON = {
    "string": "str",
    "integer": "int",
    "number": "float",
    "boolean": "bool",
    "null": "None",
}

# A ``title`` that is already a bare identifier is a model name worth reusing (Pydantic
# emits those); anything else is a human-facing field label such as "Known Locations".
_MODEL_TITLE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")


def _camel(text: str) -> str:
    """``normal-score gcp`` -> ``NormalScoreGcp``."""
    return "".join(part[:1].upper() + part[1:] for part in re.split(r"[^A-Za-z0-9]+", text) if part)


def _identifier(name: str) -> bool:
    return name.isidentifier() and not keyword.iskeyword(name)


def _literal(value: Any) -> str:
    """Render a JSON literal as Python source, double-quoting strings like the formatter."""
    return json.dumps(value) if isinstance(value, str) else repr(value)


def _member_tag(member: Any, index: int) -> str:
    """Name a union member after its discriminating constant, falling back to its position."""
    if isinstance(member, dict):
        for prop in (member.get("properties") or {}).values():
            if isinstance(prop, dict) and isinstance(prop.get("const"), str):
                return prop["const"]
    return str(index)


def _declaration(name: str, annotation: str, indent: str) -> list[str]:
    """Declare ``name: annotation``, splitting an over-long ``Literal`` as the formatter would."""
    line = f"{indent}{name}: {annotation}"
    if len(line) <= _LINE_LENGTH or not (annotation.startswith("Literal[") and annotation.endswith("]")):
        return [line]
    values = ast.literal_eval(f"[{annotation[len('Literal[') : -1]}]")
    return [f"{indent}{name}: Literal[", *(f"{indent}    {_literal(value)}," for value in values), f"{indent}]"]


def _docstring(text: str, indent: str) -> str:
    """Render ``text`` as a syntactically safe triple-quoted docstring."""
    escaped = text.strip().replace("\\", "\\\\").replace('"""', '\\"\\"\\"')
    if escaped.endswith('"'):
        escaped = escaped[:-1] + '\\"'
    lines = [line.rstrip() for line in escaped.splitlines()] or [""]
    if len(lines) == 1:
        return f'{indent}"""{lines[0]}"""'
    body = "\n".join(f"{indent}{line}".rstrip() for line in lines)
    return f'{indent}"""\n{body}\n{indent}"""'


@dataclass
class _GeneratedType:
    name: str
    body: str


@dataclass
class _TaskStub:
    """Everything the emitter needs for one task."""

    topic: str
    attribute: str
    class_name: str
    description: str
    types: list[_GeneratedType]
    run_parameters: list[str]
    return_type: str


@dataclass
class _TaskRenderer:
    """Renders the ``TypedDict`` tree for a single task's parameter and result schemas.

    Every generated name is prefixed with the task, so two tasks never fight over a name.
    Within a task, structurally identical objects collapse onto one type -- the published
    schemas inline the same shape repeatedly (a filter condition appears at four depths in
    the kriging schema), and one name per shape keeps the stub readable.
    """

    spec: TaskResource

    prefix: str = field(init=False)
    section: str = field(default="", init=False)
    types: list[_GeneratedType] = field(default_factory=list, init=False)
    _names: set[str] = field(default_factory=set, init=False)
    _by_body: dict[str, str] = field(default_factory=dict, init=False)
    _by_pointer: dict[str, str] = field(default_factory=dict, init=False)

    def __post_init__(self) -> None:
        self.prefix = _camel(self.spec.name)

    # -- naming ------------------------------------------------------------ #

    def _reserve(self, node: dict[str, Any], path: tuple[str, ...]) -> str:
        """Claim a unique name for an object at ``path``, before its body is rendered."""
        title = node.get("title")
        base = _camel(title if isinstance(title, str) and _MODEL_TITLE.match(title) else "".join(map(_camel, path)))
        # A title that already names the task ('KrigingResult') must not be prefixed twice.
        candidate = base if base.startswith(self.prefix) else f"{self.prefix}{self.section}{base}"
        name, suffix = candidate, 2
        while name in self._names:
            name, suffix = f"{candidate}{suffix}", suffix + 1
        self._names.add(name)
        return name

    def _commit(self, name: str, header: str, body: str) -> str:
        """Record a rendered type, reusing an identical one already generated."""
        if (existing := self._by_body.get(body)) is not None and not re.search(rf"\b{name}\b", body):
            self._names.discard(name)
            return existing
        self._by_body.setdefault(body, name)
        self.types.append(_GeneratedType(name, f"{header}\n{body}"))
        return name

    # -- schema -> annotation ---------------------------------------------- #

    def _typed_dict(
        self,
        node: dict[str, Any],
        root: dict[str, Any],
        path: tuple[str, ...],
        name_override: str | None = None,
    ) -> str:
        name = name_override or self._reserve(node, path)
        self._names.add(name)
        properties: dict[str, Any] = node.get("properties") or {}
        required = set(node.get("required") or [])

        annotations: dict[str, str] = {}
        for prop_name, prop in properties.items():
            annotation = self._annotation(prop, root, (*path, prop_name))
            annotations[prop_name] = annotation if prop_name in required else f"NotRequired[{annotation}]"

        if not all(_identifier(prop_name) for prop_name in properties):
            # A key that isn't a Python identifier can only be declared functionally. The
            # dict stays total, so requiredness still comes from NotRequired alone.
            entries = [f'        "{prop_name}": {annotation},' for prop_name, annotation in annotations.items()]
            body = "\n".join([f'    "{name}",', "    {", *entries, "    },", ")"])
            return self._commit(name, f"{name} = TypedDict(", body)

        lines: list[str] = []
        if description := node.get("description"):
            lines.append(_docstring(str(description), "    "))
            lines.append("")
        for prop_name, prop in properties.items():
            lines.extend(_declaration(prop_name, annotations[prop_name], "    "))
            if isinstance(prop, dict) and (prop_description := prop.get("description")):
                lines.append(_docstring(str(prop_description), "    "))
        return self._commit(name, f"class {name}(TypedDict):", "\n".join(lines))

    def _pointer(self, pointer: str, root: dict[str, Any]) -> str:
        """Resolve a local ``$ref`` to its generated type, defining it on first sight."""
        if (known := self._by_pointer.get(pointer)) is not None:
            return known
        target: Any = root
        for token in pointer.lstrip("#/").split("/"):
            target = target.get(token.replace("~1", "/").replace("~0", "~"), {}) if isinstance(target, dict) else {}
        if not isinstance(target, dict) or not target:
            return "Any"

        path = (pointer.rsplit("/", 1)[-1],)
        if not target.get("properties"):
            return self._annotation(target, root, path)
        # Claim the name before rendering, so a schema that refers back to itself terminates
        # on the same name it will end up with.
        name = self._reserve(target, path)
        self._by_pointer[pointer] = name
        self._by_pointer[pointer] = self._typed_dict(target, root, path, name_override=name)
        return self._by_pointer[pointer]

    def _union(self, members: Any, root: dict[str, Any], path: tuple[str, ...]) -> str:
        if not isinstance(members, list) or not members:
            return "Any"
        annotations = [
            self._annotation(member, root, (*path, _member_tag(member, index)))
            for index, member in enumerate(members, 1)
        ]
        return " | ".join(dict.fromkeys(annotations))

    def _of_type(self, json_type: str, node: dict[str, Any], root: dict[str, Any], path: tuple[str, ...]) -> str:
        if json_type == "object":
            if node.get("properties"):
                return self._typed_dict(node, root, path)
            additional = node.get("additionalProperties")
            if isinstance(additional, dict) and additional:
                return f"dict[str, {self._annotation(additional, root, (*path, 'value'))}]"
            return "dict[str, Any]"
        if json_type == "array":
            items = node.get("items")
            if isinstance(items, dict):
                return f"list[{self._annotation(items, root, (*path, 'item'))}]"
            return "list[Any]"
        return _JSON_SCALAR_TO_PYTHON.get(json_type, "Any")

    def _annotation(self, node: Any, root: dict[str, Any], path: tuple[str, ...]) -> str:
        if not isinstance(node, dict):
            return "Any"
        if (pointer := node.get("$ref")) is not None:
            return self._pointer(str(pointer), root)
        if "const" in node:
            return f"Literal[{_literal(node['const'])}]"
        if (values := node.get("enum")) is not None:
            named = [value for value in values if value is not None]
            literal = f"Literal[{', '.join(map(_literal, named))}]" if named else "None"
            return f"{literal} | None" if len(named) != len(values) else literal
        for combinator in ("oneOf", "anyOf"):
            if combinator in node:
                return self._union(node[combinator], root, path)
        if (members := node.get("allOf")) is not None:
            subschemas = [member for member in members if isinstance(member, dict)]
            return self._annotation(subschemas[0], root, path) if len(subschemas) == 1 else "Any"

        json_type = node.get("type")
        if isinstance(json_type, list):
            annotations = [self._of_type(str(entry), node, root, path) for entry in json_type]
            return " | ".join(dict.fromkeys(annotations))
        if isinstance(json_type, str):
            return self._of_type(json_type, node, root, path)
        return "Any"

    # -- task surface ------------------------------------------------------ #

    def _run_parameters(self) -> list[str]:
        schema = self.spec.parameters or {}
        properties: dict[str, Any] = schema.get("properties") or {}
        required = list(schema.get("required") or [])
        ordered = [*required, *(name for name in properties if name not in required)]
        if not all(_identifier(name) for name in ordered):
            # No keyword-only signature can express these; stay generic for this task.
            return ["**parameters: Any"]

        lines = []
        for name in ordered:
            annotation = self._annotation(properties.get(name, {}), schema, (name,))
            default = "" if name in required else " = ..."
            lines.append(f"{name}: {annotation}{default}")
        lines.append(f"preview: bool = {bool(self.spec.feature_flag)!r}")
        return lines

    def _return_type(self) -> str:
        results = self.spec.results
        if not isinstance(results, dict) or not results.get("properties"):
            return "dict[str, Any]"
        # Everything reachable from the results schema is named ``<Task>Result...``, so an
        # input and an output shape sharing a schema title do not collide.
        self.section = "Result"
        return self._typed_dict(results, results, ("result",), name_override=f"{self.prefix}Result")

    def render(self) -> _TaskStub:
        # Parameters first, so the shapes callers write get the unqualified names.
        run_parameters = self._run_parameters()
        return_type = self._return_type()
        return _TaskStub(
            topic=self.spec.topic,
            attribute=self.spec.name.replace("-", "_"),
            class_name=f"_{_camel(self.spec.topic)}{_camel(self.spec.name)}",
            description=(self.spec.description or "").strip(),
            types=self.types,
            run_parameters=run_parameters,
            return_type=return_type,
        )


def load_snapshot(snapshot_dir: Path) -> list[TaskResource]:
    """Load the catalogue snapshot from ``snapshot_dir``.

    Each task lives at ``<topic>/<task>.json`` and holds the discovery payload verbatim;
    the topic and task name are implied by the path.

    :param snapshot_dir: The directory holding the snapshot.

    :return: The snapshotted tasks, ordered by topic then name.
    """
    tasks: list[TaskResource] = []
    for path in sorted(snapshot_dir.glob("*/*.json")):
        payload = json.loads(path.read_text())
        payload["topic"] = path.parent.name
        payload["name"] = path.stem
        tasks.append(TaskResource.model_validate(payload))
    return tasks


def _snapshot_payload(task: TaskResource) -> dict[str, Any]:
    """The contents of a snapshot file: the discovery payload, less what the path says."""
    payload = task.model_dump(mode="json", exclude_none=True)
    payload.pop("topic", None)
    payload.pop("name", None)
    return payload


def _snapshot_json(payload: dict[str, Any]) -> str:
    """Serialise a snapshot file exactly as the committed snapshot is written.

    The format is pinned -- and checked by ``tests/test_stubgen.py`` -- so that refreshing
    the snapshot shows what changed in the catalogue instead of reformatting every line.
    """
    return json.dumps(payload, indent=2) + "\n"


def _header(snapshot_dir: Path) -> list[str]:
    manifest_path = snapshot_dir / _MANIFEST_NAME
    manifest = json.loads(manifest_path.read_text()) if manifest_path.is_file() else {}
    return [
        "#  Copyright © 2026 Bentley Systems, Incorporated",
        '#  Licensed under the Apache License, Version 2.0 (the "License");',
        "#  you may not use this file except in compliance with the License.",
        "#  You may obtain a copy of the License at",
        "#      http://www.apache.org/licenses/LICENSE-2.0",
        "#  Unless required by applicable law or agreed to in writing, software",
        '#  distributed under the License is distributed on an "AS IS" BASIS,',
        "#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.",
        "#  See the License for the specific language governing permissions and",
        "#  limitations under the License.",
        "",
        "# AUTO-GENERATED BY `python -m evo.compute._stubgen generate`. DO NOT EDIT.",
        "#",
        f"# Catalogue snapshot: {manifest.get('source', 'unknown')}",
        f"# Captured: {manifest.get('captured_at', 'unknown')}",
        "#",
        "# The runtime is always live, so tasks published after the snapshot still run --",
        "# they are simply not statically known until the snapshot is refreshed. Use",
        "# `ComputeClient.arun(topic, task, parameters)` to reach them.",
    ]


def _render_client(stubs: list[_TaskStub]) -> list[str]:
    topics = sorted({stub.topic for stub in stubs})
    lines = [
        "class ComputeClient:",
        _docstring(
            "Instance-bound async entry point to the compute task catalogue.\n\n"
            "The topic and task attributes below come from a point-in-time snapshot of the\n"
            "discovery catalogue. The runtime resolves them live, so a task missing from this\n"
            "stub still runs -- reach it with :meth:`arun`.",
            "    ",
        ),
        "",
        "    def __init__(",
        "        self,",
        "        context: IContext,",
        "        *,",
        "        cache_ttl_seconds: float = ...,",
        "        validate: bool = ...,",
        "        deep_validation: bool = ...,",
        "    ) -> None: ...",
        "    async def arun(",
        "        self,",
        "        topic: str,",
        "        task: str,",
        "        parameters: dict[str, Any],",
        "        *,",
        "        validate: bool | None = ...,",
        "        deep_validation: bool | None = ...,",
        "    ) -> dict[str, Any]:",
        _docstring(
            "Run any task by name, including one this stub does not know about.",
            "        ",
        ),
        "        ...",
        "    def __dir__(self) -> list[str]: ...",
        "    def __repr__(self) -> str: ...",
    ]
    lines.extend(f"    {topic}: _{_camel(topic)}Tasks" for topic in topics)
    return lines


def _render(tasks: list[TaskResource], snapshot_dir: Path) -> str:
    stubs = [_TaskRenderer(task).render() for task in sorted(tasks, key=lambda task: (task.topic, task.name))]

    blocks: list[str] = []
    for stub in stubs:
        blocks.extend(generated.body for generated in stub.types)

    for stub in stubs:
        lines = [f"class {stub.class_name}:"]
        if stub.description:
            lines.append(_docstring(stub.description, "    "))
            lines.append("")
        lines.append("    async def run(")
        lines.append("        self,")
        if stub.run_parameters != ["**parameters: Any"]:
            lines.append("        *,")
        lines.extend(f"        {parameter}," for parameter in stub.run_parameters)
        if stub.description:
            # Repeated on the method as well as the class: editors read the class docstring
            # when hovering the task, and the method docstring in signature help.
            lines.append(f"    ) -> {stub.return_type}:")
            lines.append(_docstring(stub.description, "        "))
            lines.append("        ...")
        else:
            lines.append(f"    ) -> {stub.return_type}: ...")
        blocks.append("\n".join(lines))

    for topic in sorted({stub.topic for stub in stubs}):
        lines = [
            f"class _{_camel(topic)}Tasks:",
            _docstring(f"Tasks published under the ``{topic}`` topic.", "    "),
            "",
        ]
        lines.extend(f"    {stub.attribute}: {stub.class_name}" for stub in stubs if stub.topic == topic)
        blocks.append("\n".join(lines))

    blocks.append("\n".join(_render_client(stubs)))

    body = "\n\n".join(blocks)
    typing_names = ["Any", *(["Literal"] if "Literal[" in body else [])]
    extensions = [name for name in ("NotRequired", "TypedDict") if name in body]
    imports = [
        f"from typing import {', '.join(typing_names)}",
        "",
        "from evo.common import IContext",
        *([f"from typing_extensions import {', '.join(extensions)}"] if extensions else []),
    ]
    preamble = [*_header(snapshot_dir), "", *imports, "", '__all__ = ["ComputeClient"]', ""]
    return "\n".join(preamble) + "\n" + body + "\n"


def generate_stub(snapshot_dir: Path = DEFAULT_SNAPSHOT_DIR, output: Path = DEFAULT_OUTPUT) -> str:
    """Render the engine type stub from a catalogue snapshot.

    :param snapshot_dir: The snapshot to read. No network access is performed.
    :param output: The path the stub is written to, quoted in error messages.

    :return: The stub source.
    """
    tasks = load_snapshot(snapshot_dir)
    if not tasks:
        raise FileNotFoundError(f"no task schemas found in {snapshot_dir}")
    return _render(tasks, snapshot_dir)


def capture_snapshot(snapshot_dir: Path = DEFAULT_SNAPSHOT_DIR) -> list[TaskResource]:
    """Refresh the catalogue snapshot from live discovery.

    This is the one online step, run by hand when the stub should move to a newer
    catalogue. Credentials come from ``EVO_ACCESS_TOKEN``, ``EVO_HUB_URL`` and
    ``EVO_ORG_ID``.

    :param snapshot_dir: The directory to write the snapshot to.

    :return: The tasks written.
    """
    import asyncio
    import os
    from datetime import date
    from uuid import UUID

    from evo.aio import AioTransport
    from evo.common import APIConnector
    from evo.oauth import AccessTokenAuthorizer

    from .discovery import DiscoveryClient

    async def _fetch() -> list[TaskResource]:
        transport = AioTransport(user_agent="evo-compute-stubgen")
        authorizer = AccessTokenAuthorizer(access_token=os.environ["EVO_ACCESS_TOKEN"])
        connector = APIConnector(base_url=os.environ["EVO_HUB_URL"], transport=transport, authorizer=authorizer)
        async with connector:
            return await DiscoveryClient(connector, UUID(os.environ["EVO_ORG_ID"])).list_tasks()

    tasks = sorted(asyncio.run(_fetch()), key=lambda task: (task.topic, task.name))
    for stale in snapshot_dir.glob("*/*.json"):
        stale.unlink()
    for task in tasks:
        path = snapshot_dir / task.topic / f"{task.name}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_snapshot_json(_snapshot_payload(task)))

    manifest = {
        "source": "GET /compute/orgs/{org_id}/tasks?details=true",
        "captured_at": date.today().isoformat(),
        "tasks": [{"topic": task.topic, "name": task.name, "version": task.version} for task in tasks],
    }
    (snapshot_dir / _MANIFEST_NAME).write_text(_snapshot_json(manifest))
    return tasks


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m evo.compute._stubgen", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    generate = commands.add_parser("generate", help="render the stub from the snapshot (offline)")
    generate.add_argument("--snapshot", type=Path, default=DEFAULT_SNAPSHOT_DIR)
    generate.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    generate.add_argument("--check", action="store_true", help="fail if the stub is out of date")

    capture = commands.add_parser("capture", help="refresh the snapshot from live discovery (needs auth)")
    capture.add_argument("--snapshot", type=Path, default=DEFAULT_SNAPSHOT_DIR)

    arguments = parser.parse_args(argv)
    if arguments.command == "capture":
        tasks = capture_snapshot(arguments.snapshot)
        print(f"captured {len(tasks)} tasks to {arguments.snapshot}")
        return 0

    stub = generate_stub(arguments.snapshot, arguments.output)
    if arguments.check:
        current = arguments.output.read_text() if arguments.output.is_file() else ""
        if current != stub:
            print(f"{arguments.output} is out of date; run `python -m evo.compute._stubgen generate`", file=sys.stderr)
            return 1
        print(f"{arguments.output} is up to date")
        return 0

    arguments.output.write_text(stub)
    print(f"wrote {arguments.output} ({len(stub.splitlines())} lines)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

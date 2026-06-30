"""Generate the ``.pyi`` stub that types the authenticated client .

The dynamic namespace hangs off a
``ComputeClient`` INSTANCE, so the stub types the client class tree:

    ComputeClient.geostatistics: _GeostatisticsNamespace
    _GeostatisticsNamespace.kriging: _GeostatisticsKriging
    _GeostatisticsKriging.run(self, *, source: str, ...) -> KrigingResult

Everything is emitted into a single ``poc_compute_engine/__init__.pyi``.

Stub generation is **offline** — it reads a point-in-time snapshot of task schemas
checked into ``poc_compute_engine/schemas/<topic>/<task>/schema.json`` (copied verbatim
from the platform's discovery, whose ``TaskResource`` shape these mirror). No auth and no network: a build
artifact must be reproducible in CI without credentials. The *runtime* is always live, so
the platform may advertise more tasks than the snapshot knows — that gap is the whole
point of the generic engine (see the demo notebook).
"""

from __future__ import annotations

import json
from pathlib import Path

_SCHEMAS_DIR = Path(__file__).resolve().parent / "poc_compute_engine" / "schemas"

_JSON_TO_PY = {
    "string": "str",
    "integer": "int",
    "number": "float",
    "boolean": "bool",
    "object": "dict",
    "array": "list",
}

_HEADER = "# AUTO-GENERATED OFFLINE FROM poc_compute_engine/schemas/ (a discovery snapshot). DO NOT EDIT BY HAND.\n"


def _load_bundled_specs() -> list[dict]:
    """Load the snapshotted task schemas, injecting topic/name from the directory path.

    The platform's discovery returns ``topic``/``name`` on every ``TaskResource``; the
    checked-in ``schema.json`` files omit them (they are implied by their location), so we
    re-attach them here to match the live shape the engine consumes.
    """
    specs: list[dict] = []
    for schema_path in sorted(_SCHEMAS_DIR.glob("*/*/schema.json")):
        spec = json.loads(schema_path.read_text())
        spec["topic"] = schema_path.parent.parent.name
        spec["name"] = schema_path.parent.name
        specs.append(spec)
    return specs


def _annotation(prop: dict) -> str:
    if "enum" in prop:
        return f"Literal[{', '.join(repr(v) for v in prop['enum'])}]"
    t = prop.get("type", "Any")
    if isinstance(t, list):
        parts = [_JSON_TO_PY.get(x, "Any") if x != "null" else "None" for x in t]
        return " | ".join(dict.fromkeys(parts))
    return _JSON_TO_PY.get(t, "Any")


def _cap(name: str) -> str:
    return "".join(part.capitalize() for part in name.replace("-", "_").split("_"))


def _result_class(task: str) -> str:
    return _cap(task) + "Result"


def _render_result_classes(spec: dict, classes: list[str]) -> str:
    """Append the result class hierarchy; return the top-level class name."""
    top = _result_class(spec["name"])
    results = spec.get("results")
    if not results:
        classes.append(f"class {top}:\n    message: str\n")
        return top
    _render_object(top, results, classes)
    return top


def _render_object(name: str, node: dict, classes: list[str]) -> None:
    props: dict = node.get("properties", {})
    fields: list[str] = []
    for fname, fspec in props.items():
        if isinstance(fspec, dict) and (fspec.get("type") == "object" or "properties" in fspec):
            child = name + _cap(fname)
            _render_object(child, fspec, classes)  # child defined before parent
            fields.append(f"    {fname}: {child}")
        else:
            fields.append(f"    {fname}: {_annotation(fspec)}")
    body = [f"class {name}:"]
    body.extend(fields or ["    ..."])
    if node.get("output") == "geoscience-object":
        body.append("    def get_object(self) -> GeoscienceObject: ...")
        body.append("    def to_dataframe(self) -> Table: ...")
    classes.append("\n".join(body))


def _task_class(topic: str, task: str) -> str:
    return f"_{_cap(topic)}{_cap(task)}"


def _namespace_class(topic: str) -> str:
    return f"_{_cap(topic)}Namespace"


def _render_task_proxy(spec: dict, result_cls: str) -> str:
    schema = spec["parameters"]
    props: dict = schema.get("properties", {})
    required = list(schema.get("required", []))
    optional = [n for n in props if n not in required]
    lines = [f"class {_task_class(spec['topic'], spec['name'])}:", "    async def run("]
    lines.append("        self,")
    lines.append("        *,")
    for name in required:
        lines.append(f"        {name}: {_annotation(props[name])},")
    for name in optional:
        lines.append(f"        {name}: {_annotation(props[name])} = ...,")
    lines.append(f"        preview: bool = {bool(spec.get('feature_flag'))!r},")
    lines.append(f"    ) -> {result_cls}:")
    desc = spec.get("description", "").strip()
    lines.append(f'        """{desc}"""')
    lines.append("        ...")
    return "\n".join(lines)


def _override_module(root: Path, topic: str, task: str) -> str | None:
    """Return the dotted module path of a per-task override, or ``None``.

    An override at ``overrides/<topic>/<task>.py`` OWNS the task: the stub re-exports its
    hand-written runner + result type instead of emitting schema-derived ones, so static
    DX matches the specialized runtime surface exactly.
    """
    mod = task.replace("-", "_")
    if (root / "overrides" / topic / f"{mod}.py").exists():
        return f"poc_compute_engine.overrides.{topic}.{mod}"
    return None


def generate(root: Path, specs: list[dict]) -> Path:
    by_topic: dict[str, list[dict]] = {}
    for spec in specs:
        by_topic.setdefault(spec["topic"], []).append(spec)

    result_classes: list[str] = []
    task_proxies: list[str] = []
    namespaces: list[str] = []
    client_attrs: list[str] = []
    override_imports: list[str] = []

    for topic in sorted(by_topic):
        topic_specs = sorted(by_topic[topic], key=lambda s: s["name"])
        ns_fields: list[str] = []
        for spec in topic_specs:
            task = spec["name"]
            task_cls = _task_class(topic, task)
            override = _override_module(root, topic, task)
            if override is not None:
                # Re-export the override's hand-written types: its runner becomes the task
                # class, its result type the result. No schema-derived stub for this task.
                result_cls = _result_class(task)
                runner_cls = _cap(task) + "Runner"
                override_imports.append(
                    f"from {override} import (\n"
                    f"    {result_cls} as {result_cls},\n"
                    f"    {runner_cls} as {task_cls},\n"
                    f")"
                )
            else:
                result_cls = _render_result_classes(spec, result_classes)
                task_proxies.append(_render_task_proxy(spec, result_cls))
            ns_fields.append(f"    {task.replace('-', '_')}: {task_cls}")
        ns = [f"class {_namespace_class(topic)}:", *ns_fields]
        namespaces.append("\n".join(ns))
        client_attrs.append(f"    {topic}: {_namespace_class(topic)}")

    parts: list[str] = [
        _HEADER.rstrip("\n"),
        "from typing import Any, Literal",
        "",
        "from evo.common import IContext",
        "from poc_compute_engine.discovery import DiscoveryClient as DiscoveryClient",
        "from poc_compute_engine.engine import (",
        "    GeoscienceObject as GeoscienceObject,",
        "    Table as Table,",
        "    TaskResult as TaskResult,",
        ")",
        *(["", "\n".join(override_imports)] if override_imports else []),
        "",
        "\n\n".join(result_classes),
        "",
        "\n\n".join(task_proxies),
        "",
        "\n\n".join(namespaces),
        "",
        "class ComputeClient:",
        "    def __init__(self, context: IContext) -> None: ...",
        "    @classmethod",
        "    async def connect(cls, context: IContext) -> ComputeClient: ...",
        "    async def aclose(self) -> None: ...",
        "    async def __aenter__(self) -> ComputeClient: ...",
        "    async def __aexit__(self, *exc: object) -> None: ...",
        "    async def refresh(self) -> None: ...",
        *client_attrs,
        "",
    ]
    out = root / "__init__.pyi"
    out.write_text("\n".join(parts) + "\n")
    return out


if __name__ == "__main__":
    # Offline: read the snapshotted schemas and emit the typed stub. No auth, no network.
    specs = _load_bundled_specs()
    pkg_root = Path(__file__).resolve().parent / "poc_compute_engine"
    written = generate(pkg_root, specs)
    print(f"wrote {written.relative_to(pkg_root.parent)} ({len(specs)} tasks)")

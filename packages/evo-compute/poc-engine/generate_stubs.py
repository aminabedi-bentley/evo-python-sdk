"""Generate ``.pyi`` type stubs from the (mock) discovery schemas.

Running this writes:

    poc_compute_engine/__init__.pyi                      # declares each topic
    poc_compute_engine/<topic>/__init__.pyi              # re-exports each task module
    poc_compute_engine/<topic>/<task>.pyi                # typed run(...) + Result

The runtime stays generic (see poc_compute_engine/_engine.py); these stubs only add the
*static* developer experience (IDE autocomplete, signature help, type-checking).
Re-run after the schema changes to refresh the static surface.
"""

from __future__ import annotations

from pathlib import Path

from poc_compute_engine.mock_discovery import fetch_discovery

_JSON_TO_PY = {
    "string": "str",
    "integer": "int",
    "number": "float",
    "boolean": "bool",
    "object": "dict",
    "array": "list",
}

_HEADER = "# AUTO-GENERATED FROM DISCOVERY SCHEMAS. DO NOT EDIT BY HAND.\n"


def _annotation(prop: dict) -> str:
    if "enum" in prop:
        inner = ", ".join(repr(v) for v in prop["enum"])
        return f"Literal[{inner}]"
    t = prop.get("type", "Any")
    if isinstance(t, list):
        parts = [_JSON_TO_PY.get(x, "Any") if x != "null" else "None" for x in t]
        return " | ".join(dict.fromkeys(parts))
    return _JSON_TO_PY.get(t, "Any")


def _cap(name: str) -> str:
    return "".join(part.capitalize() for part in name.replace("-", "_").split("_"))


def _class_name(task: str) -> str:
    return _cap(task) + "Result"


def _render_result_classes(spec: dict) -> tuple[str, str]:
    """Render the result class hierarchy from the `results` schema.

    Returns (rendered_classes, top_level_class_name). Object nodes tagged
    ``output: geoscience-object`` gain ``get_object``/``to_dataframe`` methods,
    matching the generic engine's runtime result.
    """
    top = _class_name(spec["name"])
    results = spec.get("results")
    if not results:
        return f"class {top}:\n    message: str\n", top
    classes: list[str] = []
    _render_object(top, results, classes)
    return "\n\n".join(classes) + "\n", top


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


def _render_task_stub(spec: dict) -> str:
    schema = spec["parameters"]
    props: dict = schema.get("properties", {})
    required = list(schema.get("required", []))
    optional = [n for n in props if n not in required]
    result_classes, result_cls = _render_result_classes(spec)

    lines = [
        _HEADER,
        "from typing import Any, Literal\n",
        "from poc_compute_engine._engine import GeoscienceObject, Table\n",
        "",
        result_classes,
        "def run(",
        "    *,",
    ]
    for name in required:
        lines.append(f"    {name}: {_annotation(props[name])},")
    for name in optional:
        lines.append(f"    {name}: {_annotation(props[name])} = ...,")
    lines.append(f"    preview: bool = {bool(spec.get('feature_flag'))!r},")
    lines.append(f") -> {result_cls}:")
    desc = spec.get("description", "").strip()
    lines.append(f'    """{desc}"""')
    lines.append("    ...")
    lines.append("")
    return "\n".join(lines)


def generate(root: Path) -> list[Path]:
    payload = fetch_discovery()
    by_topic: dict[str, list[dict]] = {}
    for spec in payload["results"]:
        by_topic.setdefault(spec["topic"], []).append(spec)

    written: list[Path] = []

    # Top-level: declare each topic.
    top = [_HEADER]
    for topic in sorted(by_topic):
        top.append(f"from . import {topic} as {topic}")
    top_path = root / "__init__.pyi"
    top_path.write_text("\n".join(top) + "\n")
    written.append(top_path)

    # Per topic: a stub-only package re-exporting each task module.
    for topic, specs in sorted(by_topic.items()):
        tdir = root / topic
        tdir.mkdir(exist_ok=True)
        init = [_HEADER]
        for spec in sorted(specs, key=lambda s: s["name"]):
            mod = spec["name"].replace("-", "_")
            init.append(f"from . import {mod} as {mod}")
        (tdir / "__init__.pyi").write_text("\n".join(init) + "\n")
        written.append(tdir / "__init__.pyi")

        for spec in specs:
            mod = spec["name"].replace("-", "_")
            path = tdir / f"{mod}.pyi"
            path.write_text(_render_task_stub(spec))
            written.append(path)

    return written


if __name__ == "__main__":
    pkg_root = Path(__file__).resolve().parent / "poc_compute_engine"
    for path in generate(pkg_root):
        print(f"wrote {path.relative_to(pkg_root.parent)}")

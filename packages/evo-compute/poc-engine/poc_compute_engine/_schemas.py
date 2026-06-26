"""Loader for the **bundled, offline** task schemas.

The POC ships a snapshot of ``core-compute-tasks`` schema files under
``poc-engine/schemas/<topic>/<task>/schema.json``. This single snapshot is the
source of truth for BOTH:

* stub generation (``generate_stubs.py``) — purely offline, no network, and
* runtime resolution/invocation (``_engine.py``) for bundled tasks.

Using one snapshot for both prevents the stub/runtime drift the POC originally
hit. ``topic`` and ``name`` are derived from the directory layout, so the JSON
files only carry the task contract (version / feature_flag / parameters / results).

Live discovery (``mock_discovery.fetch_discovery``) is consulted *only* for tasks
that are NOT in this bundle.
"""

from __future__ import annotations

import functools
import json
from pathlib import Path

_SCHEMAS_DIR = Path(__file__).resolve().parent.parent / "schemas"


@functools.lru_cache(maxsize=1)
def load_bundled_specs() -> list[dict]:
    """Read every ``schemas/<topic>/<task>/schema.json`` into a discovery-shaped spec."""
    specs: list[dict] = []
    for path in sorted(_SCHEMAS_DIR.glob("*/*/schema.json")):
        spec = json.loads(path.read_text())
        spec["topic"] = path.parent.parent.name
        spec["name"] = path.parent.name
        specs.append(spec)
    return specs


def get_spec(topic: str, name: str) -> dict | None:
    for spec in load_bundled_specs():
        if spec["topic"] == topic and spec["name"] == name:
            return spec
    return None

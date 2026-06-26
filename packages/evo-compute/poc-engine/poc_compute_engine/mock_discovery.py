"""A stand-in for the Evo compute **live Discovery API**.

In the real system this data comes from
``GET /compute/orgs/{org_id}/tasks?details=true``. Here it is an in-memory store
so the POC depends on no Seequent repo and no network.

IMPORTANT (per the new architecture): discovery is consulted **only for tasks
that are NOT bundled** as offline ``schema.json`` files. Bundled tasks (kriging,
declustering, normal_score) are loaded from ``poc_compute_engine._schemas`` and
have generated stubs; discovery exists purely to prove *live breadth* — tasks the
platform advertises after stubs were generated still run, just without stubs.
"""

from __future__ import annotations

import copy

# Tasks advertised live by the platform that are NOT in the offline bundle.
# Starts empty; the demo registers `turning_bands` to show a stub-less task
# running purely from the live discovery schema.
_RUNTIME_TASKS: list[dict] = []


def fetch_discovery() -> dict:
    """Return the LIVE discovery payload (non-bundled tasks only)."""
    results = copy.deepcopy(_RUNTIME_TASKS)
    return {"total": len(results), "count": len(results), "results": results}


def register_runtime_task(spec: dict) -> None:
    """Simulate the platform advertising a *new* task after stubs were generated."""
    _RUNTIME_TASKS.append(spec)

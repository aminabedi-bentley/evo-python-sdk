"""Runtime package root (authenticated, instance-bound).

The poc-engine has **no module-level dynamic namespace**. The dynamic
namespace hangs off an authenticated ``ComputeClient`` *instance*:

    from evo.notebooks import ServiceManagerWidget
    from poc_compute_engine import ComputeClient

    manager = await ServiceManagerWidget.with_auth_code(client_id=...).login()
    from poc_compute_engine import ComputeClient

    async with ComputeClient(manager) as client:    # discovery + auth happen HERE (fail-fast)
        await client.geostatistics.kriging_gcp.run(...)   # every call authenticated via the context

``ComputeClient`` accepts any real ``evo.common.IContext`` (``ServiceManagerWidget`` is
one), so the whole stack is the *real* SDK over real HTTP against the real Evo compute
platform — no test doubles. Both halves of its platform I/O are delegated to dedicated
connector-backed clients: EXECUTION to the real ``evo.compute.JobClient`` and DISCOVERY to
``DiscoveryClient`` (also exported here — the ``list_tasks`` capability the SDK still
lacks, usable standalone too).
"""

from __future__ import annotations

from .discovery import DiscoveryClient
from .engine import ComputeClient

__all__ = ["ComputeClient", "DiscoveryClient"]

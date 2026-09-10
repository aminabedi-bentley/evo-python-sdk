<p align="center"><a href="https://seequent.com" target="_blank"><picture><source media="(prefers-color-scheme: dark)" srcset="https://developer.seequent.com/img/seequent-logo-dark.svg" alt="Seequent logo" width="400" /><img src="https://developer.seequent.com/img/seequent-logo.svg" alt="Seequent logo" width="400" /></picture></a></p>
<p align="center">
    <a href="https://pypi.org/project/evo-compute/"><img alt="PyPI - Version" src="https://img.shields.io/pypi/v/evo-compute" /></a>
    <a href="https://github.com/SeequentEvo/evo-python-sdk/actions/workflows/run-all-tests.yaml"><img src="https://github.com/SeequentEvo/evo-python-sdk/actions/workflows/run-all-tests.yaml/badge.svg" alt="" /></a>
</p>
<p align="center">
    <a href="https://developer.seequent.com/" target="_blank">Seequent Developer Portal</a>
    &bull; <a href="https://community.seequent.com/group/19-evo" target="_blank">Seequent Community</a>
    &bull; <a href="https://seequent.com" target="_blank">Seequent website</a>
</p>

# Evo Compute Task Client

The Compute Task API provides the ability to execute computation tasks in Evo that require variable, on-demand processing power. Building the Compute Task API into your application can enable fast processing of long running, or resource intensive operations, without depending on the end user's physical hardware.

Tasks are created, triggering a job to be executed asynchronously within a specific topic for your organization, and can be monitored throughout their execution lifecycle.

## Pre-requisites

* Python ^3.10
* An [application registered in Bentley](https://developer.bentley.com/register/?product=seequent-evo)

## Installation

```shell
pip install evo-compute
```

## Usage

See [the evo-sdk-common documentation](https://github.com/SeequentEvo/evo-python-sdk/blob/main/packages/evo-sdk-common/README.md)
for information on how to authenticate, then select the organisation, hub and workspace that you would like to use.

### Interacting with the Compute Task API

To get up and running quickly with the Evo Compute Task SDK, start by configuring your
[environment and API connector](https://github.com/SeequentEvo/evo-python-sdk/blob/main/packages/evo-sdk-common/docs/quickstart.md).

For some interactive Jupyter notebook examples, see the [examples folder](docs/examples).

### Import a task directly

Authenticate and prepare the context through the SDK's async APIs, then load inputs and run
compute on that same event loop. With the prepared context and parameter model, import a task
instead of constructing a client:

```python
from evo.compute.tasks.geostatistics import Kriging

result = await Kriging.arun(manager, parameters, preview=True)
```

`parameters` can be an existing Pydantic parameter model or a dictionary. Alternatively, pass
the task's named arguments directly. Do not combine a parameter model or dictionary with named
task arguments; execution options such as `preview` remain separate.

Calls prefer the same bespoke runner as the client namespace. For those tasks, dictionaries
use the runner's argument names and input forms, such as `search: SearchNeighborhood` and
`method` for `Kriging`. Parameter models retain their native fields through dispatch, so their
wire aliases, filters, and diagnostics are serialized by the bespoke runner rather than lost.

The imported value is an immutable task identity, not a client or a manager singleton. Equal
task identities need not be the same Python object. Imports do not mutate package namespaces
or retain a global handle or catalogue cache, and do not authenticate or fetch discovery.
Calls reuse clients and synchronization owned by the supplied context; changing its connector
or organization replaces the clients. Contexts without a writable instance namespace still
work, without this cache. Async clients are also scoped to their event loop.

Input loading is async application setup, using the existing SDK loaders:

```python
from evo.objects.typed import object_from_reference

samples = await object_from_reference(manager, samples_reference)
variogram = await object_from_reference(manager, variogram_reference)
target_model = await object_from_reference(manager, target_reference)
```

`arun()` uses the caller's event loop. An open connection must remain on its own loop; the
facade does not move or clone its transport. The [synchronous Kriging example](docs/examples/kriging_sync.ipynb)
uses `ServiceManagerWidget` and awaited `object_from_uuid()` calls, matching the other Kriging
notebooks. It then closes the widget's open connection once, on the notebook's loop, before
calling `Kriging.run(...)`. The connector opens fresh sessions for blocking compute and generic
result loaders, so neither requires `await`. Authentication and input loading remain async.

`run()` remains a blocking compatibility API for a context compatible with the private bridge.
The bridge still owns one process-wide event loop so repeated blocking calls and generic result
loaders keep their loop-bound connections alive. Removing that shared lifetime would require
redesigning ownership across the blocking APIs. The pre-existing `TaskRegistry` singleton also
remains for legacy model-based dispatch and automatic runner registration; removing it would
change that registration contract. Neither is required for async context or input preparation.

Results retain the existing client's contract. Generic blocking results have blocking loaders;
bespoke task results retain their own types and loader behavior, including asynchronous loaders.
The synchronous notebook wraps a bespoke target reference in a public `SyncResultNode` to load
its object and dataframe without `await`, keeping the handwritten result API unchanged.
Existing lowercase task-module imports and the legacy `await tasks.run(manager, parameters)`
API are unchanged. Unique task names are also importable from `evo.compute.tasks`.

For tasks missing from the shipped import index, or names shared by multiple topics, use the
live-discovery escape hatch:

```python
from evo.compute import task

result = await task("new-topic", "new-task").arun(manager, parameters)
```

This is an additional entry point over the existing generic engine, not a generated execution
wrapper. The task index contains identities only; discovery still supplies the live schemas,
preview defaults, and execution contract. It follows the design document's separation of
[live execution and point-in-time hints](https://seequent.atlassian.net/wiki/spaces/PLT/pages/2390097923).

### Static typing for the generic engine

`ComputeClient` resolves `client.<topic>.<task>.run(...)` from the live task catalogue, so
there is no hand-written code for a type checker to read. A generated type stub gives
editors completion, signature help and parameter checking for every task in a checked-in
catalogue snapshot, while execution stays fully generic. The same generator emits imported-task
signatures and result types without generating runtime wrappers. See
[stubs/README.md](stubs/README.md) for how to regenerate it and refresh the snapshot.

For compatible neighborhood schemas, both clients and imported task signatures accept the
existing `SearchNeighborhood` model alongside schema-shaped dictionaries. Its native
`Ellipsoid`, ranges and rotation objects keep their existing serialization; the type hints
describe behavior the resolver already supports. Matching is based on serialized field
structure, not a task or schema name. Unrelated models and malformed dictionaries are still
rejected by the type checkers. Existing complete parameter models remain supported too.

## Contributing

For instructions on contributing to the development of this library, please refer to the [evo-python-sdk documentation](https://github.com/seequentevo/evo-python-sdk).

## License

The Python SDK for Evo is open source and licensed under the [Apache 2.0 license.](./LICENSE.md).

Copyright © 2025 Bentley Systems, Incorporated.

Licensed under the Apache License, Version 2.0 (the "License").
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.

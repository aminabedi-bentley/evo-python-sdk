# Bundled task schemas (point-in-time snapshot)

These `schema.json` files are a **point-in-time capture** of the schemas, copied verbatim from the source (`parameters` /
`results` / `description` / `feature_flag` / ...). They are laid out by convention as
`<topic>/<task>/schema.json`; `topic` and `name` are implied by the directory path and
re-attached when loaded. Directory names mirror the **live** task names (e.g.
`geostatistics/kriging-gcp`), so the generated stubs line up with what discovery returns.

**They reflect the last SDK release, not the live platform.** The engine itself always
reads the catalogue *live from discovery* at runtime, so it surfaces every task the
platform currently advertises — including ones added after this snapshot. This snapshot
exists only to drive the **offline** typed-stub generation (`generate_stubs.py` →
`poc_compute_engine/__init__.pyi`), which must be reproducible in CI with no auth and no
network.

The consequence is deliberate: **total runtime breadth, point-in-time static breadth.**
A task advertised after this capture still runs; it just isn't statically typed until the
snapshot is refreshed and the stubs regenerated (cut a new SDK release).

## Refreshing the snapshot

When the platform's task schemas change (typically at an SDK release), re-capture the
relevant `schema.json` files here from the source of truth (e.g. task package in core-compute-tasks), then regenerate the stub:

```bash
python generate_stubs.py   # reads this directory -> emits poc_compute_engine/__init__.pyi
```

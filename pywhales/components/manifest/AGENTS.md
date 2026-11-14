# Manifest List Contracts

`cli_wrapper.py` defines `ManifestCLI`, `ManifestList`, and the strict snapshot helper;
`models.py` defines `ManifestListInspectResult`. These are not local `Image` handles.
Registry imagetools has a separate contract in [the Buildx guide](../buildx/AGENTS.md).

## Handles And Public Commands

- `ManifestList` keeps its exact reference and `insecure` setting through inspect/reload.
  Context exit removes the local list through shared resource cleanup; see [the components guide](../AGENTS.md).
- Public `inspect()` constructs a handle through the existing lenient parsing path.
  Do not silently route it through strict inventory validation or change accepted wire values.
- `inspect(..., verbose=True)` is a keyword-only `Literal` overload: one `manifest inspect <ref> [--insecure] --verbose` launch
  returning the CLI's JSON-formatted text unparsed. It builds no `ManifestList`, so no cache is seeded; the default keeps the handle.
- `create()` retains its public `ammend` spelling, mapped to Docker's `--amend` flag.
- `push(..., insecure=True)` is keyword-only. It emits `--insecure` on the push and keeps it on the preflight inspection;
  the default argv of both launches is unchanged.
- Empty `remove()` remains a no-op. Push preflight and per-launch deadline semantics stay in the parent guide.

## Strict Inventory Inspection

- `_inspect_manifest_list_snapshot()` is the helper-owned strict boundary used by inventory/bulk reload,
  separate from public inspect. A missing/empty reference fails before launch.
- Inspect exactly one reference per CLI launch. A second positional argument means a manifest inside
  that list, not another list; never feed this path into generic plural/batch inspect.
- Require one JSON object with strict model validation, not a list. Bind the requested reference as snapshot
  `name`; accept an absent name but reject a conflicting supplied name rather than rebinding identity.
- Forward `--insecure` only from the requesting handle; preserve it on staged hydrated handles.
  Bare inventory requests do not gain insecure transport from unrelated handles or group state.
- Safe parse failures expose no response dump and suppress input-bearing exception chains;
  raw diagnostics are the explicit opt-in, not permission to change payload validation.
- Exact-config grouping, all-group staging before cache publication, detached ordered records,
  duplicate preservation and per-launch limits follow [the runtime guide](../../AGENTS.md#bulk-reload-and-inventory).

## Regression References And Selection

Paths are relative to `tests/pywhales/`; [the test guide](../../../tests/AGENTS.md) owns shared fixture cautions.
- `test_inventory.py`: `test_strict_manifest_inspect_binds_the_reference_as_the_name`,
  `test_public_manifest_inspect_stays_lenient`, malformed responses, insecure forwarding and detached duplicates.
- `test_inventory_cli.py`: one launch per reference, handle-only insecure flags, later-group failure
  preserving original caches, and distinct-handle bulk publication through a fake local Python CLI.
- `test_bulk_reload.py`: manifest per-handle launch and once-only publication tests.
- `components/test_manifest.py` mixes model/mocked timeout checks with live registry fixtures.
  Those fixtures pull fixed tags and remove them, potentially removing pre-existing tags; inspect the exact test first.
- Do not call a broad component selection daemon-free merely because markers are deselected;
  shared fixture probes and other modules' collection-time availability checks are separate hazards.

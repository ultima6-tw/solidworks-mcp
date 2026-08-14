# Audit demo reliability and selection plan

Reviewed the SolidWorks MCP server at committed baseline `e55e82e` in the isolated worktree/branch `codex/demo-reliability-plan`. No server behavior was changed; the deliverable is the code-backed plan in `docs/meeting-demo-reliability-plan.md`.

## Findings

- The semantic query discussed earlier does exist: `solidworks_find_face`, feature-tagged face/edge queries, labeled screenshots, and direct `IEntity::Select4` selection paths are implemented.
- Query outputs are not durable entity selectors. They return transient indices and points, which are re-resolved with a hard-coded 1 mm nearest-entity tolerance.
- Several reference-geometry operations ignore failed edge/face/vertex selections before calling the feature API.
- Assembly coordinate mate picks still use camera-dependent `SelectByID2`.
- Most feature creators do not use the existing shared rebuild/error verifier, so a returned feature object can be reported as success without a clean rebuild/postcondition check.
- `find_face`, visual query behavior, camera independence, rebuild-stable identity, and assembly direct-object selection lack live regressions.
- Hole Wizard advertises `fastenerType` and `positions` but does not consume them, and it remains vulnerable to a blocking dialog.
- Import-time file logging assumes the source directory is writable.
- Meaningful naming is already implemented and strongly encouraged: `rename_feature` verifies the rename and re-keys tracked IDs, while `get_feature_tree` warns about default names. The missing pieces are atomic optional `name` inputs on creators and an end-to-end live regression.

## Design decision

Proposed a versioned, document-bound entity-reference contract backed by SOLIDWORKS persistent reference IDs (`GetPersistReference3` / `GetObjectByPersistReference3`). Query tools should return the reference plus human-readable geometry; mutators should validate document/configuration/kind, resolve the exact entity, select it with `Select4`, and fail explicitly on stale or ambiguous topology. Coordinate inputs remain compatibility fallbacks.

The Tuesday-safe scope prioritizes checked selections, rebuild/postcondition verification for the exact demo mutators, purpose-oriented feature names immediately after creation, a preflight, and a 10-run exact-workflow soak. Hole Wizard and coordinate-picked assembly entity mates are excluded unless separately hardened and soaked.

## Verification

- `python test.py --list`: PASS; discovered 43 live-COM scenarios without connecting to or mutating SOLIDWORKS.
- `python -m unittest discover -s tests -v`: PASS with normal write permissions; 35 passed and 5 skipped because the published C# worker was absent.
- Initial sandboxed unit attempt: environment-blocked by denied writes to the worktree log and temporary SQLite files. The same suite passed once granted normal worktree/temp access, so this was not counted as a product test failure.
- Full live COM suite: intentionally not run because its setup closes open SOLIDWORKS documents and could disrupt active work.

## Reporting

Created and registered the Discord forum thread `SolidWorks MCP Demo Reliability Review 2026-08-14` (`1537811807941435403`). Posted the initial audit status, a follow-up on semantic feature naming, and the completed handoff (`1537814215841292291`). The webhook credential was never printed, logged, or copied into repository files.

# SolidWorks MCP demo reliability review and build plan

**Review date:** 2026-08-14  
**Demo date:** Tuesday, 2026-08-18  
**Baseline:** `e55e82e` (`feat/filemanagement`)  
**Review branch:** `codex/demo-reliability-plan`  
**Scope:** static code review plus non-destructive unit checks; no live COM suite was run because it closes open SOLIDWORKS documents.

## Executive outcome

The face-query idea was implemented. The server currently has:

- `solidworks_find_face`, with orientation, owning-feature, surface-type, and area-rank filters (`solidworks/geometry_query.py:121`, `:221`);
- feature-tagged `get_faces` and `get_edges` results (`solidworks/geometry_query.py:822`, `:874`);
- labeled model screenshots through `look_at_model`;
- geometric nearest-face/edge lookup followed by view-independent `IEntity::Select4` (`solidworks/selection_helpers.py:24`, `:49`, `:69`, `:90`);
- direct feature-owned edge selection for fillet/chamfer; and
- axis-based, direct-object direction selection for linear patterns.

That is a substantial improvement over coordinate-only `SelectByID2`, but it is not yet durable end to end. Queries return transient indices and points rather than reusable topology references. Most consumers therefore resolve the point again, a hard-coded 1 mm tolerance can be ambiguous, and several part and assembly paths still fall back to camera-dependent selection.

**Recommendation:** retain coordinates as a compatibility fallback, but make a versioned, document-bound persistent entity reference the primary contract between query tools and mutating tools. Resolve it with `IModelDocExtension::GetObjectByPersistReference3`, validate the active document and expected entity type, and select the returned COM object with `IEntity::Select4`.

The clarified query requirement—live feature and geometry data, feature/sketch consumption scopes, typed location/geometry filters, and exact selection of individual results—is specified in [ADR-001: Scoped model query and deterministic selection API](model-query-selection-api.md). It defines `query_features`, `query_geometry`, and `select_entities`, plus the shared reference contract used directly by mutating tools.

For Tuesday, prioritize selection correctness, rebuild/postcondition verification, and an exact demo soak. Do not try to finish every long-term reliability item before the meeting.

## Evidence-backed findings

### 1. Semantic queries exist, but their outputs are not stable selectors

`find_face` returns an array index, geometry, owning feature, and a sample point. The index is an enumeration position and can change after a rebuild. The point is handed to another tool, which traverses every body and picks the nearest entity within 1 mm (`selection_helpers.py:24`). It then uses `Select4`, which is good, but the identity selected is not guaranteed to be the exact face the query returned when geometry is small, close, coincident, or multi-body.

The same issue affects `get_faces`, `get_edges`, and `get_face_edges`. There is no general semantic `find_edge` or `find_vertex`, no body identity in entity results, and no way to pass a query result directly to a mutator without converting it back to coordinates.

The sample-point helper evaluates the midpoint of the face's UV bounds (`geometry_query.py:516-524`). On a trimmed or multiply-looped face, the midpoint of the parameter bounds is not itself an identity guarantee. A returned selection point should be checked with `IFace2::GetClosestPointOn`, and the actual entity identity should still travel separately.

### 2. The direct-selection implementation is useful but inconsistent

Part faces and edges first use geometry traversal plus `Select4`, then fall back to `SelectByID2` (`selection_helpers.py:69-106`). Feature, sketch, axis, and vertex helpers still use `SelectByID2` (`selection_helpers.py:226-314`). The fallback may be necessary for compatibility, but a camera pick must never silently substitute a different entity.

Assembly mate faces, edges, and vertices are selected directly with coordinate-based `SelectByID2` (`assembly.py:297-328`). This recreates the original visibility/occlusion problem in the most complex demo path. SOLIDWORKS supports `Select4` for entities traversed from component bodies in the active assembly; entities from the underlying part document must not be used directly.

### 3. Some failed selections are ignored

`reference_geometry.py` calls selection helpers without checking their Boolean results in several load-bearing paths:

- angled-plane axis edge (`:300`);
- through-point vertex (`:339`);
- both vertices, cylindrical face, or edge for a reference axis (`:386-399`);
- arc-center/on-edge/face-center reference points (`:464`, `:489`, `:514`); and
- coordinate-system origin and axis edges (`:549-557`).

The downstream COM call can therefore run against an incomplete or stale selection set. This is a small fix with high value and should be the first code change.

### 4. A non-null feature is often treated as complete success

`verify_rebuild` already checks `ForceRebuild3` plus feature-tree error markers (`com_utils.py:11-48`), but it is used mainly for parameter/configuration edits, mate edits, and C# execution. Most boss/cut/applied/pattern/hole feature creators register and report success when the creation call returns an object, without invoking the shared rebuild verifier. Many raw `ForceRebuild3(True)` return values are ignored.

This can yield a green response for a feature that exists in the tree but is in error, applied to fewer entities than requested, or did not produce the advertised topology. Applied features have a good all-or-nothing selection helper; that pattern should become shared infrastructure.

### 5. Hole Wizard is not demo-safe as advertised

The schema advertises `fastenerType` and multiple `positions` (`hole_features.py:51`, `:79`), but `hole_wizard` never consumes either. `FastenerTypeIndex` is hard-coded to zero and no positioning sketch is populated. The module also documents a blocking-dialog risk (`:36`, `:210`). Until this is implemented and soaked, use sketch circles plus through-all cut-extrude in the demonstration.

### 6. Query and newer feature coverage is thin

The live COM suite exercises `get_faces`, `get_edges`, `get_face_edges`, and a coordinate-guided fillet (`test.py:1608-1779`). It has no test for `find_face`, `look_at_model`, camera independence, persistent identity through rebuild, multi-body ambiguity, or assembly direct-object selection.

The server registers 97 tools, while the live inventory currently lists 43 scenarios. Several newer/high-risk operations appear only in agent capability prose rather than automated live regression tests. Unit coverage is concentrated on C# execution/history and registration.

### 7. Meaningful feature naming exists and is already encouraged, but it is optional and untested end to end

`solidworks_rename_feature` already gives features, sketches, and reference geometry purpose-oriented names, verifies that SOLIDWORKS accepted the rename, and re-keys the tracker (`feature_tree.py:104-128`, `:480-536`). Its tool description explicitly says to rename immediately after creation and uses examples such as `Mounting Boss`. `get_feature_tree` also detects auto-generated names and returns a naming hint (`feature_tree.py:458-477`). The README presents a readable design tree as a first-class capability.

The remaining gap is workflow friction: naming requires a separate tool call, so the agent can omit it, and there is no live semantic-tool test proving create -> rename -> use returned ID -> save/reopen. Most creation schemas also have no optional `name` field. For the demo, call `rename_feature` immediately after each meaningful creation. After the demo, add an optional `name` to creators and perform the rename before returning success, while keeping `rename_feature` for existing models.

Meaningful names should describe design intent rather than operation type: `Base Plate`, `Mounting Boss`, `M6 Clearance Holes`, `Bolt Pattern`, `Cable Relief`, `Outer Edge Fillets`. Stock planes/origin and disposable construction features need not be renamed.

### 8. Transport and recovery can misreport or hang

All normal COM calls execute synchronously inside the MCP call handler. A modal dialog or a COM call that never returns blocks the server; an asyncio timeout around the same in-process COM call would not safely cancel it. Recovery needs a process boundary or an explicit external watchdog, plus conservative retries only for known transient COM HRESULTs.

`solidworks_batch` is not transactional. It stops after a raised exception, but earlier mutations remain. Its per-call `ok` flag means "did not raise," not "rebuild and postconditions verified." This is acceptable if documented, but should not be presented as atomic.

The logging `FileHandler` is created at module import in the repository directory (`server.py:37-47`). The first sandboxed unit attempt failed at import when that location was read-only. Production logging should prefer a user-writable data directory and fall back to stderr instead of preventing server startup.

### 9. State is useful but not authoritative enough for topology

Document-scoped state and live C# reconciliation are strong foundations. However, scopes are keyed primarily from normalized document titles, and geometry query results are not registered. Open documents with colliding titles/Save As transitions/configuration changes can make topology references ambiguous unless selector tokens include document identity and configuration and are validated at resolution time.

## Prioritized backlog

Priority score follows `(Impact + Risk) x (6 - Effort)`, each input scored 1-5. The delivery tier also accounts for deadline and catastrophic failure modes; a high-effort hang-prevention item can remain important even with a lower arithmetic score.

| Tier | Work item | Impact | Risk | Effort | Score | Why now |
|---|---|---:|---:|---:|---:|---|
| P0 | Fail immediately on every missed/partial selection | 5 | 5 | 1 | 50 | Removes stale-selection corruption with a small patch. |
| P0 | Add live `find_face` and camera-independence regressions | 5 | 5 | 2 | 40 | The claimed query path currently has no direct test. |
| P0 | Make persistent entity references the query/mutator contract | 5 | 5 | 3 | 30 | Eliminates point/index handoff ambiguity. |
| P0 | Apply rebuild and operation-specific postcondition verification to demo mutators | 5 | 5 | 3 | 30 | Prevents false-positive success responses. |
| P0 | Define and soak a Tuesday-safe demo allowlist; exclude Hole Wizard | 5 | 5 | 1 | 50 | Directly protects the meeting. |
| P0 | Rename every meaningful demo feature immediately after creation | 4 | 3 | 1 | 35 | Capability already exists; makes the tree legible and later references safer. |
| P0/P1 | Replace assembly coordinate mate picks with active-assembly entity resolution | 5 | 4 | 3 | 27 | Required only if complex assembly mating is in the demo. |
| P1 | Add semantic edge/vertex/body selectors and explicit ambiguity errors | 4 | 4 | 3 | 24 | Extends the durable path beyond faces. |
| P1 | Standardize structured errors and batch success semantics | 4 | 4 | 3 | 24 | Lets the agent distinguish stale refs, ambiguity, selection failure, and rebuild failure. |
| P1 | Move logging/history to user-writable paths with safe fallbacks | 4 | 4 | 2 | 32 | Prevents startup/import failure in read-only installs. |
| P1 | Add a non-destructive preflight/health tool | 4 | 4 | 2 | 32 | Checks connection, version, templates, active doc, rebuild status, and modal state before a demo. |
| P1 | Add optional atomic `name` fields to feature/sketch/reference creators | 4 | 3 | 2 | 28 | Removes the extra call and makes semantic naming the normal path. |
| P1 | Introduce a separate watchdog/recovery boundary for blocking COM calls | 5 | 5 | 5 | 10 | Low score reflects cost, not low severity; do after the demo unless hangs reproduce. |
| P2 | Harden document identity and tracker reconciliation | 3 | 4 | 4 | 14 | Important for long sessions and external edits, less urgent for a scripted demo. |
| P2 | Add optional undo/rollback boundaries for batch workflows | 4 | 4 | 5 | 8 | Valuable, but risky to rush before Tuesday. |

## ADR: durable geometry selection

**Status:** Proposed  
**Decision owner:** project maintainer  
**Decision deadline:** before implementing the next selection-facing feature

### Context

The server needs an LLM-friendly way to discover a face/edge/vertex, inspect it, and then use that exact entity in a later feature call. Coordinates are understandable and useful for fallback diagnostics, but are not identity. Raw COM objects cannot cross MCP requests, and keeping them in a Python registry leaves stale pointers after rebuilds.

SOLIDWORKS provides persistent reference IDs through `GetPersistReference3` and resolves them through `GetObjectByPersistReference3`. Its API documentation states that these IDs are model-related and are intended to locate selectable objects across rebuilds, releases, and sessions. Direct `IEntity::Select4` then selects and marks the resolved entity without relying on the camera.

References:

- <https://help.solidworks.com/2025/English/api/sldworksapiprogguide/overview/Persistent_Reference_IDs.htm>
- <https://help.solidworks.com/2025/english/api/sldworksapi/SOLIDWORKS.Interop.sldworks~SOLIDWORKS.Interop.sldworks.IModelDocExtension~GetPersistReference3.html>
- <https://help.solidworks.com/2025/english/api/sldworksapi/SolidWorks.Interop.sldworks~SolidWorks.Interop.sldworks.IEntity~Select4.html>

### Options considered

| Option | Complexity | Durability | Assessment |
|---|---|---|---|
| Keep coordinates and improve tolerance/ranking | Low | Low | Retains ambiguity and camera fallback; compatibility only. |
| Store live COM objects behind session IDs | Medium | Low | Fast, but pointers can become invalid after rebuild and cannot survive restart. |
| Return persistent-reference tokens and resolve on use | Medium | High | **Recommended.** Matches the platform's identity mechanism and keeps MCP payloads serializable. |
| Embed a semantic query independently in every mutator | Medium | Medium | Useful convenience, but duplicates query logic and can re-resolve differently; build it on the shared selector service. |

### Decision

Add a shared `EntityReferenceService` and a versioned `EntitySelector` input.

Example query output:

```json
{
  "entityRef": {
    "version": 1,
    "kind": "face",
    "documentId": "path-or-session-document-identity",
    "configuration": "Default",
    "persistRef": "base64url-encoded-byte-array"
  },
  "point": {"x": 25.0, "y": 10.0, "z": 50.0},
  "feature": "Mounting Boss",
  "surfaceType": "Planar",
  "area": 500.0
}
```

Every selection-facing input should accept exactly one of:

```json
{"ref": {"version": 1, "kind": "face", "documentId": "...", "persistRef": "..."}}
```

```json
{"query": {"kind": "face", "orientation": "+Z", "feature": "feat:Top Pad", "areaRank": "LARGEST"}}
```

```json
{"point": {"x": 25.0, "y": 10.0, "z": 50.0}}
```

Resolution rules:

1. Validate active document identity, configuration, token version, and expected kind.
2. Resolve the persistent reference and inspect the returned state/error code.
3. Select the resolved object with `Select4` and the required mark.
4. Verify selection count, mark, and object type through `ISelectionMgr`.
5. If a query is provided, require exactly one result unless the caller explicitly opts into a ranked choice.
6. If a reference is stale, return `ENTITY_REF_STALE`; do not silently switch to the fallback point.
7. Keep the point and geometric fingerprint only for human-readable diagnostics and explicit compatibility fallback.

### Consequences

- Query-to-action workflows become deterministic and view-independent.
- Tokens are longer, document-bound, and require careful byte-array marshaling in late-bound pywin32.
- Topology that is genuinely deleted or split still invalidates a token; callers get a specific stale-reference error and can re-query.
- Assembly entities require traversal in the active assembly context, not reuse of part-document entity objects.

## Implementation sequence

### Phase 0 — immediately: protect Tuesday's demo (0.5-1 day)

1. Check every load-bearing selection result, starting with `reference_geometry.py`; clear the selection set and return a structured failure before any feature call.
2. Add a central `assert_selection(doc, expected_count, expected_types, marks)` diagnostic helper.
3. Apply `verify_rebuild` and a minimal postcondition check to the exact mutators used by the demo.
4. Add a non-destructive `solidworks_preflight` tool: COM alive/version, expected templates, active document and type, no active sketch unless expected, rebuild status, selection count, and writable workspace/log destinations.
5. Mark Hole Wizard unavailable in the demo profile; remove or explicitly reject its currently ignored `positions` and `fastenerType` inputs until implemented.
6. Rename each meaningful feature immediately after creation and use the returned renamed ID for every downstream call. End with `get_feature_tree` and require no auto-generated names among intentional design features.
7. Script one exact demo workflow, save checkpoints after major stages, and run it ten times from a known starting state.

### Phase 1 — durable selection core (1-1.5 days)

1. Add `solidworks/entity_references.py` with encode, decode, document binding, COM resolution, kind validation, and diagnostics.
2. Add a shared JSON schema builder for `EntitySelector` so face/edge/vertex contracts do not drift between modules.
3. Return `entityRef` from `get_faces`, `find_face`, `get_edges`, `get_face_edges`, and `get_vertices`; add body identity and configuration.
4. Add semantic `find_edge` and `find_vertex` predicates: owning/adjacent feature, curve type, length/radius, direction, smoothness, extremum/nearest point, and rank.
5. Teach the Tuesday mutators to accept refs first: `create_sketch`, `shell`, `draft`, `mirror`, `fillet`, `chamfer`, `ref_plane`, `ref_axis`, `ref_point`, `coordinate_system`, `thread`, and pattern directions.
6. Preserve legacy `{x,y,z}` inputs, but return an explicit diagnostic whenever point fallback was used.

### Phase 2 — verified mutation contract (1 day)

1. Create a shared mutation wrapper that captures document identity, feature/topology counts, selection diagnostics, and rebuild state before/after the COM call.
2. For each operation, define an expected postcondition: new feature type/name, body-count delta, removed/added volume where meaningful, expected selected-entity count, or component/mate count.
3. Standardize results as `ok`, `result`, `id`, `warnings`, `verification`, and `error {code, message, recoverable, context}`. Preserve the readable `result` string for compatibility.
4. Make batch `ok` depend on the structured result as well as exceptions. Continue to document that batches are non-atomic until an Undo design is proven live.
5. Add correlation IDs and elapsed time to every tool call without logging raw C# snippets or secrets.
6. Add an optional `name` to feature/sketch/reference creation schemas. Apply it through the shared rename implementation before the success response, and return the renamed tracked ID. A name conflict or rejected character must fail loudly without claiming the requested name was applied.

### Phase 3 — assembly and hang recovery (after the core path, unless needed Tuesday)

1. Add assembly geometry queries scoped by component ID and return assembly-context entity refs.
2. Replace coordinate mate selection with component-body traversal plus `Select4`, verifying both selected objects and marks before `AddMate5`.
3. Classify COM failures by HRESULT. Retry only known transient busy/rejected-call failures and only when it is safe to prove the mutation did not begin.
4. Put general COM execution behind a supervised process boundary if blocking dialogs remain reproducible. A timeout should report model state as unknown and force reconciliation before the next mutation.
5. Evaluate SOLIDWORKS UndoObject/undo-group behavior live before offering transactional batches.

### Phase 4 — broader durability (post-demo)

1. Key document scopes by a stronger identity than normalized title: saved path when available plus session identity and active configuration.
2. Reconcile tracker state from live feature/configuration/component data after external changes and reconnects.
3. Finish Hole Wizard positioning/fastener mapping or narrow the public schema to what is proven.
4. Move logs and databases to a configurable user-writable data directory; make file logging best-effort.
5. Split tests into pure unit, offline contract, live non-destructive, live document-mutating, and release soak tiers.

## Acceptance tests

### Selection contract

- Query a cube from all standard camera views; the same faces resolve to the same persistent IDs and direct selection succeeds while faces are occluded or edge-on.
- Resolve a face reference after `ForceRebuild3`, parameter changes, Save As, close/reopen, and server reconnect.
- Delete or split the referenced topology; resolution returns `ENTITY_REF_STALE` and does not fall back silently.
- Create sub-millimeter adjacent edges/faces and a multi-body part with overlapping projections; ambiguous point/query selectors fail with candidates instead of choosing the first traversal result.
- Query trimmed faces with holes/loops and verify every reported sample point lies on the returned face.
- Verify selection count, object type, and mark for every consumer.

### Mutation verification

- Deliberately request an impossible fillet, shell, draft, loft, and mate; none may return `ok=true` when the feature tree or rebuild contains an error.
- Force one item in a multi-selection to fail; no partial feature may be created.
- Verify advertised topology/mass deltas for the exact demo operations.
- Verify all AddToDB/DisplayWhenAdded and selection state is restored in `finally` blocks after success and failure.

### Assembly

- Mate occluded faces from two components without changing the camera.
- Verify the selected entities come from the active assembly context and belong to the requested component IDs.
- Rebuild, suppress/unsuppress, and reopen; references either resolve correctly or fail explicitly as stale.

### Feature naming

- Create an extrusion with `name="Mounting Boss"`; the creation response returns `feat:Mounting Boss`, the tree contains that exact name, and downstream pattern/fillet calls accept it.
- Exercise the existing create -> `rename_feature` -> downstream use path until atomic creator naming is available.
- Verify sketch entity IDs and consuming-feature metadata rebase when a sketch is renamed.
- Reject duplicate, empty, and `@`-containing names without corrupting tracker state.
- Save, close, and reopen the document; meaningful names remain visible and `get_feature_tree` does not flag them as auto-generated.

### Test gates

- Offline/unit: all 35 current tests pass; five published-worker tests may skip when the worker is not built.
- Live smoke: all selector/mutator tests used in the demo pass from a clean session.
- Soak: exact demonstration passes 10/10 times, including cold server start at least three times.
- Failure injection: stale reference, partial selection, rebuild error, read-only log directory, connection loss, and modal-dialog detection all return bounded, actionable failures.

## Tuesday-safe demonstration profile

Use the operations that already have the strongest paths:

- meaningful names immediately after creation (`Base Plate`, `Mounting Boss`, `M6 Clearance Holes`, `Bolt Pattern`) and `get_feature_tree` after each major stage;
- `find_face`/`get_faces` plus direct part-face resolution;
- fillet/chamfer by owning feature rather than a list of coordinates;
- linear pattern by model axis;
- mirror by a named plane;
- sweep/loft with tracked sketch IDs;
- through-all cut-extrude instead of Hole Wizard;
- `look_at_model`, `get_body_info`, mass properties, and saved checkpoints for visible verification;
- component-plane mates rather than assembly-space coordinate mates unless the direct assembly selector is completed and soaked.

Avoid in the live path until hardened:

- Hole Wizard;
- coordinate-picked assembly face/edge mates;
- batch calls whose later operations depend on earlier results;
- long C# snippets or any unsoaked COM signature;
- silent reliance on a 1 mm point tolerance for small or crowded geometry.

Suggested preflight on Monday and immediately before the meeting:

1. Start SOLIDWORKS and the MCP server fresh; verify version/templates and writable workspace/log paths.
2. Close unrelated PropertyManagers/dialogs, clear selection, and activate a known empty document state.
3. Run the exact scripted build once, inspect after each stage, and save checkpoints.
4. Confirm `get_feature_tree` reads like a short design narrative and reports no unintended auto-generated names.
5. Restart the server and run it again without changing camera views manually.
6. Keep a prebuilt final model and the last good checkpoint available as recovery artifacts.

## Files expected to change

- New: `solidworks/entity_references.py`
- New: `solidworks/feature_relations.py` and `solidworks/model_query.py`
- New: `solidworks/operation_guard.py` or equivalent shared verification helper
- Core: `solidworks/selection_helpers.py`, `solidworks/geometry_query.py`, `server.py`
- Naming: `solidworks/feature_tree.py` plus creator schemas/implementations; reuse one rename helper rather than duplicating tracker updates
- Consumers: `sketching.py`, `modeling.py`, `features.py`, `cut_features.py`, `applied_features.py`, `patterns.py`, `reference_geometry.py`, `hole_features.py`, `assembly.py`
- Tests: new pure selector/contract tests plus new live tests in `test.py`
- Docs: README selection examples, demo runbook, and tool audit status

## Verification performed during this review

- `python test.py --list`: PASS; 43 live-COM scenarios discovered. This command did not connect to or mutate SOLIDWORKS.
- `python -m unittest discover -s tests -v`: PASS with normal write permissions; 35 passed, 5 skipped because the published C# worker was not present.
- Initial sandboxed unit attempt: environment-blocked by denied writes to the worktree log and temporary SQLite files. It was rerun successfully with normal permissions and is not counted as a product test failure. The import-time log-path assumption remains a worthwhile durability fix.
- Full `python test.py`: intentionally not run because its documented setup closes open SOLIDWORKS documents and could interfere with active work.

## Definition of done

The Tuesday milestone is done when the exact demo workflow passes 10/10, every load-bearing selection is checked, no used mutator can report success with a rebuild error, meaningful design features have purpose-oriented names, and the demo has a verified preflight plus saved recovery checkpoints. The broader selection milestone is done when query results carry persistent refs, all selection consumers share the same resolver, legacy points are explicit fallbacks, and stale/ambiguous topology fails loudly.

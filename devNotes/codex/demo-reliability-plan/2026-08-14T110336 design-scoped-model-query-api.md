# Design scoped model query API

Expanded the demo reliability plan with `docs/model-query-selection-api.md`, a focused ADR for querying current-model feature and geometry data, scoping by feature/sketch relationships, filtering by geometry/location, and selecting exact returned elements.

## Decision

Use a shared typed service rather than independently expanding `get_faces`, `get_edges`, and `find_face`:

- `solidworks_query_features` queries features/sketches, dependencies, consumers, parameters, and geometry summaries.
- `solidworks_query_geometry` queries bodies/faces/edges/vertices/sketch entities with feature or sketch scopes and typed filters.
- `solidworks_select_entities` explicitly selects returned persistent references for inspection/escape-hatch workflows.
- Semantic mutators accept the same `entityRef` values directly, because preselection is mutable and many tools clear the selection set.

Feature scope distinguishes output faces (`IFeature.GetFaces`), affected faces (`GetAffectedFaces`), adjacency, and per-feature-type input extraction. Sketch scope can return sketch entities or geometry from features that consume the sketch.

## Important API finding

`IFace2.GetFeature` returns only the oldest owning feature even though a face can be owned by multiple features. The existing face tag is therefore useful as `primaryOwner` but is insufficient for full feature scoping. The design builds feature-first ownership sets and compares entities by persistent reference.

## Verification

Reviewed the proposed relation semantics against the official SOLIDWORKS 2025 documentation for face ownership, feature faces/affected faces, parent/child traversal, subfeatures, and persistent references. No executable server code changed and no live COM tests were required for this documentation-only design amendment.

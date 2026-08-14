# ADR-001: Scoped model query and deterministic selection API

**Status:** Proposed  
**Date:** 2026-08-14  
**Decider:** Project maintainer  
**Target:** selection foundation after the Tuesday demo P0 fixes

## Context

The server needs one coherent way to inspect the active model, narrow results by design intent and feature/sketch relationships, filter by geometry and location, and then use the exact returned element in a later operation.

The current query tools are helpful but fragmented:

- `get_feature_tree` returns structured feature/subfeature data;
- `get_state(detail="full")` adds tracked feature parents and children;
- `get_faces` and `get_edges` return geometry plus limited feature attribution;
- `find_face` filters faces by orientation, surface type, owning feature, and area rank; and
- selection helpers turn returned points back into COM objects using nearest-geometry lookup.

The missing abstraction is a query result that is also a durable selector. Array indices and sample points are display/diagnostic data, not identity.

There is also a relationship subtlety in the SOLIDWORKS API. `IFace2::GetFeature` returns the oldest owning feature even though a face can be owned by several features. Feature-scoped queries must therefore start from the feature (`IFeature::GetFaces`) rather than relying only on the current face tag. Modifier features that affect existing faces, such as draft, may return no created faces and require `IFeature::GetAffectedFaces` or feature-specific definition data.

Primary API references:

- <https://help.solidworks.com/2025/english/api/sldworksapi/solidworks.interop.sldworks~solidworks.interop.sldworks.iface2~getfeature.html>
- <https://help.solidworks.com/2025/english/api/sldworksapi/solidworks.interop.sldworks~solidworks.interop.sldworks.ifeature_methods.html>
- <https://help.solidworks.com/2025/English/api/sldworksapi/Get_Faces_Affected_by_Draft_Feature_Example_VB.htm>
- <https://help.solidworks.com/2025/English/api/sldworksapi/SOLIDWORKS.Interop.sldworks~SOLIDWORKS.Interop.sldworks.IFeature~GetChildren.html>
- <https://help.solidworks.com/2025/English/api/sldworksapi/SolidWorks.Interop.sldworks~SolidWorks.Interop.sldworks.IFeature~GetNextSubFeature.html>
- <https://help.solidworks.com/2025/English/api/sldworksapiprogguide/overview/Persistent_Reference_IDs.htm>

## Decision

Implement a shared live-model query service and expose three MCP tools:

1. `solidworks_query_features` — query features/sketches and their dependency, consumption, parameter, and topology summaries.
2. `solidworks_query_geometry` — query bodies, faces, edges, vertices, and sketch entities, optionally scoped through features or sketches.
3. `solidworks_select_entities` — resolve and visibly select exact query results for inspection or low-level workflows.

Every returned feature and selectable geometry item carries a document-bound persistent reference. Every selection-based mutator also accepts that reference directly. `select_entities` is useful for inspection and debugging, but preselecting in one MCP call must not be the normal prerequisite for a later semantic tool: many feature tools intentionally clear the SOLIDWORKS selection set. The durable contract is passing `entityRef` directly to the consuming tool.

Existing `get_faces`, `get_edges`, `get_vertices`, `get_face_edges`, and `find_face` remain compatibility wrappers over the new service.

## Architecture

```text
active SOLIDWORKS document
        |
        +-- FeatureRelationIndex
        |     feature tree / parents / children / absorbed sketches
        |     feature output faces / affected faces / parameters
        |
        +-- ModelQueryService
        |     resolve scope -> enumerate candidates -> filter -> sort/rank
        |
        +-- EntityReferenceService
              persistent ID encode/decode
              document/configuration binding
              type validation -> IEntity::Select4
```

### `FeatureRelationIndex`

Build a current-configuration relation index from live COM data:

- feature identity, name, type, build order, suppression, folder, and update stamp;
- direct and recursive `parents` / `children`;
- absorbed subfeatures, especially sketches, using `GetFirstSubFeature` / `GetNextSubFeature`;
- tracker `sourceSketch` as supplemental session metadata, never as the only authority;
- output faces from `IFeature::GetFaces`;
- affected faces from `IFeature::GetAffectedFaces` when supported;
- face/edge/vertex membership by persistent reference; and
- feature parameters/dimensions using the existing parameter traversal.

Do not infer complete ownership from `IFace2::GetFeature`: it reports only the oldest owning feature. For a global compact query it may be returned as `primaryOwner`; full ownership is computed by the feature-first index only when requested or required by scope.

### `EntityReferenceService`

Return an opaque, versioned string rather than an array index:

```json
{
  "entityRef": "swgeom:v1:face:<encoded-document-and-persistent-id>",
  "kind": "FACE"
}
```

The encoded envelope contains:

- reference version;
- entity kind;
- saved-document identity or an untitled-document session identity;
- active configuration;
- persistent-reference byte array; and
- a checksum to reject damaged tokens.

Resolution must:

1. validate token version, active document, and configuration;
2. call `GetObjectByPersistReference3` and inspect its state/error result;
3. validate the returned COM type against the declared/expected kind;
4. select with `IEntity::Select4` and the requested mark;
5. verify selection count, type, and mark through `ISelectionMgr`; and
6. fail with a specific stale/wrong-document/wrong-type error rather than falling back silently to coordinates.

Points, bounding boxes, names, and indices remain in query output for explanation and diagnostics. They are never the primary identity.

## Tool contracts

### 1. `solidworks_query_features`

Example request:

```json
{
  "scope": {
    "features": ["feat:Mounting Boss"],
    "relationship": "DESCENDANTS"
  },
  "filter": {
    "types": ["Extrusion", "Cut", "Fillet"],
    "suppressed": false,
    "nameContains": "mount"
  },
  "include": [
    "PARAMETERS",
    "DEPENDENCIES",
    "ABSORBED_SKETCHES",
    "GEOMETRY_SUMMARY"
  ],
  "limit": 50
}
```

Sketch-to-consuming-feature query:

```json
{
  "scope": {
    "sketches": ["sketch:Mounting Boss Profile"],
    "relationship": "CONSUMERS"
  },
  "include": ["PARAMETERS", "DEPENDENCIES", "GEOMETRY_SUMMARY"]
}
```

Supported feature relationships:

| Relationship | Meaning |
|---|---|
| `SELF` | Only the named feature(s). |
| `PARENTS` / `CHILDREN` | Direct dependency relation from `GetParents` / `GetChildren`. |
| `ANCESTORS` / `DESCENDANTS` | Bounded transitive dependency traversal with cycle detection. |
| `CONSUMERS` | Features that absorb or depend directly on the scoped sketch/feature. |
| `SOURCE_SKETCHES` | Sketch subfeatures absorbed/consumed by the scoped feature. |

Each result contains at least:

```json
{
  "kind": "FEATURE",
  "id": "feat:Mounting Boss",
  "featureRef": "swgeom:v1:feature:...",
  "name": "Mounting Boss",
  "featureType": "Extrusion",
  "order": 7,
  "suppressed": false,
  "parents": [],
  "children": ["feat:M6 Clearance Holes"],
  "sourceSketches": ["sketch:Mounting Boss Profile"],
  "parameters": [
    {"name": "D1@Mounting Boss", "value": 18.0, "unit": "mm"}
  ],
  "geometrySummary": {
    "outputFaces": 5,
    "affectedFaces": 0
  }
}
```

### 2. `solidworks_query_geometry`

Example: find the unique top planar face produced by the mounting boss.

```json
{
  "kinds": ["FACE"],
  "scope": {
    "features": ["feat:Mounting Boss"],
    "featureGeometry": "OUTPUT"
  },
  "filter": {
    "surfaceTypes": ["PLANE"],
    "normal": {
      "direction": "+Z",
      "angleToleranceDeg": 1.0
    }
  },
  "sort": [
    {"field": "areaMm2", "direction": "DESC"}
  ],
  "requireUnique": true,
  "include": ["RELATIONS", "MEASUREMENTS", "BOUNDS"]
}
```

Example: start at a sketch, find the features that consume it, and return their cylindrical output faces.

```json
{
  "kinds": ["FACE"],
  "scope": {
    "sketches": ["sketch:Bore Profile"],
    "sketchGeometry": "CONSUMER_OUTPUT"
  },
  "filter": {
    "surfaceTypes": ["CYLINDER"],
    "radiusMm": {"min": 4.99, "max": 5.01}
  },
  "include": ["RELATIONS", "MEASUREMENTS"]
}
```

Example: query sketch entities themselves.

```json
{
  "kinds": ["SKETCH_SEGMENT"],
  "scope": {
    "sketches": ["sketch:Mounting Boss Profile"],
    "sketchGeometry": "ENTITIES"
  },
  "filter": {
    "curveTypes": ["LINE", "ARC"],
    "construction": false
  }
}
```

Supported geometry kinds:

- `BODY`
- `FACE`
- `EDGE`
- `VERTEX`
- `SKETCH_SEGMENT`
- `SKETCH_POINT`
- later: `PLANE`, `AXIS`, and assembly-context component geometry

Feature scope modes:

| Mode | Candidate geometry |
|---|---|
| `OUTPUT` | Faces returned by `IFeature::GetFaces`; requested edges/vertices derive from those faces. |
| `AFFECTED` | Faces returned by `IFeature::GetAffectedFaces`; edges/vertices derive from those faces. |
| `OUTPUT_OR_AFFECTED` | Union of both, de-duplicated by persistent reference. Recommended default. |
| `ADJACENT` | Geometry topologically adjacent to output/affected geometry. |
| `INPUT` | Geometry consumed as feature-definition input, only for feature types with a proven extractor. Unsupported types return `RELATION_UNSUPPORTED`, not a guessed set. |

Sketch scope modes:

| Mode | Candidate geometry |
|---|---|
| `ENTITIES` | Segments and points owned by the sketch. |
| `CONSUMER_OUTPUT` | Find features that consume/absorb the sketch, then query their output faces. |
| `CONSUMER_AFFECTED` | Find consumers, then query affected faces. |
| `CONSUMER_OUTPUT_OR_AFFECTED` | Union of consumer output and affected geometry. |

### Filter model

Use a typed JSON filter rather than an arbitrary expression language. Different filter categories combine with `AND`; multiple values inside one category combine with `OR` unless the field explicitly states otherwise.

Common filters:

- entity/body/feature refs;
- feature name, tracked ID, type, folder, suppression, or dependency relation;
- body name/type/visibility;
- source or consuming sketch; and
- current configuration.

Face filters:

- surface type (`PLANE`, `CYLINDER`, `CONE`, `SPHERE`, `TORUS`, `BSPLINE`);
- outward normal direction/vector with angular tolerance;
- area range;
- cylinder/sphere radius range;
- edge/loop count;
- output/affected/primary owning feature; and
- planar/non-planar.

Edge filters:

- curve type (`LINE`, `CIRCLE`, `ARC`, `ELLIPSE`, `SPLINE`, `OTHER`);
- length/radius range;
- model-axis/vector direction with angular tolerance;
- closed/open;
- sharp/smooth;
- adjacent face or feature refs; and
- convex/concave when it can be determined reliably.

Vertex filters:

- near point with an explicit tolerance;
- within/intersecting bounding box;
- adjacent face/edge refs; and
- global/scoped extrema.

Location filters:

```json
{
  "location": {
    "near": {
      "pointMm": {"x": 20, "y": 0, "z": 30},
      "maxDistanceMm": 0.25
    },
    "withinBoxMm": {
      "min": {"x": 10, "y": -5, "z": 25},
      "max": {"x": 30, "y": 5, "z": 35}
    },
    "extreme": {
      "axis": "Z",
      "side": "MAX",
      "toleranceMm": 0.01
    }
  }
}
```

`near` uses each entity's actual closest-point API, not centroid distance. `extreme` is evaluated over the already-scoped candidate set, so “top face of Mounting Boss” does not accidentally mean the top face of the entire part.

### Result envelope

```json
{
  "ok": true,
  "queryId": "query:7e3187b4",
  "snapshot": {
    "documentId": "doc:...",
    "configuration": "Default",
    "revision": "..."
  },
  "matched": 1,
  "returned": 1,
  "items": [
    {
      "kind": "FACE",
      "entityRef": "swgeom:v1:face:...",
      "displayIndex": 12,
      "surfaceType": "Planar",
      "areaMm2": 900.0,
      "samplePointMm": {"x": 0.0, "y": 0.0, "z": 48.0},
      "normal": {"x": 0.0, "y": 0.0, "z": 1.0},
      "boundsMm": {
        "min": {"x": -15.0, "y": -15.0, "z": 48.0},
        "max": {"x": 15.0, "y": 15.0, "z": 48.0}
      },
      "relations": {
        "outputOf": ["feat:Mounting Boss"],
        "affectedBy": [],
        "sourceSketches": ["sketch:Mounting Boss Profile"],
        "bodyRef": "swgeom:v1:body:..."
      }
    }
  ]
}
```

`displayIndex` may help line up with screenshots, but it is explicitly unstable and must not be accepted as a selector.

Default `limit` is 50 and maximum is 500. A cursor is bound to the document snapshot. If the model changes between pages, return `QUERY_SNAPSHOT_STALE` and require a fresh query rather than mixing topology revisions.

`requireUnique: true` has three outcomes:

- one result: success;
- zero results: `NO_MATCH`; and
- more than one result: `AMBIGUOUS_QUERY`, with compact candidate summaries but no automatic selection.

### 3. `solidworks_select_entities`

```json
{
  "mode": "REPLACE",
  "items": [
    {"entityRef": "swgeom:v1:face:...", "mark": 0},
    {"entityRef": "swgeom:v1:edge:...", "mark": 1}
  ],
  "verify": true,
  "zoomToSelection": false
}
```

Behavior:

- resolve all references before changing selection;
- on any resolution failure, select nothing;
- apply in deterministic request order;
- verify selected count, COM type, component ownership, and mark;
- return the exact selected refs plus verification diagnostics; and
- optionally highlight/zoom for a human, but never change the camera by default.

This tool is for explicit selection, inspection, and escape-hatch workflows. Semantic mutators should accept the same refs directly:

```json
{
  "radius": 3,
  "edges": [
    {"entityRef": "swgeom:v1:edge:..."},
    {"entityRef": "swgeom:v1:edge:..."}
  ]
}
```

```json
{
  "thickness": 2,
  "facesToRemove": [
    {"entityRef": "swgeom:v1:face:..."}
  ]
}
```

Legacy `{x,y,z}` selectors remain accepted during migration and are returned with `selectionMode: "POINT_FALLBACK"` diagnostics.

## Error contract

| Code | Meaning |
|---|---|
| `NO_ACTIVE_DOCUMENT` | No model is active. |
| `WRONG_DOCUMENT_TYPE` | Query kind/scope is invalid for the active document. |
| `UNKNOWN_FEATURE` / `UNKNOWN_SKETCH` | Scope reference cannot be resolved. |
| `RELATION_UNSUPPORTED` | Requested feature-input/affected relation has no proven extractor. |
| `NO_MATCH` | Filters produced no candidates. |
| `AMBIGUOUS_QUERY` | Uniqueness was required but multiple candidates remain. |
| `ENTITY_REF_STALE` | SOLIDWORKS cannot resolve the persistent ID in this model state. |
| `ENTITY_REF_WRONG_DOCUMENT` | Token belongs to another document/session. |
| `ENTITY_REF_WRONG_CONFIGURATION` | Token is bound to another configuration and was not explicitly re-queried. |
| `ENTITY_REF_WRONG_TYPE` | Resolved COM object does not match the declared/required kind. |
| `QUERY_SNAPSHOT_STALE` | Model changed during pagination or saved selection-set use. |
| `SELECTION_VERIFICATION_FAILED` | Count/type/mark differs from the request. |

No error should silently substitute the nearest coordinate, first enumeration result, or oldest owning feature.

## Options considered

### A. Expand each existing query independently

| Dimension | Assessment |
|---|---|
| Complexity | Low initially, high over time |
| Compatibility | High |
| Consistency | Low |

Pros: smallest short-term patch.  
Cons: filters, feature relations, pagination, and selector encoding will drift between tools.

### B. One arbitrary query language

| Dimension | Assessment |
|---|---|
| Expressiveness | High |
| Validation | Difficult |
| Agent reliability | Medium/low |

Pros: maximum flexibility.  
Cons: hard to describe in MCP schema, easy for models to produce invalid expressions, and difficult to map consistently to COM.

### C. Shared typed service with two query tools and one selector tool

| Dimension | Assessment |
|---|---|
| Complexity | Medium |
| Compatibility | High through wrappers |
| Consistency | High |
| Extensibility | High |

Pros: one relation/filter/reference implementation, strong schemas, and gradual migration.  
Cons: requires an intentional internal refactor before adding more one-off filters.

**Decision:** Option C.

## Implementation plan

1. Add `solidworks/entity_references.py` and prove byte-array round trips for face, edge, vertex, feature, sketch segment, and sketch point across rebuild and save/reopen.
2. Add `solidworks/feature_relations.py`; extract/de-duplicate the safe feature/subfeature traversal from `feature_tree.py`.
3. Add `solidworks/model_query.py` with typed candidate records, scope resolution, filter predicates, sorting, uniqueness, and bounded pagination.
4. Expose `solidworks_query_features`, `solidworks_query_geometry`, and `solidworks_select_entities` through a new module.
5. Reimplement the existing geometry query tools as compatibility projections of `ModelQueryService`.
6. Add `EntitySelector` schema helpers and migrate the highest-value consumers: `create_sketch`, fillet/chamfer, shell/draft, mirror, reference geometry, thread, patterns, and assembly mates.
7. Add feature-type-specific `INPUT` relation extractors only after each COM definition API is tested. Unsupported types must fail explicitly.
8. Add assembly-context geometry scope by component ID, ensuring selected entities are obtained from the active assembly rather than the underlying part document.

## Acceptance tests

- Query by renamed feature, raw name, tracked ID, and persistent `featureRef`; all produce the same output face reference set.
- Query a sketch's consuming extrusion/cut and then its output faces.
- Query a modifier feature where `GetFaces` is empty but `GetAffectedFaces` is populated.
- Confirm feature scoping finds all applicable ownership instead of only `IFace2.GetFeature`'s oldest owner.
- Combine feature/sketch scope with surface, radius/length/area, direction, near-point, bounding-box, and scoped-extreme filters.
- Require uniqueness and verify zero/multiple matches never select anything.
- Use returned refs directly in create-sketch, fillet, shell, mirror, reference-geometry, and mate operations.
- Rebuild, rename, change parameters, save/reopen, and reconnect; refs either resolve to the same topology or fail specifically as stale.
- Delete/split topology and verify no point fallback or first-match substitution occurs.
- Query more than the page limit, change the model, and verify the old cursor fails with `QUERY_SNAPSHOT_STALE`.
- Run the exact Tuesday demonstration ten times without depending on display index, current camera, or hand-entered topology coordinates.

## Consequences

- Agents can ask design-intent questions such as “the largest +Z face produced by Mounting Boss” or “all circular edges from features consuming Bore Profile,” then operate on the exact answers.
- Feature, sketch, topology, and selection logic share one vocabulary and error model.
- Persistent references improve durability but cannot make deleted or split topology immortal; explicit stale handling remains required.
- Full “input geometry consumed by any feature” is not generic in SOLIDWORKS. It must be implemented per feature-definition type and truthfully reported as unsupported elsewhere.
- Rich relation output can be expensive on large parts, so compact defaults, explicit `include`, bounded limits, and snapshot-aware pagination are required.

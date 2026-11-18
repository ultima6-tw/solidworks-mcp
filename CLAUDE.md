# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

SolidWorks MCP Server bridges Claude AI with SolidWorks CAD via the Model Context Protocol (MCP), enabling natural language creation and manipulation of 3D CAD models. Requires Windows + SolidWorks installed.

## Commands

```powershell
# Install dependencies
pip install -r requirements.txt

# Or install as an editable package (exposes `solidworks` and `server` for import)
pip install -e .

# Run all tests (closes open docs first, then runs all categories)
python test.py

# Interactive CLI test picker — select tests by number, range, or category
python test.py --gui

# Run a single category
python test.py --category "Sketch Tools"
python test.py --category "Feature Tools"
python test.py --category "Integration"

# Run a single test by name
python test.py --test basic_cube
python test.py --test sketch_line

# List all available tests
python test.py --list

# Run the server directly (normally launched by an MCP client such as Claude Desktop)
python server.py

# Dev server with hot reload (requires watchdog)
python dev_server.py

# Close all open SolidWorks documents (standalone utility)
python clean.py
```

## Claude Desktop Configuration

The server is registered in `%APPDATA%\Claude\claude_desktop_config.json`:
```json
{
  "mcpServers": {
    "solidworks": {
      "command": "python",
      "args": ["C:\\path\\to\\solidworks-mcp\\server.py"]
    }
  }
}
```

## Architecture

```
server.py                         # MCP server: registers tools, routes calls via dispatch map
solidworks/
  __init__.py                     # Package exports for all modules
  connection.py                   # COM connection to SolidWorks, template discovery, part creation
  state_tracker.py                # Centralized state tracker: stable IDs for features, sketches, entities
  state_query.py                  # MCP tools for querying tracked state (get_state, get_entity, get_sketch_entities)
  feature_errors.py               # Feature error/warning check run by server._route_tool after feature tools; official swFeatureError_e text
  volume_check.py                 # Volume before/after check run by server._route_tool around tools that change the solid
  update_check.py                 # Start-up check: GitHub compare <local commit>...<branch>; one-time notice on the first tool result
  sketching.py                    # 2D sketch tools, dimensioning, and constraints with spatial tracking
  modeling.py                     # Core modeling (new_part, extrusion, cut-extrusion, mass_properties, list_features)
  selection_helpers.py            # Shared selection utilities (edge, face, plane, feature, axis)
  features.py                     # Boss/Base features (revolve, sweep, loft, boundary boss)
  cut_features.py                 # Cut features (cut revolve, cut sweep, cut loft, boundary cut)
  applied_features.py             # Applied features (fillet, chamfer, shell, draft, rib, wrap, intersect)
  patterns.py                     # Patterns (linear pattern, circular pattern, mirror)
  hole_features.py                # Hole features (hole wizard, cosmetic thread)
  reference_geometry.py           # Reference geometry (ref plane, ref axis, ref point, coordinate system)
  geometry_query.py               # Geometry inspection (body info, faces, edges, face edges, vertices) with feature tagging
  document_manager.py             # Document lifecycle (save, open, activate, close, list, capture_views)
  assembly.py                     # Assembly tools (new assembly, insert component, mates, interference, assembly queries)
  configurations.py               # Configurations (variants, per-config dims) and equations
  feature_tree.py                 # Timeline as structured data, renaming, folders
  com_utils.py                    # Shared late-bound COM helpers (com_prop)
test.py                           # Unified test suite with registry, CLI selector (--gui), category/test filters
clean.py                          # Close all open SolidWorks documents (standalone utility)
INSTALL.md / INSTALL.zh-TW.md     # Setup guide addressed to the AI doing the install (there is no installer script)
```

**Test suite (`test.py`):** Uses a decorator-based test registry with 6 categories (Basic, Sketch Tools, Feature Tools, MCP Tools, Assembly, Integration). Each test is `def test_xxx(sw, template) -> bool`. The runner closes all open docs before starting, and between each test. The Integration category wraps the sequential cut-extrude reliability sub-tests as a single meta-test.

**Data flow:** Claude → MCP tool call → `server.py` → `_route_tool()` (dispatch map) → module → SolidWorks COM API

**Routing:** `server.py` builds a `{tool_name: module}` dispatch map at startup from each module's `get_tool_definitions()`. No static tool lists needed—adding a tool to a module auto-registers it. `server.py` itself defines one extra tool: `solidworks_batch` (up to 25 sequential tool calls in one MCP request; no result chaining; stops on first error unless `stopOnError=false`; cannot nest itself).

**Workflow order:** `new_part` → `create_sketch` → sketch entities → (optional: dimensions/constraints) → `exit_sketch` → feature creation (extrude, revolve, etc.)

## Key Implementation Details

**Units:** All tool inputs/outputs use millimeters. The SolidWorks COM API requires meters, so all values are divided by 1000 internally before API calls. Angles are input in degrees and converted to radians internally.

**State tracking:** A centralized `StateTracker` (`state_tracker.py`) assigns stable IDs to all created objects and maintains them across the session. State is **scoped per document**: each part/assembly gets its own `DocumentScope` (features, sketches, entities, components, mates, spatial tracking), so building multiple parts and an assembly in one session preserves each document's state. `new_part`/`new_assembly` create a fresh scope; `activate_document`/`open_document` switch scopes; `save_document` re-keys the scope to the new title. IDs follow the format:
- Features: `feat:<sw_name>` (e.g., `feat:Boss-Extrude1`)
- Sketches: `sketch:<sw_name>` (e.g., `sketch:Sketch1`)
- Sketch entities: `entity:<sketch>/<type>_<idx>` (e.g., `entity:Sketch1/rect_0`, `entity:Sketch1/line_0`)
- Reference geometry: `ref:<sw_name>` (e.g., `ref:Plane1`)
- Components: `comp:<sw_name>` (e.g., `comp:bracket-1`)
- Mates: `mate:<sw_name>` (e.g., `mate:Coincident1`)

All tools accept both tracked IDs and raw SolidWorks names. When raw names are used, a deprecation warning is logged.

**JSON returns:** All tools return JSON strings with a standard structure: `{"result": "✓ ...", "id": "feat:...", "type": "..."}`. Geometry query tools return structured data (faces/edges/vertices as arrays of objects). Error returns remain plain strings prefixed with `❌`.

**Spatial tracking:** The `StateTracker` maintains a `last_shape` dict with the center, edges, width/height/radius of the most recently drawn shape. This enables Claude to position shapes relative to previous ones without needing to track coordinates itself. Entities that update spatial tracking: rectangle, circle, line, arc, polygon, ellipse, spline, slot. Entities that do NOT update tracking: point, centerline, text.

**Positioning priority** in `sketch_rectangle`/`sketch_circle`/`sketch_polygon` (highest wins):
1. Absolute `centerX`/`centerY`
2. `spacing` from last shape edge
3. Relative `relativeX`/`relativeY` offset from last center
4. Default: origin `(0, 0)`

**Loft workflow:** Lofts require profiles on different planes. The `create_sketch` `plane` parameter accepts both standard planes ("Front", "Top", "Right") and custom reference plane names or IDs ("Plane1", "ref:Plane1"). Typical workflow:
1. `create_sketch(plane="Front")` → draw first profile → `exit_sketch` (returns `{"id": "sketch:Sketch1", ...}`)
2. `ref_plane(type="OFFSET", referencePlane="Front", offset=80)` (returns `{"id": "ref:Plane1", ...}`)
3. `create_sketch(plane="Plane1")` → draw second profile → `exit_sketch` (returns `{"id": "sketch:Sketch2", ...}`)
4. `loft(profileSketches=["sketch:Sketch1", "sketch:Sketch2"])` — accepts both IDs and raw names

**Selection helpers** (`selection_helpers.py`): All geometry selection (edges, faces, planes, features, axes, vertices) is centralized here. Functions accept coordinates in mm and convert to meters. Used by all feature modules. Key functions: `select_edge`, `select_face`, `select_plane`, `select_feature`, `select_sketch`, `select_axis`, `select_vertex`, `select_multiple_edges`, `select_multiple_faces`.

**Module pattern:** Each feature module follows the same structure: class with `__init__(self, connection, tracker=None)`, `get_tool_definitions() -> list[Tool]`, and `execute(tool_name, args) -> str`. Returns are JSON strings. Modules that accept sketch/feature name inputs (features, cut_features, patterns) have a `_resolve_name()` helper that accepts both tracked IDs and raw SolidWorks names.

**Feature ID returns:** All tools that create objects return a JSON string containing the stable ID (e.g., `{"result": "✓ Extrusion ...", "id": "feat:Boss-Extrude1", "type": "extrusion"}`). Agents should use these IDs for subsequent operations. Sketch entity tools also include the parent `sketchId`.

**Extrusion end conditions:** Both `create_extrusion` and `create_cut_extrusion` accept an optional `endCondition` parameter: `"BLIND"` (default, extrudes to specified depth) or `"THROUGH_ALL"` (extrudes through entire body). Through All is especially useful for cut-extrusions where the agent doesn't need to calculate exact depth.

### Sketch Tools

`sketch_rectangle`, `sketch_circle`, `sketch_profile` (whole chained LINE/TANGENT_ARC/ARC profile in one call, optional auto-close + corner fillets — preferred over many line/arc calls), `sketch_fillet` (round a sketch corner by vertex with an exact tangent arc), `sketch_offset` (offset existing geometry/chain by a distance; negative flips side), `sketch_line`, `sketch_centerline`, `sketch_arc` (3-point or center-point), `sketch_spline`, `sketch_ellipse`, `sketch_polygon`, `sketch_slot`, `sketch_point`, `sketch_text`, `sketch_dimension` (add smart dimension with optional driving value), `set_dimension_value` (modify existing dimension), `sketch_constraint`, `sketch_toggle_construction`, `create_sketch`, `exit_sketch`, `get_last_shape_info`.

### Modeling Tools

`new_part`, `create_extrusion` (optional `merge: false` creates a separate body for multi-body work; a feature returned in error — e.g. a THROUGH_ALL boss pointing away from every body, `swFeatureErrorExtrusionNoEndFound` (31) on SW2022 — is deleted, sketch kept, and reported as an error; it is NOT retried in the other direction, which would add material where nobody asked. With no body at all, THROUGH_ALL makes SolidWorks extrude a default 1000 mm without any error — not handled yet), `create_cut_extrusion` (cut direction auto-flips into the body when the first attempt fails — sketches on boundary planes just work; "fails" includes a feature returned in error, e.g. SW2022 returns a Through-All cut pointing away from the body as a feature with `swFeatureErrorExtrusionNoEndFound` (31) that removed nothing, so the error code is read and such a feature is deleted before the retry), `combine_bodies` (ADD/SUBTRACT/COMMON boolean of a multi-body part; swBodyOperationType_e probed live: 15901=COMMON, 15902=SUBTRACT, 15903=ADD — reverse of common docs), `set_material` (assign a SOLIDWORKS Materials entry, e.g. '6061 Alloy' — required for mass questions; density is default 1000 kg/m³ otherwise), `set_parameter` (set any dimension by name, e.g. 'D1@Boss-Extrude1' — parametric "modify the part" operations), `suppress_feature`, `delete_feature`, `get_mass_properties` (kg + grams; optional `coordinateSystem` reports COM relative to a coordinate-system feature), `list_features`, `list_parameters` (every driving dimension with its addressable name, value, unit, and owning feature — use before `set_parameter`/`set_config_parameter`/equations instead of guessing names).

**Sketch coordinate frames:** `create_sketch` returns `sketchFrame` (origin + X/Y axis directions in model space). Face sketches can have mirrored or rotated axes (back face: sketch X = model −X; top face: sketch Y = model −Z) — convert model targets with `sketch_x = xAxisModel · (target − originModel_mm)`. The sketch ORIGIN on a face is the model origin projected onto the face plane, NOT the face center — face sketches therefore also return `faceCenter` (`model_mm` + `sketch_mm`, from the face bounding box) as the layout datum.

**Update check:** `main()` starts `update_check.start()` (background thread, ≤4 s timeout, never blocks). It reads the local commit from `.git` (a directory, a `gitdir:` file such as `.git.nosync`, or packed-refs), takes `owner/repo` from the origin remote if it is on github.com (else none; `SOLIDWORKS_MCP_UPDATE_REPO` overrides), and asks `api.github.com/repos/<repo>/compare/<sha>...<branch>`; `ahead_by` > 0 → a one-time notice that `call_tool` appends as an extra text item to the first tool result. 404 (local commits not on GitHub), network errors, non-git copies and `SOLIDWORKS_MCP_UPDATE_CHECK=0` → silent. Only the stdio server checks (the bridge host does not run `main()`). Every user-facing change goes into `CHANGELOG.md` and `CHANGELOG.zh-TW.md`, marked **Update recommended** when a tool could report success without doing what was asked. Test: `update_check` (offline stand-in for GitHub).

**Feature error/warning check (every feature tool):** a SOLIDWORKS feature call can return a feature that is in an error or warning state and did nothing while the tool reports success — probed on SW2022: a Through-All cut/boss pointing away from the body comes back in error 31; a linear pattern or mirror whose copies land outside the body comes back with warning 51 and adds nothing. Whether SOLIDWORKS returns None or such a feature can change during a long session (the same away-from-body cut returned an error feature after ~11 h and None again after a restart). So `SolidWorksMCPServer._route_tool` snapshots the top-level feature names before any tool of the modeling, features, cut_features, applied_features, patterns, hole_features and reference_geometry modules runs, and after it reads `IFeature.GetErrorCode2` on every new feature (`solidworks/feature_errors.py`; the BYREF BOOL out parameter is the warning flag). Error → the feature is deleted (option 0, its sketch stays), its tracker record dropped, and the call fails with the official `swFeatureError_e` text (from swconst.chm, in `FEATURE_ERROR_TEXT`). Warning → the feature is kept, the result's ✓ becomes ⚠ with the text, and `featureWarnings` lists code/description. This also covers the SSH bridge (the host routes through `_route_tool`) and `solidworks_batch`. An error code does not catch everything: a 120 mm fillet on a 100 mm cube came back with code 0 and an odd shape. Test: `mcp_feature_error_check` (the error branch also runs on a stand-in document, since a freshly started SOLIDWORKS tends to return None instead).

**Volume check (tools that change the solid):** an error code does not catch a feature that did nothing — probed on SW2022: a boss drawn entirely inside the body returns a feature with code 0 and adds nothing (a cut that misses the body, by contrast, does come back in error 51). So for the tools in `volume_check.VOLUME_TOOLS` on a part, `_route_tool` reads the total solid volume (sum of `IBody2.GetMassProperties` over `GetBodies2(swSolidBody, False)`; equals `CreateMassProperty.Volume`) before and after, AFTER the error-code check, and adds `volumeChange` {before/after/delta mm³, expected_delta, expected_basis, check} to the result. Each tool has an expected sign (+1 adds, −1 removes, 0 either but must change, None report only — `intersect`, `combine_bodies`, `wrap` SCRIBE). No change (<1e-3 mm³) or the wrong sign → ✓ becomes ⚠. For BLIND `create_extrusion`/`create_cut_extrusion` the tool also computes the nominal change itself: the new non-sketch feature's absorbed sketch (`GetFirstSubFeature`, `ProfileFeature`) → `IModelDocExtension.GetSectionProperties2` area (must be `_FlagAsMethod`-ed; status ≠ 0 for an open/invalid profile → not computed) × depth. Within 0.1% → `matches_expected`; more → ⚠ `more_than_expected`; less → `less_than_expected` with an ℹ note only (a cut partly outside the body, a boss overlapping material are normal). THROUGH_ALL: not computed. A merged boss can legitimately give 0 when it joins two overlapping bodies (their double-counted overlap disappears), which is why "no change" is a warning, not an error. Test: `mcp_volume_check`.

**Sketch snapping (read-back):** at a zoomed-out view SolidWorks snaps a new entity onto nearby geometry and the Create* call still succeeds — probed: a radius-1.7 circle beside a 7 mm rectangle came out radius 3.5, and a rectangle in line with small circles lost half its width, the latter even with `AddToDB=True` (the API help says AddToDB avoids snapping; it did not here). `sketch_rectangle`/`sketch_circle`/`sketch_line` therefore read the drawn geometry back (`SketchingTools._draw_checked`, tolerance 1e-5 mm); on a mismatch they delete it and redraw with `swUserPreferenceToggle_e.swSketchInference` (249, "Enable snapping") switched off, then restore it, and error out if the redraw is still wrong. Snapping is not switched off up front because it is also what joins separately drawn lines that share an endpoint. The same check covers `sketch_arc` (both modes), `sketch_polygon`, `sketch_ellipse`, `sketch_slot`, `sketch_spline`, `sketch_point` and `sketch_centerline` — probed with a 7×14 rectangle next to each, all but the spline used to snap (arc endpoints onto its corners, centres onto its edge midpoints, the slot moved 2.5 mm, the centerline laid onto its edge). `CreateSketchSlot` returns an ISketchSlot, so the slot's segments are found by diffing the sketch before/after by geometry (`_new_segments`; `ISketchSegment.GetID` repeats within a sketch on SW2022 and cannot be used). `sketch_profile` is only checked, not redrawn (snapping is what joins its chained segments): a profile that came out wrong is removed and the call fails. Tests: `mcp_sketch_snapping`, `mcp_sketch_snapping_tools`.

### Configuration & Equation Tools

`add_configuration`, `switch_configuration`, `list_configurations`, `set_config_parameter` (dimension value in ONE configuration only), `add_equation` (e.g. `"D1@Boss-Extrude1" = "D1@Sketch1" / 2` — re-solves automatically when driving dims change), `list_equations`, `delete_equation`.

### Boss/Base Features

`revolve` (requires centerline in sketch), `sweep` (profile + path sketches), `loft` (2+ profile sketches), `boundary_boss` (profiles + optional guide curves).

### Cut Features

`cut_revolve`, `cut_sweep`, `cut_loft`, `boundary_cut`. Same params as boss counterparts but remove material.

### Applied Features

`fillet` (edges + radius), `chamfer` (edges + distance) — both accept a `feature` param that fillets/chamfers every edge of that feature by selecting IEdge objects DIRECTLY via `Select4` (view-independent, includes closed/circular edges; PREFERRED over coordinates), `shell` (faces to remove + thickness), `draft` (neutral plane + faces + angle), `rib` (sketch profile + thickness), `wrap` (emboss/deboss/scribe onto face), `intersect` (overlapping bodies).

**Selection pitfall:** `SelectByID2` coordinate picks resolve against the CURRENT CAMERA VIEW — geometry occluded from the camera cannot be picked even with exact on-geometry coordinates (no single view sees all 12 edges of a box), and geometry viewed edge-on can silently hijack a pick meant for its neighbor. Prefer direct object selection (`selection_helpers.select_entities_directly`, used by fillet's `feature` path) for anything enumerable via COM traversal. `create_sketch`'s face path verifies the picked face is within 1mm of the requested point and falls back to nearest-face object selection (`_face_object_at_point`) — both occlusion and edge-on mis-picks self-heal. Other coordinate-based tools warn about this in their error messages.

### Patterns

`linear_pattern` (features + direction + spacing + count, optional 2nd direction; direction accepts `{"axis": "X"|"Y"|"Z"}` — PREFERRED, picks any parallel body edge via object selection — or a coordinate point on an edge), `circular_pattern` (features + axis + count + angle), `mirror` (features + mirror plane).

### Hole Features

`hole_wizard` (HOLE / COUNTERBORE / COUNTERSINK / STRAIGHT_TAP at one or more points on a planar face; sizes exactly as in the SOLIDWORKS table, e.g. `Ø10.0` for ISO drill sizes, `M6` for screws/taps), `thread` (cosmetic thread on circular edge).

### Reference Geometry

`ref_plane` (offset/angle/through-point), `ref_axis` (two-points/cylindrical-face/edge), `ref_point` (coordinates/arc-center/face-center/on-edge), `coordinate_system` (origin + optional axis edges).

### Geometry Query Tools

`get_body_info` (bounding box, face/edge/vertex counts), `get_faces` (enumerate with type, area, normal, sample point; optional `surfaceType` filter), `get_edges` (enumerate with endpoints, midpoint, length; optional `edgeType` and `feature` filters), `get_face_edges` (edges of a specific face by coordinate), `get_vertices` (all unique vertex coordinates), `find_face` (select a face by DESCRIPTION: orientation +X/−X/.../ANGLED/ANY, creating feature, surface type, and/or area rank — returns sample points ready for `create_sketch`/selection tools; face normals are sense-corrected OUTWARD via `FaceInSurfaceSense`, same as `get_faces` details), `look_at_model` (screenshot of the model returned as image content in the MCP response, with face labels F0/F1/... matching `get_faces` indices; `view='sketch'` captures the active sketch normal-to; labels need Pillow).

**Feature tagging:** every face returned by `get_faces`/`get_face_edges` includes `"feature"` (and `"featureId"` when tracked) — the feature that created it, via `IFace2.GetFeature`. Every edge from `get_edges` includes a `"features"` list (features whose faces meet at that edge, via `GetTwoAdjacentFaces2`). This lets agents select geometry by feature ("fillet the edges of Boss-Extrude2") instead of guessing coordinates.
### Document Tools

`save_document` (true Save As; bare filenames go to `workspace/`; required before inserting a part into an assembly; a neutral extension — .STEP/.STP/.IGS/.X_T/.SAT/.STL/.3MF — **exports** instead, leaving the session document open and unchanged), `open_document` (silent OpenDoc6; neutral formats auto-route to `import_file`), `import_file` (neutral CAD import), `recognize_features` (FeatureWorks), `activate_document` (switch active doc — subsequent tools operate on it), `close_document` (discards unsaved changes), `capture_views` (export standard-view PNGs — default ISOMETRIC/FRONT/TOP — to `workspace/screenshots` or a given `outputDir`), `list_documents`.

**Neutral-format import:** `OpenDoc6` **cannot** open STEP/IGES — it fails with `swFileRequiresRepairError` (2097152), which is misleading; the file is fine. Import goes through `ISldWorks::LoadFile4(path, "r", importData, err)` with `importData` from `GetImportFileData`. `import_file` handles this, and forces 3D Interconnect (`swMultiCAD_Enable3DInterconnect` = **691**) OFF for the duration: with it ON the file lands as an associative `<file>.STEP<1>` `[MBimport]` feature that cannot be feature-recognized or parametrically edited; with it OFF you get a dumb `Imported1` `[BaseBody]` solid. Import diagnostics prefs (690, 291) are suppressed so nothing can raise a blocking dialog. All prefs are restored afterwards. Pass `linked: true` to deliberately keep the associative form.

**Feature recognition (`recognize_features`):** turns an imported dumb solid into a parametric tree. FeatureWorks ships with SOLIDWORKS at `fworks\fworks.dll` but is not loaded by default and needs Professional/Premium — `LoadAddIn(dll)` then `GetAddInObject("FeatureWorks.FeatureWorksApp")` (that exact ProgID; the obvious guesses return `None`). `IFeatureWorksApp` = `RecognizeFeatureAutomatic(mask)` → count, then `CreateFeatures(opts)` → bool. Mask bits: EXTRUDE 1, **VOLUME 2**, REVOLVE 4, HOLES 8, CHAMFER_FILLET 16, RIBS 32.

- **Keep VOLUME in the mask** — it finds the base feature. Measured: a plate with a hole and a fillet recognized 0 features without it, 3 with it.
- Recovered features are genuinely drivable: `set_parameter("D1@Fillet1", …)` rebuilds and moves the volume as expected.
- **Recognized sketches are constrained but NOT dimensioned** — there are no `D<i>@Sketch<n>` dimensions, so cut/boss *profiles* cannot be driven numerically. Change those by editing sketch geometry or by direct face editing.
- Cost: ~6s on a simple part, ~2.5 min on a 435-face one. Volume is preserved to ~0.001%.
- **First call after FeatureWorks loads returns 0** (SW2022, measured): when the add-in is loaded for the first time in a SOLIDWORKS session (`LoadAddIn` → 0), the first `RecognizeFeatureAutomatic` returns 0 and `CreateFeatures` does nothing; an immediate second call recognizes normally. `recognize_features` therefore calls it a second time when the first returns 0.

### Assembly Tools

`new_assembly`, `insert_component` (pre-opens the part file, re-activates the assembly, AddComponent5; SolidWorks centers the component's **bounding box** at the drop point — the returned `actualPosition` is the component origin; first component is auto-fixed), `add_mate` (COINCIDENT, CONCENTRIC, PERPENDICULAR, PARALLEL, TANGENT, DISTANCE, ANGLE, LOCK; entities are either component planes `{"plane": "Front", "component": "comp:x-1"}` or coordinate picks `{"entityType": "FACE", "x":…, "y":…, "z":…}` in assembly-space mm), `edit_mate` (change a DISTANCE/ANGLE mate's value; rebuilds and returns updated component positions — mate dims are regular dimensions named `D1@<MateName>`), `check_interference` (overlap volumes + components involved), `suppress_component`, `list_components`, `list_mates`, `get_assembly_mass_properties` (kg + grams; optional `coordinateSystem`). `add_mate` also supports GEAR (mateType GEAR + `gearRatioNumerator`/`gearRatioDenominator`). WIDTH mates are not yet implemented.

**Assembly workflow:** build parts → `save_document` each → `new_assembly` → `insert_component` (first is fixed) → position with mates, not coordinates (the Transform2 setter is unavailable via late-bound COM). Coordinates returned by part-level `get_faces` are in PART space — offset them by the component's position from `list_components` when coordinate-picking mate faces.

**Recommended workflow:** After creating geometry, call `get_body_info` for an overview, then `get_faces` or `get_edges` to find exact coordinates for fillet, chamfer, shell, draft, and pattern operations. The sample points and midpoints returned by these tools are guaranteed to be on/near the geometry and can be passed directly to selection-based tools.

### Feature Tree Tools

`get_feature_tree` (the design timeline as structured JSON — build order, type, tracked ID, suppression, folder nesting, and absorbed sub-features such as the sketch consumed by an extrude; prefer it over `list_features`, which returns unstructured text), `rename_feature` (`IFeature.Name` is a settable property; the tracker is re-keyed in the same call so `feat:Boss-Extrude1` becomes `feat:Base Plate` and sketch-entity IDs / consuming features follow), `create_feature_folder`, `move_to_folder` (via `ReorderFeature`, see below).

**Naming is encouraged, not just possible.** Auto-generated names (`Boss-Extrude7`, `Sketch12`) make a model unreadable to a later agent and to the human who opens it. `rename_feature`'s description tells agents to name each feature as they create it, and `get_feature_tree` returns a `hint` listing features that still carry default names.

**Tree traversal gotchas:** `IModelDoc2::FirstFeature` + `GetNextFeature` walks the timeline, but absorbed sketches and foldered features **also appear in that linear walk**, so they must be de-duplicated against what was already emitted as a sub-feature or folder child. Descend into absorbed children with `GetFirstSubFeature`/**`GetNextSubFeature`** — calling `GetNextFeature` on a sub-feature rejoins the top-level chain and the traversal never terminates (this hung a test run). Folder contents come from `IFeature::GetSpecificFeature2()` → `IFeatureFolder::GetFeatures()`.

**Folders — what works and what does not:**

- ✅ `InsertFeatureTreeFolder2(2)` = **Containing** wraps the current selection — the simplest way to group features: pass the full `features` list to `create_feature_folder`.
- ✅ `InsertFeatureTreeFolder2(1)` = **EmptyBefore** creates an empty folder, but it is inserted *above the current selection* and returns `None` if nothing is selected — the tool anchors it on the last feature automatically.
- ✅ **Moving features into an existing folder** uses `IModelDocExtension::ReorderFeature(feature, target, swMoveLocation_e)` (SW2022 API help; probed on SW2022): `swMoveToFolder` (5) with the folder name puts the item at the **top** of the folder, so a feature that depends on the folder's contents (a hole on a foldered block) is refused; `swMoveAfter` (3) after the folder's last member appends it at the **end**, keeping the build order. `move_to_folder` tries append first, then top, and verifies by reading the folder back.
- ❌ `IFeatureManager::MoveToFolder` is **not** for design-tree folders — the API help says it is specific to the Solid Bodies folder. It returned False in every case tested (SW2025 and SW2022); do not use it for feature folders.
- ⚠ Nesting: `ReorderFeature(folder, parentFolder, swMoveToFolder)` DID nest one folder in another on SW2022 (probed), but `create_feature_folder`'s `parentFolder` still uses the old `MoveToFolder` call and reports nesting as unsupported — not yet switched over. `get_feature_tree` lists a nested folder only inside its parent.

**Folder end tags:** every folder adds a closing pseudo-feature named `<Folder>___EndTag___` to the linear walk. It is not a design step — filter it out of any traversal (`get_feature_tree` does).

### State Query Tools

`get_state` (session state for the active document: tracked features/sketches/entities/refGeo — plus live enrichment: bounding box + face/edge counts, per-sketch `constrainedStatus` (FULLY_DEFINED/UNDER_DEFINED), active configuration, open documents, and components/mates for assemblies; `detail: "full"` adds feature parent/child dependencies; all COM enrichment is best-effort so `get_state` never fails), `get_entity` (detailed info about a single object by its tracked ID), `get_sketch_entities` (the tracked entities with their requested coordinates, plus `geometry`: every segment and standalone point read live from the sketch feature in mm — after dimensions/relations, including geometry not drawn by the tools, and for sketches that are not being edited; the two lists are not matched, since `ISketchSegment.GetID` repeats within a sketch. Test: `mcp_sketch_entities_live`).

**Dimensioning tools:** `sketch_dimension` adds a smart dimension to selected sketch entities (1 entity for length/radius, 2 for between-distance), with an optional `value` parameter to set the driving value (mm for linear dimensions, degrees for angular dimensions). The dimension type is auto-detected via `IDisplayDimension.Type2`; angular values are converted to radians internally, linear values to meters. `set_dimension_value` modifies an existing dimension by selecting it at its text position. `AddDimension2` triggers a blocking "Modify" dialog; this is handled by a background thread that auto-dismisses the dialog via `win32gui` (finds window class `#32770` title "Modify", sends Enter). `AddToDB=True` / `DisplayWhenAdded=False` reduce UI overhead.

**Hole Wizard:** `IFeatureManager::HoleWizard5` takes 27 arguments on SW2022. Probed facts: the hole goes at the selection POINT (picked with `SelectByRay` along the inward normal, view-independent); several pre-selected points still give ONE hole, so the tool calls it once per position; for taps the cosmetic-thread type and thread end condition must be passed (despite the docs, -1 is not ignored there and the tap removes most of the part); a plain HOLE with THROUGH_ALL is rejected, so it is built as a blind hole deeper than the part.

**SolidWorks COM:** Uses `win32com.client` via `pywin32`. `connection.py` attempts to connect to an already-running SolidWorks instance first, then launches a new one. Template paths are discovered by glob (`C:\ProgramData\SOLIDWORKS\SOLIDWORKS *\templates\Part.prtdot` / `Assembly.asmdot`), preferring the template matching the running SW version (multiple versions' templates can coexist).

**Late-bound COM pitfalls (IMPORTANT):**
- Never test a property result with `callable()` — CDispatch COM objects are ALWAYS callable (calling one invokes its default dispatch method → 'Member not found'). Use `com_utils.com_prop(obj, name)`, which only invokes `types.MethodType` wrappers.
- Save with `Extension.SaveAs(path, 0, 1, VARIANT(VT_DISPATCH, None), errs, warns)` — options **1** = Silent. Options **2** = Copy: writes a detached copy and leaves the session doc untitled (breaks activation/scoping).
- `OpenDoc6`/`ActivateDoc3`/`AddMate5` take `VARIANT(VT_BYREF | VT_I4, 0)` byref error args.
- `AddComponent5` returns None unless the part file is already open in memory.

**Key COM APIs:**
- `FeatureExtrusion2` (23 params) - see `modeling.py`
- `FeatureCut4` (27 params) - see `modeling.py`
- `FeatureRevolve2` (20 params) - see `features.py` / `cut_features.py`
- `InsertProtrusionSwept4` / `InsertCutSwept5` - see `features.py` / `cut_features.py`
- `InsertProtrusionBlend2` (18 params for SW2025) / `InsertCutBlend2` (18 params for SW2025) - see `features.py` / `cut_features.py`
- Boundary boss/cut: `IFeatureManager::InsertNetBlend2` (Type 1 = boss, 3 = cut; `InsertBoundaryBoss`/`InsertBoundaryCut` do not exist). Profiles selected with marks 8193, 16385, … and guides with 8194, 16386, … (SW2022 API help); preceded by `SetNetBlendCurveData`/`SetNetBlendDirectionData`. Returns None even on success — the feature is found by diffing the tree - see `features.insert_boundary_feature`
- `FeatureFillet3`, `InsertFeatureChamfer`, `InsertFeatureShell`, `IFeatureManager::InsertMultiFaceDraft` (neutral plane mark 1, faces mark 2; `InsertFeatureDraft` does not exist) - see `applied_features.py`
- `FeatureLinearPattern4` (20 params for SW2025 — 11/12-param forms raise 'Parameter not optional'), `FeatureCircularPattern4`, `InsertMirrorFeature2` - see `patterns.py`
- `InsertRefPlane`, `IModelDoc2::InsertAxis2` (returns bool; `InsertRefAxis` does not exist), `InsertReferencePoint` (returns an ARRAY; swRefPointType_e AlongCurve=2 / CenterEdge=3 / FaceCenter=4 / SketchPoint=7; percent is 0-100; no coordinate type — COORDINATES goes through a 3D-sketch point), `InsertCoordinateSystem` - see `reference_geometry.py`
- `IFeatureManager::InsertRib` (10 args; returns None even on success), `InsertWrapFeature2` (5 args; sketch mark 4, faces mark 1), `InsertCutBlend` (12 args — `InsertCutBlend2` exists only on newer versions, used when present), `HoleWizard5` (27 args) - see `applied_features.py` / `cut_features.py` / `hole_features.py`
- `IFeatureManager::InsertCosmeticThread2(Type, Depth, Length, Note)` — 2nd arg becomes the minor Diameter, 3rd the BlindDepth (read back from the definition) - see `hole_features.py`
- `AddComponent5`, `AddMate5` (15 params), `GetComponents`, `IComponent2::Transform2` (getter only), MateGroup sub-feature traversal - see `assembly.py`
- `IModelDocExtension::SaveAs`, `OpenDoc6`, `ActivateDoc3`, `QuitDoc`, `GetDocuments` - see `document_manager.py` / `connection.py`
- `IFace2::GetFeature`, `IFeature::GetParents` / `GetChildren`, `ISketch::GetConstrainedStatus` (3=fully defined, 2=under defined) - see `geometry_query.py` / `state_query.py`
- `IBody2::GetBodyBox`, `IBody2::GetFaces`, `IBody2::GetEdges`, `IFace2::GetArea`, `IFace2::GetUVBounds`, `ISurface::Evaluate`, `IEdge::GetStartVertex`, `IEdge::GetEndVertex`, `IEdge::GetClosestPointOn` - see `geometry_query.py`

**Logging:** Written to `solidworks_mcp.log` in the project root and to stderr (stdout is the MCP transport). Log level is INFO.

## Platform Constraint

This project only runs on Windows with SolidWorks installed. There is no mock/stub for the COM layer—any test or development requires a live SolidWorks instance.

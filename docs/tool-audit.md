# SolidWorks MCP tool and capability audit

**Audit baseline:** server version 0.3.0, 97 MCP tools, Windows x64 with
SolidWorks 2025-compatible interops.

## Architecture map

```text
MCP client
  -> server.py (registration, dispatch, batch, result/image transport)
     -> one SolidWorksConnection (pywin32 application/session)
     -> one document-scoped StateTracker
     -> 14 semantic/query modules (94 tools)
     -> CSharpExecutionTools (2 tools)
          -> fresh self-contained .NET/Roslyn worker per request
             -> same running SW process + identity-checked active document
     -> solidworks_batch (1 server-level tool)
```

All semantic tool inputs/outputs use millimeters and degrees. The shared
selection helpers convert to meters for COM. Raw C# uses native SolidWorks API
units. Module registration remains dynamic: each module returns `Tool`
definitions, and `server.py` builds its name-to-module dispatch map at startup.

The tracker provides stable semantic IDs (`feat:`, `sketch:`, `entity:`,
`ref:`, `comp:`, and `mate:`) in a separate scope for each document. Live COM
geometry, feature IDs, rebuild state, and mass properties remain the source of
truth, especially for opened documents and C# changes made outside semantic
tools.

## Inventory and recommendation

`KEEP` means the operation remains a direct MCP tool. `KEEP / LIMITED` means it
still provides value but carries an API- or product-specific limitation. No
existing tool is deprecated or removed in this milestone; future changes should
be justified by benchmark use and the local snippet-history corpus.

### Sketching — 21 tools

Primary APIs: `ISketchManager.InsertSketch`, sketch creation methods,
`ISketchSegment`, `ISketchPoint`, relation APIs, `AddDimension2`, and dimension
system values. Mutating except the last-shape query.

| Tool | Capability | Recommendation |
|---|---|---|
| `solidworks_create_sketch` | Start a plane/face sketch and report its model/sketch coordinate frame | KEEP |
| `solidworks_sketch_rectangle` | Rectangle with absolute, relative, or spacing placement | KEEP |
| `solidworks_sketch_circle` | Circle with spatial tracking | KEEP |
| `solidworks_sketch_profile` | Chained line/arc profile with optional closure and corner fillets | KEEP |
| `solidworks_sketch_fillet` | Exact tangent sketch-corner fillet | KEEP |
| `solidworks_sketch_offset` | Offset selected sketch chains | KEEP |
| `solidworks_sketch_line` | Line segment | KEEP |
| `solidworks_sketch_centerline` | Construction centerline | KEEP |
| `solidworks_sketch_arc` | Three-point or center-point arc | KEEP |
| `solidworks_sketch_spline` | Spline through control points | KEEP |
| `solidworks_sketch_ellipse` | Ellipse | KEEP |
| `solidworks_sketch_polygon` | Regular polygon | KEEP |
| `solidworks_sketch_slot` | Straight or arc slot | KEEP |
| `solidworks_sketch_point` | Sketch point | KEEP |
| `solidworks_sketch_text` | Sketch text | KEEP |
| `solidworks_sketch_dimension` | Driving/reference smart dimension | KEEP / LIMITED: modal dialog mitigation |
| `solidworks_set_dimension_value` | Change an existing sketch dimension | KEEP |
| `solidworks_sketch_constraint` | Common geometric relation | KEEP |
| `solidworks_sketch_toggle_construction` | Toggle construction geometry | KEEP |
| `solidworks_get_last_shape_info` | Read spatial-placement state | KEEP |
| `solidworks_exit_sketch` | Exit sketch and reconcile its real feature name | KEEP |

### Core part modeling — 11 tools

Primary APIs: `NewDocument`, `FeatureExtrusion2`, `FeatureCut4`, body operations,
material database APIs, `IParameter.SystemValue`, feature suppression/deletion,
`GetMassProperties`, and feature/dimension traversal.

| Tool | Capability | Recommendation |
|---|---|---|
| `solidworks_new_part` | Create a templated part and new tracker scope | KEEP |
| `solidworks_create_extrusion` | Blind/through-all boss, optionally separate body | KEEP |
| `solidworks_combine_bodies` | ADD/SUBTRACT/COMMON multi-body operation | KEEP |
| `solidworks_create_cut_extrusion` | Blind/through-all cut with direction recovery | KEEP |
| `solidworks_set_material` | Assign SolidWorks material for correct density | KEEP |
| `solidworks_set_parameter` | Drive any named model dimension | KEEP |
| `solidworks_get_mass_properties` | Mass, volume, area, COM, optional coordinate system | KEEP |
| `solidworks_suppress_feature` | Suppress/unsuppress feature | KEEP |
| `solidworks_delete_feature` | Delete feature and reconcile tracked dependents | KEEP |
| `solidworks_list_features` | Legacy human-readable feature list | KEEP / COMPATIBILITY; prefer structured tree |
| `solidworks_list_parameters` | Discover addressable driving dimensions | KEEP |

### Boss/base and cut features — 8 tools

Primary APIs: `FeatureRevolve2`, `InsertProtrusionSwept4`,
`InsertProtrusionBlend2`, boundary feature methods, and their cut equivalents.

| Boss/base | Cut counterpart | Recommendation |
|---|---|---|
| `solidworks_revolve` | `solidworks_cut_revolve` | KEEP |
| `solidworks_sweep` | `solidworks_cut_sweep` | KEEP |
| `solidworks_loft` | `solidworks_cut_loft` | KEEP |
| `solidworks_boundary_boss` | `solidworks_boundary_cut` | KEEP |

### Applied features, patterns, and holes — 12 tools

Primary APIs: `FeatureFillet3`, `InsertFeatureChamfer`, shell/draft/rib/wrap/
intersect methods, `FeatureLinearPattern4`, `FeatureCircularPattern4`, mirror,
Hole Wizard, and cosmetic-thread APIs.

| Tool | Capability | Recommendation |
|---|---|---|
| `solidworks_fillet` | Edge or feature-owned-edge fillet | KEEP |
| `solidworks_chamfer` | Edge or feature-owned-edge chamfer | KEEP |
| `solidworks_shell` | Shell with selected removal faces | KEEP |
| `solidworks_draft` | Neutral-plane face draft | KEEP |
| `solidworks_rib` | Rib from open sketch | KEEP |
| `solidworks_wrap` | Emboss/deboss/scribe | KEEP / LIMITED: sensitive API workflow |
| `solidworks_intersect` | Intersect overlapping bodies | KEEP |
| `solidworks_linear_pattern` | One/two-direction feature pattern | KEEP |
| `solidworks_circular_pattern` | Axis-based feature pattern | KEEP |
| `solidworks_mirror` | Feature mirror about plane | KEEP |
| `solidworks_hole_wizard` | Standard Hole Wizard feature | KEEP / LIMITED: PropertyManager dialog risk |
| `solidworks_thread` | Cosmetic thread on circular edge | KEEP / LIMITED |

### Reference geometry and configurations — 11 tools

Primary APIs: `InsertRefPlane`, `InsertRefAxis`, reference point and coordinate
system methods, `IConfigurationManager`, `IParameter.SetSystemValue3`, and
`IEquationMgr`.

| Tool | Capability | Recommendation |
|---|---|---|
| `solidworks_ref_plane` | Offset/angle/through-point plane | KEEP |
| `solidworks_ref_axis` | Axis from points, face, or edge | KEEP |
| `solidworks_ref_point` | Coordinate/center/on-edge point | KEEP |
| `solidworks_coordinate_system` | Model coordinate-system feature | KEEP |
| `solidworks_add_configuration` | Add configuration | KEEP |
| `solidworks_switch_configuration` | Activate configuration | KEEP |
| `solidworks_list_configurations` | Structured configuration state | KEEP |
| `solidworks_set_config_parameter` | Set one-configuration dimension value | KEEP |
| `solidworks_add_equation` | Add/rebuild equation | KEEP |
| `solidworks_list_equations` | Query equations | KEEP |
| `solidworks_delete_equation` | Remove equation | KEEP |

### Geometry, state, and visual inspection — 10 tools

Primary APIs: body/faces/edges/vertices traversal, surface evaluation,
feature ownership, constrained sketch status, feature parent/child relations,
view orientation, and PNG capture.

| Tool | Capability | Recommendation |
|---|---|---|
| `solidworks_get_body_info` | Body count, bounds, topology overview | KEEP |
| `solidworks_get_faces` | Structured, feature-tagged face inventory | KEEP |
| `solidworks_get_edges` | Structured, adjacent-feature-tagged edge inventory | KEEP |
| `solidworks_get_face_edges` | Edges belonging to a selected face | KEEP |
| `solidworks_get_vertices` | Unique model vertices | KEEP |
| `solidworks_find_face` | Semantic face search | KEEP |
| `solidworks_look_at_model` | Orient/capture an annotated visual inspection image | KEEP |
| `solidworks_get_state` | Tracker plus live document/body/sketch/dependency state | KEEP |
| `solidworks_get_entity` | Resolve one stable tracked ID | KEEP |
| `solidworks_get_sketch_entities` | Tracked sketch entity coordinates | KEEP; live C# entities are not fabricated |

### Documents and feature tree — 12 tools

Primary APIs: `SaveAs`, `OpenDoc6`, import APIs, FeatureWorks, `ActivateDoc3`,
`QuitDoc`, document traversal, view capture, feature traversal/renaming, and
feature-tree folders.

| Tool | Capability | Recommendation |
|---|---|---|
| `solidworks_save_document` | Silent true Save As | KEEP |
| `solidworks_open_document` | Silent native-document open | KEEP |
| `solidworks_import_file` | STEP/IGES/Parasolid import, optional 3D Interconnect | KEEP / LIMITED |
| `solidworks_recognize_features` | FeatureWorks recognition of imported solids | KEEP / LIMITED: license and sketch dimensions |
| `solidworks_activate_document` | Activate open document and tracker scope | KEEP |
| `solidworks_close_document` | Close without saving and remove scope | KEEP |
| `solidworks_capture_views` | Capture standard model views | KEEP |
| `solidworks_list_documents` | Open-document inventory | KEEP |
| `solidworks_get_feature_tree` | Structured ordered design tree | KEEP |
| `solidworks_rename_feature` | Rename and re-key tracked IDs | KEEP |
| `solidworks_create_feature_folder` | Create folder with initial membership | KEEP |
| `solidworks_move_to_folder` | Move later items into folder | KEEP / LIMITED: rejected by tested SW2025 builds |

### Assemblies — 9 tools

Primary APIs: `NewDocument`, `AddComponent5`, `AddMate5`, mate dimension edits,
interference detection, component suppression, component/mate traversal, and
assembly mass properties.

| Tool | Capability | Recommendation |
|---|---|---|
| `solidworks_new_assembly` | Create assembly and tracker scope | KEEP |
| `solidworks_insert_component` | Insert saved part/component | KEEP |
| `solidworks_add_mate` | Common mates plus gear ratio | KEEP; WIDTH not implemented |
| `solidworks_list_components` | Component state and positions | KEEP |
| `solidworks_list_mates` | Mate state | KEEP |
| `solidworks_edit_mate` | Drive distance/angle mate dimensions | KEEP |
| `solidworks_check_interference` | Assembly overlap volumes/components | KEEP |
| `solidworks_suppress_component` | Component suppression | KEEP |
| `solidworks_get_assembly_mass_properties` | Assembly mass, volume, and COM | KEEP |

### Extensibility and transport — 3 tools

| Tool | Capability | Recommendation |
|---|---|---|
| `solidworks_compile_csharp` | Offline Roslyn diagnostics and interop-symbol extraction | KEEP |
| `solidworks_execute_csharp` | Target-pinned trusted snippet execution with rebuild/state diff/history | KEEP / ESCAPE HATCH |
| `solidworks_batch` | Up to 25 independent sequential calls in one MCP request | KEEP; no result chaining or nesting |

## Decisions and future audit signals

1. Structured state/query and high-frequency modeling tools remain direct even
   when C# could reproduce them; their normalized units, validation, stable IDs,
   and predictable JSON are the reliability layer.
2. C# initially replaces no wrapper. It covers unanticipated and low-frequency
   API work while recording evidence.
3. Candidates for a wrapper or new direct tool should show repeated resolved
   interop signatures/sequences, recurring compiler repair, or repeated
   verification logic in `snippet_history.sqlite3`.
4. Specialized tools should only be deprecated after equivalent C# workflows
   are demonstrably safer or more reliable and benchmark usage is negligible.
5. State-service extraction, API metadata lookup, stricter opt-in analysis, and
   experimentally validated Undo behavior remain follow-up work.

"""
SolidWorks Boss/Base Feature Tools
Revolve, Sweep, Loft, Boundary Boss
"""

import json
import logging
import math
from mcp.types import Tool
from . import selection_helpers as sel
from .com_utils import exit_active_sketch, resolve_tracked_name

logger = logging.getLogger(__name__)


def insert_boundary_feature(doc, profiles, guides, net_type, list_available=None):
    """Create a boundary boss (net_type=1) or boundary cut (net_type=3).

    SOLIDWORKS has no InsertBoundaryBoss/InsertBoundaryCut (what this module
    used to call); boundary features are IFeatureManager.InsertNetBlend2,
    per the SW2022 API help (sldworksapi.chm). Profiles are selected with
    marks 8193, 16385, 24577, ... (Direction 1) and guide curves with 8194,
    16386, ... (Direction 2), as in the official "Insert Boundary Feature"
    example. Probed on SW2022: the feature is built correctly but the call
    returns None, so the new feature is found by diffing the tree and the
    body volume is checked. Returns the feature.
    """
    def _features():
        out, f = [], doc.FirstFeature
        while f is not None:
            out.append(f)
            f = f.GetNextFeature
        return out

    def _volume():
        try:
            return float(doc.Extension.CreateMassProperty.Volume) * 1e9
        except Exception:
            return 0.0  # no solid body yet (boundary boss as first feature)

    names_before = {f.Name for f in _features()}
    volume_before = _volume()
    callout = sel.make_callout()

    sel.clear_selection(doc)
    for i, name in enumerate(profiles):
        if not doc.Extension.SelectByID2(name, "SKETCH", 0, 0, 0, i > 0,
                                         8192 * (i + 1) + 1, callout, 0):
            available = list_available(doc) if list_available else ""
            raise Exception(f"Could not select profile sketch: {name}. "
                            f"Available sketches in feature tree: [{available}]")
    for j, name in enumerate(guides):
        if not doc.Extension.SelectByID2(name, "SKETCH", 0, 0, 0, True,
                                         8192 * (j + 1) + 2, callout, 0):
            raise Exception(f"Could not select guide curve sketch: {name}")

    fm = doc.FeatureManager
    for i in range(len(profiles)):
        fm.SetNetBlendCurveData(0, i, 0, 0.0, 1.0, True)   # no end tangency
    for j in range(len(guides)):
        fm.SetNetBlendCurveData(1, j, 0, 0.0, 1.0, True)
    fm.SetNetBlendDirectionData(0, 32, 0, False, False)    # global influence
    fm.SetNetBlendDirectionData(1, 32, 0, False, False)

    fm.InsertNetBlend2(
        net_type, len(profiles), len(guides),
        False,      # HasCenterline
        0.0001,     # TessTolFactor
        True,       # WantsSolid
        True,       # MergeBody
        True, True, # FeatureScope, AutoSelect
        False, -1, -1, False, -1, False,   # no thin feature
        False, -1,  # CapEnds, EndThickness
        False, -1,  # AutoFillet, FilletRadius
        True,       # ForceNonRational
        False,      # CreateSolid (surfaces only)
    )
    sel.clear_selection(doc)

    new = [f for f in _features() if f.Name not in names_before]
    if not new:
        return None
    feature = new[-1]
    if abs(_volume() - volume_before) < 1e-3:
        raise Exception(f"Boundary feature '{feature.Name}' was created but did not "
                        "change the body — check the profiles (closed, on different "
                        "planes) and, for a cut, that they overlap the body.")
    return feature


class FeatureTools:
    """Boss/Base feature operations (revolve, sweep, loft, boundary boss)"""

    def __init__(self, connection, tracker=None):
        self.connection = connection
        self.tracker = tracker

    def _json_result(self, result, **extra):
        d = {"result": result}
        d.update(extra)
        return json.dumps(d)

    def _resolve_name(self, name_or_id):
        return resolve_tracked_name(self.tracker, name_or_id)

    def get_tool_definitions(self) -> list[Tool]:
        return [
            Tool(
                name="solidworks_revolve",
                description="Revolve the current sketch around a centerline axis to create a solid of revolution. The sketch MUST contain a centerline that serves as the revolve axis.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "angle": {
                            "type": "number",
                            "description": "Revolve angle in degrees (default: 360 for full revolution)",
                            "default": 360
                        },
                        "reverse": {
                            "type": "boolean",
                            "description": "Reverse revolve direction (default: false)",
                            "default": False
                        }
                    }
                }
            ),
            Tool(
                name="solidworks_sweep",
                description="Create a swept boss/base by sweeping a profile sketch along a path sketch. Both sketches must already exist. The profile sketch defines the cross-section shape and the path sketch defines the sweep trajectory.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "profileSketch": {
                            "type": "string",
                            "description": "Name of the profile sketch (cross-section), e.g. 'Sketch1'"
                        },
                        "pathSketch": {
                            "type": "string",
                            "description": "Name of the path sketch (trajectory), e.g. 'Sketch2'"
                        }
                    },
                    "required": ["profileSketch", "pathSketch"]
                }
            ),
            Tool(
                name="solidworks_loft",
                description="Create a lofted boss/base by blending between two or more profile sketches. The profiles should be closed sketches on different planes. They are connected in the order provided. Workflow: create_sketch on Front plane, draw profile, exit_sketch (returns sketch name); create ref_plane offset from Front (returns plane name e.g. 'Plane1'); create_sketch on that plane name, draw second profile, exit_sketch; then call loft with the sketch names.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "profileSketches": {
                            "type": "array",
                            "description": "Ordered array of profile sketch names to loft between, e.g. ['Sketch1', 'Sketch2', 'Sketch3']",
                            "items": {"type": "string"},
                            "minItems": 2
                        }
                    },
                    "required": ["profileSketches"]
                }
            ),
            Tool(
                name="solidworks_boundary_boss",
                description="Create a boundary boss/base feature from multiple profile sketches with optional guide curves. Similar to loft but with more control over surface curvature.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "profiles": {
                            "type": "array",
                            "description": "Ordered array of direction 1 profile sketch names",
                            "items": {"type": "string"},
                            "minItems": 2
                        },
                        "guideCurves": {
                            "type": "array",
                            "description": "Optional array of direction 2 guide curve sketch names",
                            "items": {"type": "string"}
                        }
                    },
                    "required": ["profiles"]
                }
            ),
        ]

    def execute(self, tool_name: str, args: dict) -> str:
        self.connection.ensure_connection()
        dispatch = {
            "solidworks_revolve": lambda: self.revolve(args),
            "solidworks_sweep": lambda: self.sweep(args),
            "solidworks_loft": lambda: self.loft(args),
            "solidworks_boundary_boss": lambda: self.boundary_boss(args),
        }
        handler = dispatch.get(tool_name)
        if not handler:
            raise Exception(f"Unknown feature tool: {tool_name}")
        return handler()

    def _list_available_sketches(self, doc) -> str:
        """List all sketch names in the feature tree for error messages."""
        try:
            features = doc.FeatureManager.GetFeatures(True)
            if features:
                names = [f.Name for f in features if f.GetTypeName2 == "ProfileFeature"]
                return ", ".join(names) if names else "none found"
        except Exception:
            pass
        return "none found"

    def _get_latest_sketch_name(self, doc) -> str:
        """Walk the feature tree in reverse to find the most recent sketch."""
        try:
            features = doc.FeatureManager.GetFeatures(True)
            if features:
                for feature in reversed(features):
                    if feature.GetTypeName2 == "ProfileFeature":
                        return feature.Name
        except Exception as e:
            logger.warning(f"Could not find latest sketch: {e}")
        return "Sketch1"

    def revolve(self, args: dict) -> str:
        doc = self.connection.get_active_doc()
        if not doc:
            raise Exception("No active document")

        angle_deg = args.get("angle", 360)
        angle_rad = math.radians(angle_deg)
        reverse = args.get("reverse", False)

        # Exit sketch mode (tracker-aware) and select the sketch
        sketch_name = exit_active_sketch(doc, self.tracker) or self._get_latest_sketch_name(doc)
        sketch_feature = doc.FeatureByName(sketch_name)
        if not sketch_feature:
            raise Exception(f"Could not find sketch: {sketch_name}")

        doc.ClearSelection2(True)
        sketch_feature.Select2(False, 0)

        # FeatureRevolve2 parameters:
        # SingleDir, IsSolid, IsThin, IsCut, ReverseDir,
        # BothDirectionUpToSameEntity,
        # Dir1Type, Dir2Type, Dir1Angle, Dir2Angle,
        # OffsetReverse1, OffsetReverse2,
        # OffsetDist1, OffsetDist2,
        # ThinType, ThinThickness1, ThinThickness2,
        # Merge, UseFeatScope, UseAutoSelect
        feature = doc.FeatureManager.FeatureRevolve2(
            True,       # SingleDir
            True,       # IsSolid
            False,      # IsThin
            False,      # IsCut
            reverse,    # ReverseDir
            False,      # BothDirectionUpToSameEntity
            0,          # Dir1Type: 0 = Blind
            0,          # Dir2Type: 0 = Blind
            angle_rad,  # Dir1Angle
            0.0,        # Dir2Angle
            False,      # OffsetReverse1
            False,      # OffsetReverse2
            0.0,        # OffsetDist1
            0.0,        # OffsetDist2
            0,          # ThinType: 0 = One Direction
            0.0,        # ThinThickness1
            0.0,        # ThinThickness2
            True,       # Merge
            True,       # UseFeatScope
            True,       # UseAutoSelect
        )

        if not feature:
            raise Exception("Failed to create revolve. Ensure sketch contains a centerline for the axis.")

        feature_name = feature.Name
        doc.ViewZoomtofit2()
        logger.info(f"Revolve '{feature_name}' created: {angle_deg}°")
        feature_id = ""
        if self.tracker:
            feature_id = self.tracker.register_feature(feature_name, "revolve", parameters={"angle": angle_deg})
        return self._json_result(f"✓ Revolve '{feature_name}' {angle_deg}° created", id=feature_id, type="revolve")

    def sweep(self, args: dict) -> str:
        doc = self.connection.get_active_doc()
        if not doc:
            raise Exception("No active document")

        profile_name = self._resolve_name(args["profileSketch"])
        path_name = self._resolve_name(args["pathSketch"])

        # Exit any active sketch (tracker-aware)
        exit_active_sketch(doc, self.tracker)

        # Select profile sketch (mark=1) and path sketch (mark=4)
        sel.clear_selection(doc)
        if not sel.select_sketch(doc, profile_name, mark=1, append=False):
            raise Exception(f"Could not select profile sketch: {profile_name}")
        if not sel.select_sketch(doc, path_name, mark=4, append=True):
            raise Exception(f"Could not select path sketch: {path_name}")

        # InsertProtrusionSwept4 takes exactly 20 parameters on SW2025 —
        # fewer raises DISP_E_PARAMNOTOPTIONAL ('Parameter not optional').
        # Verified live: this call produces a correct simple sweep
        # (probe: circle r5 along 60mm line -> volume 4712.4 mm^3, exact).
        feature = doc.FeatureManager.InsertProtrusionSwept4(
            False,      # Propagate
            False,      # Alignment
            0,          # TwistCtrlOption: 0 = none
            False,      # PathAlign
            False,      # KeepTangency
            0,          # ThinType
            0.0,        # ThinThickness1
            0.0,        # ThinThickness2
            False,      # ThinReverse
            True,       # Merge
            True,       # UseFeatScope
            True,       # UseAutoSelect
            0,          # StartTangentType
            0,          # EndTangentType
            False,      # IsThinBody
            0.0,        # TwistAngle
            False,      # MergeSmoothFaces
            0, 0, 0,    # trailing SW2025 params (defaults; count verified live)
        )

        if not feature:
            raise Exception(
                "Failed to create sweep. Ensure the profile is a closed sketch, "
                "the path is an open sketch on a different plane, and the path "
                "does NOT lie within the profile's plane — it must head away from "
                "the profile (ideally starting perpendicular to it)."
            )

        feature_name = feature.Name
        doc.ViewZoomtofit2()
        logger.info(f"Sweep '{feature_name}' created: profile={profile_name}, path={path_name}")
        feature_id = ""
        if self.tracker:
            feature_id = self.tracker.register_feature(feature_name, "sweep", parameters={"profile": profile_name, "path": path_name})
        return self._json_result(f"✓ Sweep '{feature_name}' created (profile: {profile_name}, path: {path_name})", id=feature_id, type="sweep")

    def loft(self, args: dict) -> str:
        doc = self.connection.get_active_doc()
        if not doc:
            raise Exception("No active document")

        profile_names = [self._resolve_name(n) for n in args["profileSketches"]]
        if len(profile_names) < 2:
            raise Exception("At least 2 profile sketches are required for a loft")

        # Exit any active sketch (tracker-aware)
        exit_active_sketch(doc, self.tracker)

        # Select all profile sketches with mark=1
        sel.clear_selection(doc)
        for i, name in enumerate(profile_names):
            if not sel.select_sketch(doc, name, mark=1, append=(i > 0)):
                available = self._list_available_sketches(doc)
                raise Exception(
                    f"Could not select profile sketch: {name}. "
                    f"Available sketches in feature tree: [{available}]"
                )

        # InsertProtrusionBlend2 parameters (18 params for SW2025):
        # Closed, KeepTangency, ForceNonRational, TessToleranceFactor,
        # StartMatchingType, EndMatchingType,
        # StartTangentLength, EndTangentLength,
        # MaintainTangency, Merge, IsThinBody,
        # Thickness1, Thickness2, ThinType,
        # UseFeatScope, UseAutoSelect, Close, GuideCurveInfluence
        feature = doc.FeatureManager.InsertProtrusionBlend2(
            False,  # Closed
            True,   # KeepTangency
            True,   # ForceNonRational
            1.0,    # TessToleranceFactor
            0,      # StartMatchingType: 0 = None
            0,      # EndMatchingType: 0 = None
            1.0,    # StartTangentLength
            1.0,    # EndTangentLength
            False,  # MaintainTangency
            True,   # Merge
            False,  # IsThinBody
            0.0,    # Thickness1
            0.0,    # Thickness2
            0,      # ThinType
            True,   # UseFeatScope
            True,   # UseAutoSelect
            True,   # Close (feature scope)
            0,      # GuideCurveInfluence
        )

        if not feature:
            raise Exception(
                "Failed to create loft. Ensure profiles are closed sketches on different planes."
            )

        feature_name = feature.Name
        doc.ViewZoomtofit2()
        logger.info(f"Loft '{feature_name}' created from {len(profile_names)} profiles")
        feature_id = ""
        if self.tracker:
            feature_id = self.tracker.register_feature(feature_name, "loft", parameters={"profiles": profile_names})
        return self._json_result(f"✓ Loft '{feature_name}' created from {len(profile_names)} profiles: {', '.join(profile_names)}", id=feature_id, type="loft")

    def boundary_boss(self, args: dict) -> str:
        doc = self.connection.get_active_doc()
        if not doc:
            raise Exception("No active document")

        profiles = [self._resolve_name(n) for n in args["profiles"]]
        guide_curves = [self._resolve_name(n) for n in args.get("guideCurves", [])]

        if len(profiles) < 2:
            raise Exception("At least 2 profile sketches are required for a boundary boss")

        # Exit any active sketch (tracker-aware)
        exit_active_sketch(doc, self.tracker)

        feature = insert_boundary_feature(doc, profiles, guide_curves, 1,
                                          self._list_available_sketches)

        if not feature:
            raise Exception(
                "Failed to create boundary boss. Ensure profiles are closed sketches on different planes."
            )

        feature_name = feature.Name
        doc.ViewZoomtofit2()
        logger.info(f"Boundary boss '{feature_name}' created from {len(profiles)} profiles")
        feature_id = ""
        if self.tracker:
            feature_id = self.tracker.register_feature(feature_name, "boundary_boss", parameters={"profiles": profiles})
        return self._json_result(f"✓ Boundary boss '{feature_name}' created from {len(profiles)} profiles", id=feature_id, type="boundary_boss")

"""
SolidWorks Hole Feature Tools
Hole Wizard, Thread

NOTE: Advanced Hole and Stud are deferred - these are wizard-driven UI features
that are extremely unlikely to work via COM automation without blocking dialogs.
"""

import json
import logging
import math
from mcp.types import Tool
from . import selection_helpers as sel
from .com_utils import com_prop

logger = logging.getLogger(__name__)


def _volume_mm3(doc):
    try:
        return float(com_prop(doc.Extension, "CreateMassProperty").Volume) * 1e9
    except Exception:
        return 0.0


class HoleFeatureTools:
    """Hole and thread feature operations"""

    def __init__(self, connection, tracker=None):
        self.connection = connection
        self.tracker = tracker

    def _json_result(self, result, **extra):
        d = {"result": result}
        d.update(extra)
        return json.dumps(d)

    def get_tool_definitions(self) -> list[Tool]:
        return [
            Tool(
                name="solidworks_hole_wizard",
                description=(
                    "Create Hole Wizard holes (standard drilled / counterbore / countersink / tapped "
                    "holes) on a planar face, one at each requested point. Sizes come from the "
                    "SOLIDWORKS hole standard tables, so they must be spelled exactly as SOLIDWORKS "
                    "lists them: drill sizes like 'Ø10.0', screw and tap sizes like 'M6'. "
                    "Verified on SOLIDWORKS 2022 for ISO."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "type": {
                            "type": "string",
                            "enum": ["HOLE", "COUNTERBORE", "COUNTERSINK", "STRAIGHT_TAP"],
                            "description": "Hole type (HOLE = plain drilled hole)"
                        },
                        "standard": {
                            "description": "Hole standard: 'ISO' (default), 'ANSI Metric', 'ANSI Inch', 'DIN', 'JIS', 'GB', 'BSI', 'KS', 'IS' — or the integer swWzdHoleStandards_e value.",
                            "default": "ISO"
                        },
                        "fastenerType": {
                            "type": "integer",
                            "description": "Optional swWzdHoleStandardFastenerTypes_e value. Defaults for ISO / ANSI Metric: HOLE = drill sizes, COUNTERBORE = socket head cap screw, COUNTERSINK = flat head, STRAIGHT_TAP = tapped hole."
                        },
                        "size": {
                            "type": "string",
                            "description": "Size exactly as in the SOLIDWORKS table: 'Ø10.0' for a 10 mm drill size, 'M6' for counterbore/countersink/tap."
                        },
                        "endCondition": {
                            "type": "string",
                            "enum": ["BLIND", "THROUGH_ALL"],
                            "description": "Hole end condition (default: BLIND)"
                        },
                        "depth": {
                            "type": "number",
                            "description": "Hole (drill) depth in mm for BLIND (default 10)"
                        },
                        "threadDepth": {
                            "type": "number",
                            "description": "STRAIGHT_TAP only: thread depth in mm (default: depth)"
                        },
                        "face": {
                            "type": "object",
                            "description": "Point ON the planar face where the hole goes ({x, y, z} in mm, model space). Used as the hole position when 'positions' is not given.",
                            "properties": {
                                "x": {"type": "number"},
                                "y": {"type": "number"},
                                "z": {"type": "number"}
                            },
                            "required": ["x", "y", "z"]
                        },
                        "positions": {
                            "type": "array",
                            "description": "Optional: several hole centres, each a model-space point ON the same face ({x, y, z} in mm). One hole feature is created per point.",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "x": {"type": "number"},
                                    "y": {"type": "number"},
                                    "z": {"type": "number"}
                                },
                                "required": ["x", "y", "z"]
                            }
                        }
                    },
                    "required": ["type", "size", "face"]
                }
            ),
            Tool(
                name="solidworks_thread",
                description=(
                    "Apply a cosmetic thread to a cylindrical edge or face. Cosmetic threads display "
                    "thread callouts in drawings but do not modify the solid geometry."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "edge": {
                            "type": "object",
                            "description": "Point on the circular edge to apply thread ({x, y, z} in mm)",
                            "properties": {
                                "x": {"type": "number"},
                                "y": {"type": "number"},
                                "z": {"type": "number"}
                            },
                            "required": ["x", "y", "z"]
                        },
                        "depth": {
                            "type": "number",
                            "description": "Thread depth/length (mm)"
                        },
                        "diameter": {
                            "type": "number",
                            "description": "Minor diameter (mm) for the thread display"
                        }
                    },
                    "required": ["edge", "depth", "diameter"]
                }
            ),
        ]

    def execute(self, tool_name: str, args: dict) -> str:
        self.connection.ensure_connection()
        dispatch = {
            "solidworks_hole_wizard": lambda: self.hole_wizard(args),
            "solidworks_thread": lambda: self.thread(args),
        }
        handler = dispatch.get(tool_name)
        if not handler:
            raise Exception(f"Unknown hole feature tool: {tool_name}")
        return handler()

    # swWzdGeneralHoleTypes_e
    _HOLE_TYPES = {"COUNTERBORE": 0, "COUNTERSINK": 1, "HOLE": 2, "STRAIGHT_TAP": 4}
    # swWzdHoleStandards_e
    _STANDARDS = {"ANSI INCH": 0, "ANSI METRIC": 1, "BSI": 2, "DIN": 4, "ISO": 8,
                  "JIS": 9, "GB": 13, "KS": 14, "IS": 15}
    # default swWzdHoleStandardFastenerTypes_e per (standard, type)
    _DEFAULT_FASTENER = {
        (8, "HOLE"): 143, (8, "COUNTERBORE"): 139, (8, "COUNTERSINK"): 141, (8, "STRAIGHT_TAP"): 147,
        (1, "HOLE"): 39, (1, "COUNTERBORE"): 33, (1, "COUNTERSINK"): 36, (1, "STRAIGHT_TAP"): 43,
    }

    @staticmethod
    def _outward_normal(face):
        """Outward unit normal of a planar face (None if not planar)."""
        try:
            surface = face.GetSurface
            if surface.Identity != 4001:      # PLANE_TYPE
                return None
            p = surface.PlaneParams
            n = [p[0], p[1], p[2]]
            if com_prop(face, "FaceInSurfaceSense"):
                n = [-v for v in n]
            m = math.sqrt(sum(v * v for v in n))
            return [v / m for v in n]
        except Exception:
            return None

    @staticmethod
    def _through_depth(doc, point_m, direction):
        """Distance (m) from point along direction to the far side of the
        part's bounding box, + 1 mm, so a blind hole goes all the way through."""
        lo, hi = [1e9] * 3, [-1e9] * 3
        for body in doc.GetBodies2(0, True) or []:
            box = body.GetBodyBox()
            for k in range(3):
                lo[k], hi[k] = min(lo[k], box[k]), max(hi[k], box[k + 3])
        far = 0.0
        for cx in (lo[0], hi[0]):
            for cy in (lo[1], hi[1]):
                for cz in (lo[2], hi[2]):
                    c = (cx, cy, cz)
                    far = max(far, sum((c[k] - point_m[k]) * direction[k] for k in range(3)))
        return far + 0.001

    def hole_wizard(self, args: dict) -> str:
        """IFeatureManager.HoleWizard5 (27 arguments, SW2022 API help).

        Probed on SW2022 (see NOTES): the hole is placed at the selection
        POINT, so each position is picked with a ray shot into the face along
        its inward normal (view-independent). Selecting several points before
        one call still makes a single hole, so one call is made per position.
        Values SOLIDWORKS documents as 'ignored when -1' are not all ignored:
        for taps the cosmetic-thread type and thread end condition must be
        given, or the tap removes most of the part. A plain HOLE with
        THROUGH_ALL is rejected by HoleWizard5 on SW2022 (counterbores are
        not), so it is built as a BLIND hole deep enough to exit the part.
        """
        doc = self.connection.get_active_doc()
        if not doc:
            raise Exception("No active document")

        hole_type = args["type"]
        if hole_type not in self._HOLE_TYPES:
            raise Exception(f"Unsupported hole type {hole_type}; use one of {list(self._HOLE_TYPES)}")
        std = args.get("standard", "ISO")
        if isinstance(std, str):
            key = std.strip().upper().replace("_", " ")
            if key not in self._STANDARDS:
                raise Exception(f"Unknown standard '{std}'; use one of {list(self._STANDARDS)} or an integer")
            std = self._STANDARDS[key]
        fastener = args.get("fastenerType")
        if fastener is None:
            fastener = self._DEFAULT_FASTENER.get((std, hole_type))
            if fastener is None:
                raise Exception("No default fastenerType for this standard — pass the integer "
                                "swWzdHoleStandardFastenerTypes_e value")
        size = args["size"]
        end = args.get("endCondition", "BLIND")
        depth_m = args.get("depth", 10) / 1000.0
        thread_m = args.get("threadDepth", args.get("depth", 10)) / 1000.0

        points = args.get("positions") or [args["face"]]
        created, removed_total = [], 0.0
        for pt in points:
            p_m = [pt["x"] / 1000.0, pt["y"] / 1000.0, pt["z"] / 1000.0]
            face = sel._nearest_entity(doc, "face", *p_m)
            n = self._outward_normal(face) if face is not None else None
            if n is None:
                raise Exception(f"No planar face at {pt} mm — the point must lie ON a flat face "
                                "(use get_faces / find_face for exact coordinates)")
            inward = [-v for v in n]

            end_type, hole_depth = (1, -1) if end == "THROUGH_ALL" else (0, depth_m)
            if end == "THROUGH_ALL" and hole_type == "HOLE":
                end_type, hole_depth = 0, self._through_depth(doc, p_m, inward)

            if hole_type == "STRAIGHT_TAP":
                # thread depth, near/far csink (4), drill angle, cosmetic thread
                # type (none), thread end condition (blind), hcoil, offset, -, -
                values = [thread_m, -1, -1, -1, -1, -1, 0, 0, -1, -1, -1, -1]
            else:
                values = [-1] * 12

            volume_before = _volume_mm3(doc)
            sel.clear_selection(doc)
            start = [p_m[k] + n[k] * 0.001 for k in range(3)]
            if not doc.Extension.SelectByRay(start[0], start[1], start[2],
                                             inward[0], inward[1], inward[2],
                                             0.0005, 2, False, 0, 0):   # 2 = swSelFACES
                raise Exception(f"Could not pick the face at {pt} mm")
            feature = doc.FeatureManager.HoleWizard5(
                self._HOLE_TYPES[hole_type], std, fastener, size, end_type,
                -1,             # Diameter: from the standard table
                hole_depth,     # Depth
                -1,             # Length (slots only)
                *values,
                "",             # ThreadClass (ANSI inch only)
                False,          # RevDir
                True, True,     # FeatureScope, AutoSelect
                True, True,     # AssemblyFeatureScope, AutoSelectComponents
                False,          # PropagateFeatureToParts
            )
            sel.clear_selection(doc)
            if not feature:
                raise Exception(
                    f"Hole Wizard rejected {hole_type} '{size}' (standard {std}, fastener type "
                    f"{fastener}). The size must match the SOLIDWORKS table exactly — e.g. "
                    f"'Ø10.0' for ISO drill sizes, 'M6' for screws/taps.")
            removed = volume_before - _volume_mm3(doc)
            if removed <= 1e-6:
                raise Exception(f"Hole '{feature.Name}' was created but removed no material")
            removed_total += removed
            fid = feature.Name
            if self.tracker:
                fid = self.tracker.register_feature(feature.Name, "hole_wizard", parameters={
                    "hole_type": hole_type, "standard": std, "size": size,
                    "endCondition": end, "position_mm": [pt["x"], pt["y"], pt["z"]],
                })
            created.append(fid)

        doc.ViewZoomtofit2()
        msg = (f"✓ {len(created)} Hole Wizard {hole_type} '{size}' hole(s) created "
               f"({end}), removing {removed_total:.1f} mm^3")
        logger.info(msg)
        return self._json_result(msg, id=created[0] if len(created) == 1 else created,
                                 type="hole_wizard", removed_mm3=round(removed_total, 2))

    def thread(self, args: dict) -> str:
        doc = self.connection.get_active_doc()
        if not doc:
            raise Exception("No active document")

        edge = args["edge"]
        depth_m = args["depth"] / 1000.0
        diameter_m = args["diameter"] / 1000.0

        sel.clear_selection(doc)
        if not sel.select_edge(doc, edge["x"], edge["y"], edge["z"]):
            raise Exception("Could not select edge for thread")

        # IFeatureManager.InsertCosmeticThread2(Type, Depth, Length, Note).
        # (The previous call, IModelDoc2.InsertCosmeticThread3, does not
        # exist; InsertCosmeticThread3 is on IFeatureManager with a different
        # 7-argument, thread-standard signature.) Probed on SW2022 by reading
        # the feature definition back: the 2nd argument becomes Diameter
        # (minor, DiameterType=3) and the 3rd BlindDepth — despite the names.
        feature = doc.FeatureManager.InsertCosmeticThread2(
            0,              # swApplyCosmeticThread_Blind
            diameter_m,     # -> minor diameter
            depth_m,        # -> blind depth (thread length)
            "",             # Note
        )

        if not feature:
            raise Exception("Failed to create cosmetic thread. Ensure a circular edge is selected.")

        feature_name = feature.Name
        feature_id = None
        if self.tracker:
            feature_id = self.tracker.register_feature(feature_name, "thread", parameters={
                "depth": args["depth"],
                "diameter": args["diameter"],
            })
        doc.ViewZoomtofit2()
        logger.info(f"Cosmetic thread '{feature_name}' created: depth={args['depth']}mm, diameter={args['diameter']}mm")
        return self._json_result(f"✓ Cosmetic thread '{feature_name}' created: {args['depth']}mm deep, {args['diameter']}mm diameter", id=feature_id, type="thread")

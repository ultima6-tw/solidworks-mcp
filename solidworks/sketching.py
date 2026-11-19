"""
SolidWorks Sketching Tools
Handles sketch creation and drawing operations with spatial tracking
"""

import contextlib
import json
import logging
import math
import threading
import time
from mcp.types import Tool
from typing import Any, Dict, List

from .com_utils import com_prop
from .feature_errors import newest_sketch_name

logger = logging.getLogger(__name__)


# swUserPreferenceToggle_e.swInputDimValOnCreate (swconst.tlb) — Tools >
# Options > General > "Input dimension value". While ON, AddDimension2 opens
# the modal "Modify" dialog and blocks until someone answers it.
SW_INPUT_DIM_VAL_ON_CREATE = 10

# Title of that dialog per UI language. Only used by the fallback dismisser;
# the preference switch above is language-independent. Verified titles only:
# "Modify" (English, upstream), "修改" (Traditional Chinese, SW2022).
_MODIFY_DIALOG_TITLES = ("Modify", "修改")


@contextlib.contextmanager
def no_dimension_value_prompt(app):
    """Temporarily turn off "Input dimension value" so AddDimension2 never
    opens the blocking Modify dialog, then restore the user's setting.
    Never raises: if the preference cannot be read/changed, the caller still
    has dismiss_modify_dialog() as a fallback."""
    previous = None
    try:
        previous = bool(app.GetUserPreferenceToggle(SW_INPUT_DIM_VAL_ON_CREATE))
        if previous:
            app.SetUserPreferenceToggle(SW_INPUT_DIM_VAL_ON_CREATE, False)
    except Exception as e:
        logger.warning(f"could not switch off 'Input dimension value': {e}")
        previous = None
    try:
        yield
    finally:
        if previous:
            try:
                app.SetUserPreferenceToggle(SW_INPUT_DIM_VAL_ON_CREATE, True)
            except Exception as e:
                logger.warning(f"could not restore 'Input dimension value': {e}")


# swUserPreferenceToggle_e.swSketchInference (swconst.tlb, SW2022) — Tools >
# Options > Sketch > Relations/Snaps > "Enable snapping". While ON, a Create*
# call can be pulled onto nearby geometry: probed at a zoomed-out view (scale
# 0.42), a radius-1.7 circle next to a 7 mm-wide rectangle came out radius 3.5
# (its edge snapped onto the rectangle's side), and a rectangle drawn in line
# with small circles lost half its width (its sides snapped to the circles'
# quadrant points) -- even with AddToDB=True. With this toggle OFF both draw
# exactly. It is also what joins separately drawn lines that share an
# endpoint, so it is only switched off for a redraw that has already gone wrong.
SW_SKETCH_INFERENCE = 249

# Read-back tolerance for drawn geometry, in metres (1e-5 mm).
_DRAW_TOL_M = 1e-8


@contextlib.contextmanager
def sketch_snapping_off(app):
    """Temporarily turn off sketch snapping, then restore the user's setting.
    Never raises: if the preference cannot be read/changed, the redraw simply
    runs with whatever is set and the read-back check reports the outcome."""
    previous = None
    try:
        previous = bool(app.GetUserPreferenceToggle(SW_SKETCH_INFERENCE))
        if previous:
            app.SetUserPreferenceToggle(SW_SKETCH_INFERENCE, False)
    except Exception as e:
        logger.warning(f"could not switch off sketch snapping: {e}")
        previous = None
    try:
        yield
    finally:
        if previous:
            try:
                app.SetUserPreferenceToggle(SW_SKETCH_INFERENCE, True)
            except Exception as e:
                logger.warning(f"could not restore sketch snapping: {e}")


def _xy(point):
    """(x, y) in metres from a sketch point (COM object with X/Y) or a
    coordinate array, whichever late binding handed back."""
    if hasattr(point, "X"):
        return float(com_prop(point, "X")), float(com_prop(point, "Y"))
    seq = list(point)
    return float(seq[0]), float(seq[1])


def _segment_list(obj):
    """A Create* result as a list of sketch segments (rectangles return an
    array of four lines, the other calls a single segment)."""
    if isinstance(obj, (list, tuple)):
        return [s for s in obj if s is not None]
    return [obj]


def _near(p, q, tol=_DRAW_TOL_M):
    return abs(p[0] - q[0]) <= tol and abs(p[1] - q[1]) <= tol


def _fmt_mm(p):
    return f"({p[0] * 1000:.4f}, {p[1] * 1000:.4f})"


def _seg_sig(seg):
    """Geometry signature of a sketch segment, used to find the segments a
    Create* call added when the call itself returns something else
    (CreateSketchSlot returns an ISketchSlot). ISketchSegment.GetID is no use
    for this: probed on SW2022 it repeats within one sketch."""
    try:
        t = com_prop(seg, "GetType")
        r = lambda p: tuple(round(v, 9) for v in _xy(p))
        sig = [t]
        if t in (0, 1):
            sig += sorted([r(com_prop(seg, "GetStartPoint2")), r(com_prop(seg, "GetEndPoint2"))])
        if t == 1:
            sig.append(r(com_prop(seg, "GetCenterPoint2")))
        sig.append(bool(com_prop(seg, "ConstructionGeometry")))
        return tuple(sig)
    except Exception:
        return ("?",)


def _spline_points(obj):
    """Through-points of a spline in metres. GetPoints2 comes back as sketch
    point objects or as a flat x,y,z array depending on late binding."""
    v = getattr(obj, "GetPoints2")
    seq = list(v() if callable(v) else v)
    if seq and isinstance(seq[0], float):
        return [(seq[i], seq[i + 1]) for i in range(0, len(seq), 3)]
    return [_xy(p) for p in seq]


def dismiss_modify_dialog(delay=0.5, max_wait=10.0):
    """Fallback: find and auto-dismiss the SolidWorks 'Modify' dimension dialog.

    AddDimension2 can trigger a blocking popup (Win32 class #32770, title
    "Modify" — localized, e.g. "修改" in Chinese). Prefer
    no_dimension_value_prompt(), which stops the dialog from opening at all;
    run this in a background thread *before* AddDimension2 as a safety net.
    """
    import win32gui
    import win32con

    deadline = time.monotonic() + max_wait
    time.sleep(delay)
    while time.monotonic() < deadline:
        try:
            for title in _MODIFY_DIALOG_TITLES:
                hwnd = win32gui.FindWindow("#32770", title)
                if hwnd and win32gui.IsWindowVisible(hwnd):
                    win32gui.PostMessage(hwnd, win32con.WM_KEYDOWN, win32con.VK_RETURN, 0)
                    win32gui.PostMessage(hwnd, win32con.WM_KEYUP, win32con.VK_RETURN, 0)
                    logger.info(f"Auto-dismissed '{title}' dimension dialog")
                    return
        except Exception:
            pass
        time.sleep(0.1)



class _direct_retry:
    """SketchManager proxy: if a Create* call returns None, retry it ONCE with
    AddToDB=True, then restore the previous AddToDB value.

    Probed on SW2022: with AddToDB off (the default), small entities fail
    depending on the current view scale — a radius-1mm circle returns None
    while 2mm works, anywhere in the sketch. With AddToDB on the same call
    succeeds. The retry only rescues calls that would have failed.

    A call that SUCCEEDS can still be wrong: snapping may move or resize the
    entity. That is caught separately by SketchingTools._draw_checked, which
    reads the geometry back.
    """

    def __init__(self, doc):
        self._sm = doc.SketchManager

    def __getattr__(self, name):
        method = getattr(self._sm, name)

        def call(*args):
            obj = method(*args)
            if obj is not None:
                return obj
            prev = self._sm.AddToDB
            try:
                self._sm.AddToDB = True
                obj = method(*args)
                if obj is not None:
                    logger.info(f"{name} succeeded only with AddToDB=True "
                                "(entity too small for the current view scale)")
                return obj
            finally:
                self._sm.AddToDB = prev
        return call

class SketchingTools:
    """Sketch creation and drawing operations with spatial awareness"""
    
    def __init__(self, connection, tracker=None):
        self.connection = connection
        self.tracker = tracker
        self.current_sketch_name = None
        self.sketch_counter = 0

        # Spatial tracking - stores info about created shapes
        self.created_shapes = []  # List of shape info
        self.last_shape = None    # Most recent shape for "relative to last" positioning

    def _json_result(self, result, **extra):
        d = {"result": result}
        d.update(extra)
        return json.dumps(d)

    def _sync_spatial(self):
        """Sync spatial tracking from tracker."""
        if self.tracker:
            self.last_shape = self.tracker.last_shape
            self.created_shapes = self.tracker.created_shapes
    
    def get_tool_definitions(self) -> list[Tool]:
        """Define all sketching tools"""
        return [
            Tool(
                name="solidworks_create_sketch",
                description="Create a new sketch on a specified plane. If no part exists, creates one automatically. The result includes sketchFrame: the sketch origin and X/Y axis directions in MODEL space. IMPORTANT for face sketches: (1) sketch axes can be mirrored or rotated relative to model axes (e.g. on a back-facing face sketch X = model -X; on a top face sketch Y = model -Z); (2) the sketch ORIGIN is the model origin projected onto the face — NOT the face center. The result therefore also includes faceCenter.sketch_mm — use it as the layout datum for centered geometry. Convert other model coordinates with: sketch_x = xAxisModel . (target - originModel_mm), sketch_y = yAxisModel . (target - originModel_mm).",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "plane": {
                            "type": "string",
                            "description": "Reference plane name. Standard planes: 'Front', 'Top', 'Right'. Also accepts custom reference plane names like 'Plane1', 'Plane2', etc."
                        },
                        "faceX": {
                            "type": "number",
                            "description": "X coordinate (mm) of a point on the solid face to sketch on. Use with faceY and faceZ to create a sketch on an existing solid face instead of a reference plane."
                        },
                        "faceY": {
                            "type": "number",
                            "description": "Y coordinate (mm) of a point on the solid face to sketch on."
                        },
                        "faceZ": {
                            "type": "number",
                            "description": "Z coordinate (mm) of a point on the solid face to sketch on."
                        }
                    }
                }
            ),
            Tool(
                name="solidworks_sketch_rectangle",
                description="Draw a rectangle in the active sketch. Can specify absolute position or position relative to the last created shape.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "width": {
                            "type": "number",
                            "description": "Width of rectangle (mm)"
                        },
                        "height": {
                            "type": "number",
                            "description": "Height of rectangle (mm)"
                        },
                        "centerX": {
                            "type": "number",
                            "description": "Absolute X position of center (mm)"
                        },
                        "centerY": {
                            "type": "number",
                            "description": "Absolute Y position of center (mm)"
                        },
                        "relativeX": {
                            "type": "number",
                            "description": "X offset from last shape's right edge (mm)"
                        },
                        "relativeY": {
                            "type": "number",
                            "description": "Y offset from last shape's top edge (mm)"
                        },
                        "spacing": {
                            "type": "number",
                            "description": "Spacing from last shape (mm). Automatically calculates position."
                        },
                        "x1": {
                            "type": "number",
                            "description": "Alternative: First corner X (mm)"
                        },
                        "y1": {
                            "type": "number",
                            "description": "Alternative: First corner Y (mm)"
                        },
                        "x2": {
                            "type": "number",
                            "description": "Alternative: Opposite corner X (mm)"
                        },
                        "y2": {
                            "type": "number",
                            "description": "Alternative: Opposite corner Y (mm)"
                        }
                    }
                }
            ),
            Tool(
                name="solidworks_sketch_circle",
                description="Draw a circle in the active sketch. Can specify absolute position or relative to last shape.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "centerX": {
                            "type": "number",
                            "description": "Absolute center X position (mm)"
                        },
                        "centerY": {
                            "type": "number",
                            "description": "Absolute center Y position (mm)"
                        },
                        "radius": {
                            "type": "number",
                            "description": "Radius (mm)"
                        },
                        "relativeX": {
                            "type": "number",
                            "description": "X offset from last shape (mm)"
                        },
                        "relativeY": {
                            "type": "number",
                            "description": "Y offset from last shape (mm)"
                        },
                        "spacing": {
                            "type": "number",
                            "description": "Spacing from last shape (mm)"
                        }
                    },
                    "required": ["radius"]
                }
            ),
            Tool(
                name="solidworks_sketch_profile",
                description=(
                    "Draw an entire chained profile in ONE call: a sequence of "
                    "connected segments (LINE, TANGENT_ARC, ARC) starting at "
                    "startX/startY, each continuing from the previous endpoint, "
                    "optionally auto-closed, with optional corner fillets applied "
                    "afterward (exact tangency — no coordinate math needed). "
                    "STRONGLY PREFERRED over many sketch_line/sketch_arc calls "
                    "for complex drawing profiles: one call instead of 10-20."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "startX": {"type": "number", "description": "Profile start X (mm)"},
                        "startY": {"type": "number", "description": "Profile start Y (mm)"},
                        "segments": {
                            "type": "array",
                            "minItems": 1,
                            "items": {
                                "type": "object",
                                "properties": {
                                    "type": {"type": "string",
                                             "enum": ["LINE", "TANGENT_ARC", "ARC"],
                                             "description": "Segment kind (default LINE)"},
                                    "x": {"type": "number", "description": "Segment end X (mm)"},
                                    "y": {"type": "number", "description": "Segment end Y (mm)"},
                                    "cx": {"type": "number", "description": "ARC only: center X (mm)"},
                                    "cy": {"type": "number", "description": "ARC only: center Y (mm)"},
                                    "direction": {"type": "integer",
                                                  "description": "ARC only: 1=CCW, -1=CW (default 1)"},
                                    "tangentType": {"type": "integer",
                                                    "description": "TANGENT_ARC only: swTangentArcTypes (default 1=forward)"}
                                },
                                "required": ["x", "y"]
                            },
                            "description": "Connected segments; each starts where the previous ended"
                        },
                        "close": {"type": "boolean",
                                  "description": "Close back to start with a line if not already closed (default true)"},
                        "cornerFillets": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "x": {"type": "number", "description": "Corner vertex X (mm)"},
                                    "y": {"type": "number", "description": "Corner vertex Y (mm)"},
                                    "radius": {"type": "number", "description": "Fillet radius (mm)"}
                                },
                                "required": ["x", "y", "radius"]
                            },
                            "description": "Sketch fillets applied at profile corners after drawing"
                        }
                    },
                    "required": ["startX", "startY", "segments"]
                }
            ),
            Tool(
                name="solidworks_sketch_fillet",
                description=(
                    "Round a corner of EXISTING sketch geometry with an exact "
                    "tangent arc — SolidWorks computes the tangency; never "
                    "hand-calculate fillet arcs. Select the corner by the vertex "
                    "where the two segments meet."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "x": {"type": "number", "description": "Corner vertex X (mm) — the shared endpoint of two segments"},
                        "y": {"type": "number", "description": "Corner vertex Y (mm)"},
                        "radius": {"type": "number", "description": "Fillet radius (mm)"}
                    },
                    "required": ["x", "y", "radius"]
                }
            ),
            Tool(
                name="solidworks_sketch_offset",
                description=(
                    "Offset existing sketch geometry by a distance — THE tool for "
                    "hollow profiles, wall thicknesses, and 'offset from outer "
                    "edge' hints. Select any point on the geometry; chain=true "
                    "(default) offsets the whole connected chain. Negative "
                    "distance offsets to the other side."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "x": {"type": "number", "description": "Point on the segment/chain to offset (mm)"},
                        "y": {"type": "number", "description": "Point on the segment/chain to offset (mm)"},
                        "distance": {"type": "number", "description": "Offset distance (mm); negative = other side"},
                        "chain": {"type": "boolean", "description": "Offset the whole connected chain (default true)"}
                    },
                    "required": ["x", "y", "distance"]
                }
            ),
            Tool(
                name="solidworks_sketch_line",
                description="Draw a line segment in the active sketch between two points.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "x1": {
                            "type": "number",
                            "description": "Start point X (mm)"
                        },
                        "y1": {
                            "type": "number",
                            "description": "Start point Y (mm)"
                        },
                        "x2": {
                            "type": "number",
                            "description": "End point X (mm)"
                        },
                        "y2": {
                            "type": "number",
                            "description": "End point Y (mm)"
                        }
                    },
                    "required": ["x1", "y1", "x2", "y2"]
                }
            ),
            Tool(
                name="solidworks_sketch_centerline",
                description="Draw a construction centerline in the active sketch. Centerlines are used as axes for mirror/pattern operations and do not form part of the sketch profile.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "x1": {
                            "type": "number",
                            "description": "Start point X (mm)"
                        },
                        "y1": {
                            "type": "number",
                            "description": "Start point Y (mm)"
                        },
                        "x2": {
                            "type": "number",
                            "description": "End point X (mm)"
                        },
                        "y2": {
                            "type": "number",
                            "description": "End point Y (mm)"
                        }
                    },
                    "required": ["x1", "y1", "x2", "y2"]
                }
            ),
            Tool(
                name="solidworks_sketch_arc",
                description="Draw an arc in the active sketch. Supports 3-point mode (start, end, midpoint on arc) or center-point mode (center, start endpoint, end endpoint).",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "mode": {
                            "type": "string",
                            "enum": ["3point", "center"],
                            "description": "Arc creation mode. '3point': define by start, end, and midpoint on arc. 'center': define by center point and two endpoints. Default: '3point'"
                        },
                        "x1": {
                            "type": "number",
                            "description": "Start point X (mm) - used in both modes"
                        },
                        "y1": {
                            "type": "number",
                            "description": "Start point Y (mm) - used in both modes"
                        },
                        "x2": {
                            "type": "number",
                            "description": "End point X (mm) - used in both modes"
                        },
                        "y2": {
                            "type": "number",
                            "description": "End point Y (mm) - used in both modes"
                        },
                        "x3": {
                            "type": "number",
                            "description": "Third point X (mm) - 3point mode only: midpoint on arc"
                        },
                        "y3": {
                            "type": "number",
                            "description": "Third point Y (mm) - 3point mode only: midpoint on arc"
                        },
                        "centerX": {
                            "type": "number",
                            "description": "Arc center X (mm) - center mode only"
                        },
                        "centerY": {
                            "type": "number",
                            "description": "Arc center Y (mm) - center mode only"
                        }
                    }
                }
            ),
            Tool(
                name="solidworks_sketch_spline",
                description="Draw a spline curve through a series of points in the active sketch.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "points": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "x": {"type": "number", "description": "X coordinate (mm)"},
                                    "y": {"type": "number", "description": "Y coordinate (mm)"}
                                },
                                "required": ["x", "y"]
                            },
                            "minItems": 2,
                            "description": "Array of points the spline passes through (mm)"
                        }
                    },
                    "required": ["points"]
                }
            ),
            Tool(
                name="solidworks_sketch_ellipse",
                description="Draw an ellipse in the active sketch defined by center, semi-major and semi-minor axis lengths, and optional rotation angle.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "centerX": {
                            "type": "number",
                            "description": "Center X position (mm)"
                        },
                        "centerY": {
                            "type": "number",
                            "description": "Center Y position (mm)"
                        },
                        "majorRadius": {
                            "type": "number",
                            "description": "Semi-major axis length (mm)"
                        },
                        "minorRadius": {
                            "type": "number",
                            "description": "Semi-minor axis length (mm)"
                        },
                        "angle": {
                            "type": "number",
                            "description": "Rotation angle of major axis in degrees counterclockwise from X-axis. Default: 0"
                        }
                    },
                    "required": ["centerX", "centerY", "majorRadius", "minorRadius"]
                }
            ),
            Tool(
                name="solidworks_sketch_polygon",
                description="Draw a regular polygon in the active sketch. Supports relative positioning from last shape.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "centerX": {
                            "type": "number",
                            "description": "Absolute center X position (mm)"
                        },
                        "centerY": {
                            "type": "number",
                            "description": "Absolute center Y position (mm)"
                        },
                        "radius": {
                            "type": "number",
                            "description": "Distance from center to vertex (mm)"
                        },
                        "numSides": {
                            "type": "integer",
                            "description": "Number of sides (3-100)",
                            "minimum": 3,
                            "maximum": 100
                        },
                        "inscribed": {
                            "type": "boolean",
                            "description": "If true, radius is measured from the center to the MIDPOINT of each side (inscribed circle / apothem). Default: false = center to a vertex (circumscribed circle)."
                        },
                        "relativeX": {
                            "type": "number",
                            "description": "X offset from last shape (mm)"
                        },
                        "relativeY": {
                            "type": "number",
                            "description": "Y offset from last shape (mm)"
                        },
                        "spacing": {
                            "type": "number",
                            "description": "Spacing from last shape (mm)"
                        }
                    },
                    "required": ["radius", "numSides"]
                }
            ),
            Tool(
                name="solidworks_sketch_slot",
                description="Draw a straight slot shape in the active sketch defined by two center points and a width.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "x1": {
                            "type": "number",
                            "description": "First center point X (mm)"
                        },
                        "y1": {
                            "type": "number",
                            "description": "First center point Y (mm)"
                        },
                        "x2": {
                            "type": "number",
                            "description": "Second center point X (mm)"
                        },
                        "y2": {
                            "type": "number",
                            "description": "Second center point Y (mm)"
                        },
                        "width": {
                            "type": "number",
                            "description": "Total slot width - diameter of end caps (mm)"
                        }
                    },
                    "required": ["x1", "y1", "x2", "y2", "width"]
                }
            ),
            Tool(
                name="solidworks_sketch_point",
                description="Create a sketch point in the active sketch. Useful as a construction reference for dimensions and constraints.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "x": {
                            "type": "number",
                            "description": "X position (mm)"
                        },
                        "y": {
                            "type": "number",
                            "description": "Y position (mm)"
                        }
                    },
                    "required": ["x", "y"]
                }
            ),
            Tool(
                name="solidworks_sketch_text",
                description="Insert sketch text in the active sketch.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "x": {
                            "type": "number",
                            "description": "X position (mm)"
                        },
                        "y": {
                            "type": "number",
                            "description": "Y position (mm)"
                        },
                        "text": {
                            "type": "string",
                            "description": "The text string to insert"
                        },
                        "height": {
                            "type": "number",
                            "description": "Font height (mm)"
                        },
                        "angle": {
                            "type": "number",
                            "description": "Rotation angle in degrees. Default: 0"
                        }
                    },
                    "required": ["x", "y", "text", "height"]
                }
            ),
            Tool(
                name="solidworks_sketch_dimension",
                description="Add a driving dimension to sketch entities — the cornerstone of reusable, modifiable models. Select 1 entity for a single dimension (line length, circle diameter, arc radius), or 2 entities for a between-dimension. The dimension text is placed at (dimX, dimY); pass 'value' to set the driving value immediately. Dimensions are named D1@SketchN, D2@SketchN... in creation order and can be re-driven later with set_parameter (parametric modification). Fully defined sketches (all geometry dimensioned/anchored to the origin) rebuild predictably; get_state reports each sketch's constrainedStatus.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "entityPoints": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "x": {"type": "number", "description": "X coordinate on/near entity (mm)"},
                                    "y": {"type": "number", "description": "Y coordinate on/near entity (mm)"}
                                },
                                "required": ["x", "y"]
                            },
                            "minItems": 1,
                            "maxItems": 2,
                            "description": "Points to select entities to dimension. 1 point for a single entity (line length, arc radius), 2 points for a between-dimension (distance between two entities)."
                        },
                        "entityTypes": {
                            "type": "array",
                            "items": {
                                "type": "string",
                                "enum": ["SKETCHSEGMENT", "SKETCHPOINT", "EXTSKETCHPOINT"]
                            },
                            "description": "Selection type for each entity point. Default: SKETCHSEGMENT."
                        },
                        "dimX": {
                            "type": "number",
                            "description": "X position (mm) for the dimension text placement"
                        },
                        "dimY": {
                            "type": "number",
                            "description": "Y position (mm) for the dimension text placement"
                        },
                        "value": {
                            "type": "number",
                            "description": "Optional driving value to set on the dimension (mm for linear, degrees for angular). If provided, the dimension becomes a driving dimension that constrains the geometry to this value. The dimension type is auto-detected."
                        }
                    },
                    "required": ["entityPoints", "dimX", "dimY"]
                }
            ),
            Tool(
                name="solidworks_set_dimension_value",
                description="Set the value of an existing dimension by selecting it at its text location. Changes the driving value of the dimension, which updates the sketch geometry accordingly. "
                            "KNOWN ISSUE (tested on SOLIDWORKS 2022 SP04, 2026-11-17): the pick by text coordinates depends on the SOLIDWORKS window/view and can fail with 'No dimension found at (x, y)' even when the dimension is there. "
                            "PREFER solidworks_set_parameter with the dimension's name (the 'parameter' returned by sketch_dimension, or from list_parameters, e.g. 'D1@Sketch1') — it is view-independent and passes all tests. Use this tool only when the name is unknown.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "dimX": {
                            "type": "number",
                            "description": "X coordinate (mm) on/near the dimension text to select"
                        },
                        "dimY": {
                            "type": "number",
                            "description": "Y coordinate (mm) on/near the dimension text to select"
                        },
                        "value": {
                            "type": "number",
                            "description": "New dimension value (mm for linear, degrees for angular dimensions)"
                        }
                    },
                    "required": ["dimX", "dimY", "value"]
                }
            ),
            Tool(
                name="solidworks_sketch_constraint",
                description="Add a geometric constraint (relation) between selected sketch entities.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "constraintType": {
                            "type": "string",
                            "enum": ["COINCIDENT", "CONCENTRIC", "TANGENT", "PARALLEL",
                                     "PERPENDICULAR", "HORIZONTAL", "VERTICAL", "EQUAL",
                                     "SYMMETRIC", "MIDPOINT", "COLLINEAR", "CORADIAL"],
                            "description": "Type of geometric constraint to apply"
                        },
                        "entityPoints": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "x": {"type": "number", "description": "X coordinate on/near entity (mm)"},
                                    "y": {"type": "number", "description": "Y coordinate on/near entity (mm)"}
                                },
                                "required": ["x", "y"]
                            },
                            "minItems": 1,
                            "maxItems": 3,
                            "description": "Points to select entities. 1 for single-entity constraints (Horizontal, Vertical), 2 for pair constraints (Parallel, Equal), 3 for Symmetric (entity, entity, centerline)."
                        },
                        "entityTypes": {
                            "type": "array",
                            "items": {
                                "type": "string",
                                "enum": ["SKETCHSEGMENT", "SKETCHPOINT", "EXTSKETCHPOINT"]
                            },
                            "description": "Selection type for each entity point. Default: SKETCHSEGMENT."
                        }
                    },
                    "required": ["constraintType", "entityPoints"]
                }
            ),
            Tool(
                name="solidworks_sketch_toggle_construction",
                description="Toggle the construction geometry flag on a selected sketch entity. Construction geometry is for reference only and does not form part of the extrusion profile.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "x": {
                            "type": "number",
                            "description": "X coordinate on/near entity to toggle (mm)"
                        },
                        "y": {
                            "type": "number",
                            "description": "Y coordinate on/near entity to toggle (mm)"
                        }
                    },
                    "required": ["x", "y"]
                }
            ),
            Tool(
                name="solidworks_get_last_shape_info",
                description="Get information about the last created shape (position, size, bounding box)",
                inputSchema={
                    "type": "object",
                    "properties": {}
                }
            ),
            Tool(
                name="solidworks_exit_sketch",
                description="Exit sketch edit mode",
                inputSchema={
                    "type": "object",
                    "properties": {}
                }
            )
        ]
    
    def execute(self, tool_name: str, args: dict) -> str:
        """Execute a sketching tool"""
        self.connection.ensure_connection()

        dispatch = {
            "solidworks_create_sketch": lambda: self.create_sketch(args),
            "solidworks_sketch_rectangle": lambda: self.sketch_rectangle(args),
            "solidworks_sketch_circle": lambda: self.sketch_circle(args),
            "solidworks_sketch_line": lambda: self.sketch_line(args),
            "solidworks_sketch_profile": lambda: self.sketch_profile(args),
            "solidworks_sketch_fillet": lambda: self.sketch_fillet(args),
            "solidworks_sketch_offset": lambda: self.sketch_offset(args),
            "solidworks_sketch_centerline": lambda: self.sketch_centerline(args),
            "solidworks_sketch_arc": lambda: self.sketch_arc(args),
            "solidworks_sketch_spline": lambda: self.sketch_spline(args),
            "solidworks_sketch_ellipse": lambda: self.sketch_ellipse(args),
            "solidworks_sketch_polygon": lambda: self.sketch_polygon(args),
            "solidworks_sketch_slot": lambda: self.sketch_slot(args),
            "solidworks_sketch_point": lambda: self.sketch_point(args),
            "solidworks_sketch_text": lambda: self.sketch_text(args),
            "solidworks_sketch_dimension": lambda: self.sketch_dimension(args),
            "solidworks_set_dimension_value": lambda: self.set_dimension_value(args),
            "solidworks_sketch_constraint": lambda: self.sketch_constraint(args),
            "solidworks_sketch_toggle_construction": lambda: self.toggle_construction(args),
            "solidworks_get_last_shape_info": lambda: self.get_last_shape_info(),
            "solidworks_exit_sketch": lambda: self.exit_sketch(),
        }

        handler = dispatch.get(tool_name)
        if not handler:
            raise Exception(f"Unknown sketching tool: {tool_name}")
        return handler()
    
    def create_sketch(self, args: dict) -> str:
        """Create sketch on a reference plane or a solid face.

        If faceX/faceY/faceZ are provided, selects the solid face at that point
        (in mm, converted to metres internally) and opens a sketch on it.
        Otherwise, uses the named reference plane ("Front", "Top", or "Right").
        Face-based sketches are required for cut-extrusions to work.
        """
        face_mode = "faceX" in args and "faceY" in args and "faceZ" in args

        # Check for active document
        doc = self.connection.get_active_doc()

        if not doc:
            logger.info("No active document. Creating new part...")
            doc = self.connection.create_new_part()
            self.sketch_counter = 0
            self.created_shapes = []
            self.last_shape = None
            created_new = True
            # Start a fresh state scope for the auto-created part (mirrors
            # modeling.new_part) — without this, the new part's state lands
            # in the PREVIOUS document's scope.
            if self.tracker:
                try:
                    title = doc.GetTitle
                except Exception:
                    title = "Part"
                self.tracker.new_document(title, "part")
        else:
            logger.info("Using existing document")
            # Reset shape tracking for new sketch
            self.created_shapes = []
            self.last_shape = None
            created_new = False

        doc.ClearSelection2(True)

        if face_mode:
            # Select solid face by a point on it (coordinates in mm → metres)
            x_m = args["faceX"] / 1000.0
            y_m = args["faceY"] / 1000.0
            z_m = args["faceZ"] / 1000.0
            import win32com.client as _wc
            import pythoncom as _pc
            callout = _wc.VARIANT(_pc.VT_DISPATCH, None)
            ok = doc.Extension.SelectByID2('', 'FACE', x_m, y_m, z_m, False, 0, callout, 0)
            if ok:
                # SelectByID2 hit-tests along the CURRENT CAMERA direction. A
                # face viewed edge-on can silently hijack the pick meant for
                # its neighbor — verify the selected face actually lies within
                # 1mm of the requested point, else force the fallback.
                try:
                    picked = doc.SelectionManager.GetSelectedObject6(1, -1)
                    cp = picked.GetClosestPointOn(x_m, y_m, z_m)
                    d = ((cp[0] - x_m) ** 2 + (cp[1] - y_m) ** 2
                         + (cp[2] - z_m) ** 2) ** 0.5
                    if d > 1e-3:
                        logger.info(f"SelectByID2 picked a face {d*1000:.1f}mm from "
                                    f"the requested point (edge-on mis-pick); re-picking")
                        doc.ClearSelection2(True)
                        ok = False
                except Exception:
                    pass
            if not ok:
                # Face turned away from (or edge-on to) the camera: select the
                # nearest IFace2 object directly — view-independent.
                face_obj = self._face_object_at_point(doc, x_m, y_m, z_m)
                if face_obj is not None:
                    from .selection_helpers import select_entities_directly
                    ok = select_entities_directly(doc, [face_obj], mark=0) == 1
                    if ok:
                        logger.info("Face picked via view-independent object-selection fallback")
            if not ok:
                raise Exception(
                    f"Could not select face at ({args['faceX']}, {args['faceY']}, "
                    f"{args['faceZ']}) mm — no face within 1mm of that point. "
                    f"Use get_faces or find_face for an on-face samplePoint."
                )
            # Echo back WHICH face got selected (feature, orientation, area) so
            # a mis-pick is visible in this turn, not three features later.
            face_echo = self._describe_selected_face(doc)
            doc.SketchManager.InsertSketch(True)
            # InsertSketch can silently no-op (e.g. non-planar face selected) —
            # without this guard the tool would ✓ and register a stale sketch.
            if doc.SketchManager.ActiveSketch is None:
                raise Exception(
                    f"Face at ({args['faceX']}, {args['faceY']}, {args['faceZ']}) mm "
                    f"was selected but no sketch opened — sketching requires a "
                    f"PLANAR face (selected: {face_echo.get('orientation', 'unknown')})."
                )
            location = f"face at ({args['faceX']}, {args['faceY']}, {args['faceZ']}) mm"
            if face_echo:
                location += (f" — selected face: {face_echo.get('feature', '?')}, "
                             f"{face_echo.get('orientation', '?')}, "
                             f"area {face_echo.get('area_mm2', '?')} mm2")
        else:
            from .selection_helpers import resolve_plane_feature

            plane_name = args.get("plane", "Front")
            if str(plane_name).startswith("ref:"):
                # accept the ID that ref_plane returns (e.g. "ref:Plane1"),
                # as the tool description promises
                from .com_utils import resolve_tracked_name
                plane_name = resolve_tracked_name(self.tracker, plane_name)                     if self.tracker else plane_name[len("ref:"):]
            plane_feature = resolve_plane_feature(doc, plane_name)
            if not plane_feature:
                raise Exception(
                    f"Could not find plane: {plane_name}. "
                    f"Use 'Front', 'Top', 'Right', or a custom reference plane name like 'Plane1'."
                )
            plane_feature.Select2(False, 0)
            doc.SketchManager.InsertSketch(True)
            location = f"{plane_name} plane"

        # Track sketch — read the REAL name back from the feature tree.
        # Guessing Sketch{counter} desyncs after activating a document with
        # existing sketches, and create_extrusion then selects the wrong
        # sketch BY THE GUESSED NAME with a ✓.
        self.sketch_counter += 1
        actual = self._read_back_sketch_name(doc)
        self.current_sketch_name = actual or f"Sketch{self.sketch_counter}"

        sketch_id = ""
        if self.tracker:
            sketch_id = self.tracker.register_sketch(self.current_sketch_name, location)
            self._sync_spatial()

        logger.info(f"Sketch created: {self.current_sketch_name} on {location}")

        if created_new:
            msg = f"✓ New part created. Sketch '{self.current_sketch_name}' on {location}"
        else:
            msg = f"✓ Sketch '{self.current_sketch_name}' created on {location}"

        # Report the sketch's coordinate frame in model space. Face sketches can
        # have mirrored/rotated axes (e.g. on a back-facing face, sketch +X is
        # model -X) — without this agents place geometry off the body.
        frame = self._sketch_frame(doc)
        extra = {}
        if frame:
            extra["sketchFrame"] = frame
            axis_note = self._frame_note(frame)
            if axis_note:
                msg += f" ({axis_note})"
            # Face sketches: also report the face center in SKETCH coordinates.
            # The sketch origin is the model origin projected onto the face —
            # not the face center — so without this agents place geometry a
            # face-half-width off the part.
            center = (face_echo or {}).get("centerModel_mm") if face_mode else None
            if center:
                o = frame["originModel_mm"]
                d = [center[i] - o[i] for i in range(3)]
                sx = sum(frame["xAxisModel"][i] * d[i] for i in range(3))
                sy = sum(frame["yAxisModel"][i] * d[i] for i in range(3))
                extra["faceCenter"] = {"model_mm": center,
                                       "sketch_mm": [round(sx, 3), round(sy, 3)]}
                msg += f" — face center is at sketch ({round(sx, 1)}, {round(sy, 1)})"

        return self._json_result(msg, id=sketch_id, type="sketch", **extra)

    def _face_object_at_point(self, doc, x_m, y_m, z_m, tol_m=1e-3):
        """Nearest IFace2 to a model-space point (metres), or None if no face
        lies within tol_m. View-independent — pure geometry, no camera."""
        from .selection_helpers import _nearest_entity
        return _nearest_entity(doc, "face", x_m, y_m, z_m, tol_m=tol_m)

    def _describe_selected_face(self, doc) -> dict:
        """Best-effort description of the currently selected face: creating
        feature, orientation (normal or 'curved'), area. Empty dict on any
        failure — never blocks sketch creation."""
        try:
            import types as _types
            sel_mgr = doc.SelectionManager
            face = sel_mgr.GetSelectedObject6(1, -1)
            if face is None:
                return {}
            out = {}
            try:
                feat = face.GetFeature
                if isinstance(feat, _types.MethodType):
                    feat = feat()
                if feat is not None:
                    name = feat.Name
                    out["feature"] = name() if isinstance(name, _types.MethodType) else name
            except Exception:
                pass
            try:
                surface = face.GetSurface
                if surface.Identity == 4001:
                    # PlaneParams is the SURFACE normal — flip by
                    # FaceInSurfaceSense to report the OUTWARD face normal
                    # (same correction as geometry_query._face_normal;
                    # without it opposite faces echo identical normals).
                    p = surface.PlaneParams
                    nx, ny, nz = p[0], p[1], p[2]
                    try:
                        fis = face.FaceInSurfaceSense
                        if isinstance(fis, _types.MethodType):
                            fis = fis()
                        if fis:
                            nx, ny, nz = -nx, -ny, -nz
                    except Exception:
                        pass
                    out["orientation"] = (f"normal ({round(nx, 2)}, "
                                          f"{round(ny, 2)}, {round(nz, 2)})")
                else:
                    out["orientation"] = "curved"
            except Exception:
                pass
            try:
                out["area_mm2"] = round(face.GetArea * 1e6, 1)
            except Exception:
                pass
            try:
                # Face bounding-box center (mm, model space). For planar faces
                # this is the natural layout datum — the sketch ORIGIN is the
                # projection of the MODEL origin onto the face plane, which is
                # usually NOT the face center. Caveat: bbox center equals the
                # true centroid only for symmetric faces (drifts on L-shaped
                # or notched faces).
                box = face.GetBox
                if isinstance(box, _types.MethodType):
                    box = box()
                if box and len(box) >= 6:
                    out["centerModel_mm"] = [
                        round((box[0] + box[3]) / 2 * 1000, 3),
                        round((box[1] + box[4]) / 2 * 1000, 3),
                        round((box[2] + box[5]) / 2 * 1000, 3),
                    ]
            except Exception:
                pass
            return out
        except Exception:
            return {}

    def _read_back_sketch_name(self, doc):
        """Actual name of the just-entered sketch: newest ProfileFeature in
        the tree (same authority exit_sketch already uses). None on failure."""
        try:
            return newest_sketch_name(doc)
        except Exception as e:
            logger.warning(f"Sketch name read-back failed: {e}")
        return None

    def _sketch_frame(self, doc) -> dict:
        """Sketch origin and axis directions in model space (mm). Best-effort."""
        try:
            sketch = doc.SketchManager.ActiveSketch
            if not sketch:
                return {}
            # ModelToSketchTransform maps model -> sketch; its rotation is
            # orthonormal, so sketch axes in model space are the matrix COLUMNS
            # (ArrayData rotation layout: rows 0-2, 3-5, 6-8; translation 9-11).
            arr = sketch.ModelToSketchTransform.ArrayData
            x_axis = [round(arr[0], 6), round(arr[3], 6), round(arr[6], 6)]
            y_axis = [round(arr[1], 6), round(arr[4], 6), round(arr[7], 6)]
            # Sketch origin in model space: invert model->sketch on (0,0,0):
            # origin = -R^T*t. ArrayData stores the rotation column-major, so the
            # stored triples arr[0:3]/arr[3:6]/arr[6:9] ARE the rows of R^T.
            # (Validated live on front/back/top/right faces.)
            t = arr[9:12]
            origin = [round(-(arr[0]*t[0] + arr[1]*t[1] + arr[2]*t[2]) * 1000, 3),
                      round(-(arr[3]*t[0] + arr[4]*t[1] + arr[5]*t[2]) * 1000, 3),
                      round(-(arr[6]*t[0] + arr[7]*t[1] + arr[8]*t[2]) * 1000, 3)]
            return {"originModel_mm": origin, "xAxisModel": x_axis, "yAxisModel": y_axis}
        except Exception as e:
            logger.debug(f"Could not compute sketch frame: {e}")
            return {}

    @staticmethod
    def _frame_note(frame) -> str:
        """Human-readable axis mapping like 'sketch X = model -X, sketch Y = model +Y'."""
        def axis_name(vec):
            for i, name in enumerate(("X", "Y", "Z")):
                if abs(vec[i]) > 0.999:
                    return ("+" if vec[i] > 0 else "-") + name
            return None
        x = axis_name(frame.get("xAxisModel", []))
        y = axis_name(frame.get("yAxisModel", []))
        if x and y:
            return f"sketch X = model {x}, sketch Y = model {y}"
        return ""
    
    def _calculate_position(self, args: dict, shape_width: float, shape_height: float) -> tuple:
        """Calculate position based on absolute or relative coordinates"""
        last = self.tracker.last_shape if self.tracker else self.last_shape

        # Priority 1: Explicit absolute position
        if "centerX" in args and "centerY" in args:
            return args["centerX"], args["centerY"]

        # Priority 2: Spacing from last shape (horizontal)
        if "spacing" in args and last:
            spacing = args["spacing"]
            last_right = last["right"]
            center_x = last_right + spacing + (shape_width / 2)
            center_y = last["centerY"]
            return center_x, center_y

        # Priority 3: Relative offset from last shape
        if ("relativeX" in args or "relativeY" in args) and last:
            offset_x = args.get("relativeX", 0)
            offset_y = args.get("relativeY", 0)
            center_x = last["centerX"] + offset_x
            center_y = last["centerY"] + offset_y
            return center_x, center_y

        # Default: Origin
        return 0, 0

    def _require_active_sketch(self, doc):
        """Raise unless a sketch is currently being edited.

        Drawing calls issued outside sketch-edit mode fail silently (COM
        returns None) — previously the tool still returned ✓ and registered
        a phantom entity, poisoning last_shape.
        """
        try:
            sketch = doc.SketchManager.ActiveSketch
        except Exception:
            sketch = None
        if sketch is None:
            raise Exception(
                "No active sketch — call create_sketch first "
                "(or re-enter the sketch if it was already exited)"
            )
        return sketch

    @staticmethod
    def _check_drawn(obj, what: str):
        """Raise if a SketchManager draw call returned None (draw failed)."""
        if obj is None:
            raise Exception(
                f"SolidWorks failed to draw {what} (COM call returned None). "
                f"Nothing was added to the sketch — check the coordinates/"
                f"parameters and that the active sketch is valid."
            )
        return obj

    @staticmethod
    def _segment_count(doc) -> int:
        try:
            segs = com_prop(doc.SketchManager.ActiveSketch, "GetSketchSegments")
        except Exception:
            return -1
        return len(segs) if segs else 0

    def _delete_drawn(self, doc, obj):
        """Delete the segments of one Create* result from the active sketch."""
        callout = self._null_callout()
        doc.ClearSelection2(True)
        for seg in _segment_list(obj):
            seg.Select4(True, callout)
        doc.EditDelete()
        doc.ClearSelection2(True)

    def _draw_checked(self, doc, what, create, check):
        """Draw, read the geometry back, and redraw with snapping off if the
        sketch did not get what was asked for.

        create(sm) performs the Create* call on the given SketchManager proxy;
        check(obj) returns a list of mismatch descriptions (empty = exact).
        Snapping silently moves/resizes entities that land near existing
        geometry at a zoomed-out view, and the call still "succeeds" -- so the
        result is verified instead of trusted. Raises (with nothing left in
        the sketch) if even the snapping-off redraw is wrong.
        """
        before = self._segment_count(doc)
        obj = self._check_drawn(create(_direct_retry(doc)), what)
        problems = check(obj)
        if not problems:
            return obj
        logger.warning(f"{what}: drawn geometry differs from the request "
                       f"({'; '.join(problems)}) -- redrawing with snapping off")
        self._delete_drawn(doc, obj)
        if before >= 0 and self._segment_count(doc) != before:
            raise Exception(f"{what}: snapping moved the entity and it could not be "
                            f"removed cleanly ({'; '.join(problems)}). Check the sketch.")
        with sketch_snapping_off(self.connection.app):
            obj = self._check_drawn(create(_direct_retry(doc)), what)
        problems = check(obj)
        if problems:
            self._delete_drawn(doc, obj)
            raise Exception(f"SolidWorks drew {what} at the wrong size/position even "
                            f"with snapping off ({'; '.join(problems)}). Nothing was "
                            f"added to the sketch.")
        logger.info(f"{what}: redrawn exactly with snapping off")
        return obj

    @staticmethod
    def _check_circle(obj, cx, cy, r):
        """Mismatches of a drawn circle vs centre (cx, cy) / radius r (metres)."""
        out = []
        got_r = float(com_prop(obj, "GetRadius"))
        gx, gy = _xy(com_prop(obj, "GetCenterPoint2"))
        if abs(got_r - r) > _DRAW_TOL_M:
            out.append(f"radius {got_r * 1000:.4f} mm, wanted {r * 1000:.4f}")
        if abs(gx - cx) > _DRAW_TOL_M or abs(gy - cy) > _DRAW_TOL_M:
            out.append(f"centre ({gx * 1000:.4f}, {gy * 1000:.4f}) mm, "
                       f"wanted ({cx * 1000:.4f}, {cy * 1000:.4f})")
        return out

    @staticmethod
    def _check_rectangle(obj, x1, y1, x2, y2):
        """Mismatches of a drawn corner rectangle vs its corners (metres)."""
        segs = _segment_list(obj)
        pts = []
        for seg in segs:
            pts.append(_xy(com_prop(seg, "GetStartPoint2")))
            pts.append(_xy(com_prop(seg, "GetEndPoint2")))
        if len(segs) != 4:
            return [f"{len(segs)} edges instead of 4"]
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        got = (min(xs), min(ys), max(xs), max(ys))
        want = (min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2))
        if any(abs(g - w) > _DRAW_TOL_M for g, w in zip(got, want)):
            return ["corners ({:.4f}, {:.4f})-({:.4f}, {:.4f}) mm, wanted "
                    "({:.4f}, {:.4f})-({:.4f}, {:.4f})".format(
                        *(v * 1000 for v in got + want))]
        return []

    @staticmethod
    def _check_line(obj, x1, y1, x2, y2):
        """Mismatches of a drawn line vs its endpoints (metres), either direction."""
        a = _xy(com_prop(obj, "GetStartPoint2"))
        b = _xy(com_prop(obj, "GetEndPoint2"))
        close = lambda p, q: abs(p[0] - q[0]) <= _DRAW_TOL_M and abs(p[1] - q[1]) <= _DRAW_TOL_M
        if (close(a, (x1, y1)) and close(b, (x2, y2))) or (close(a, (x2, y2)) and close(b, (x1, y1))):
            return []
        return [f"endpoints ({a[0] * 1000:.4f}, {a[1] * 1000:.4f})-({b[0] * 1000:.4f}, "
                f"{b[1] * 1000:.4f}) mm, wanted ({x1 * 1000:.4f}, {y1 * 1000:.4f})-"
                f"({x2 * 1000:.4f}, {y2 * 1000:.4f})"]

    @staticmethod
    def _check_point(obj, x, y):
        got = _xy(obj)
        return [] if _near(got, (x, y)) else [f"point {_fmt_mm(got)} mm, wanted {_fmt_mm((x, y))}"]

    @staticmethod
    def _check_arc_ends(obj, p, q):
        """Start/end of an arc vs p, q (either order)."""
        a = _xy(com_prop(obj, "GetStartPoint2"))
        b = _xy(com_prop(obj, "GetEndPoint2"))
        if (_near(a, p) and _near(b, q)) or (_near(a, q) and _near(b, p)):
            return []
        return [f"arc ends {_fmt_mm(a)}-{_fmt_mm(b)} mm, wanted {_fmt_mm(p)}-{_fmt_mm(q)}"]

    @classmethod
    def _check_arc_3pt(cls, obj, p1, p2, p3):
        out = cls._check_arc_ends(obj, p1, p2)
        c = _xy(com_prop(obj, "GetCenterPoint2"))
        r = float(com_prop(obj, "GetRadius"))
        d = math.hypot(p3[0] - c[0], p3[1] - c[1])
        if abs(d - r) > _DRAW_TOL_M:
            out.append(f"arc (centre {_fmt_mm(c)}, radius {r * 1000:.4f} mm) does not pass "
                       f"through {_fmt_mm(p3)}")
        return out

    @classmethod
    def _check_arc_center(cls, obj, c, p1, p2):
        out = []
        got_c = _xy(com_prop(obj, "GetCenterPoint2"))
        if not _near(got_c, c):
            out.append(f"centre {_fmt_mm(got_c)} mm, wanted {_fmt_mm(c)}")
        r = math.hypot(p1[0] - c[0], p1[1] - c[1])
        got_r = float(com_prop(obj, "GetRadius"))
        if abs(got_r - r) > _DRAW_TOL_M:
            out.append(f"radius {got_r * 1000:.4f} mm, wanted {r * 1000:.4f}")
        d = math.hypot(p2[0] - c[0], p2[1] - c[1]) or 1.0
        end = (c[0] + r * (p2[0] - c[0]) / d, c[1] + r * (p2[1] - c[1]) / d)
        return out + cls._check_arc_ends(obj, p1, end)

    @staticmethod
    def _check_ellipse(obj, c, major_r, minor_r, angle_rad):
        out = []
        got_c = _xy(com_prop(obj, "GetCenterPoint2"))
        maj = _xy(com_prop(obj, "GetMajorPoint2"))
        mnr = _xy(com_prop(obj, "GetMinorPoint2"))
        if not _near(got_c, c):
            out.append(f"centre {_fmt_mm(got_c)} mm, wanted {_fmt_mm(c)}")
        vx, vy = maj[0] - got_c[0], maj[1] - got_c[1]
        if abs(math.hypot(vx, vy) - major_r) > _DRAW_TOL_M:
            out.append(f"major radius {math.hypot(vx, vy) * 1000:.4f} mm, wanted {major_r * 1000:.4f}")
        if abs(vx * math.sin(angle_rad) - vy * math.cos(angle_rad)) > _DRAW_TOL_M:
            out.append(f"major axis towards {_fmt_mm(maj)} mm is not at the requested angle")
        rm = math.hypot(mnr[0] - got_c[0], mnr[1] - got_c[1])
        if abs(rm - minor_r) > _DRAW_TOL_M:
            out.append(f"minor radius {rm * 1000:.4f} mm, wanted {minor_r * 1000:.4f}")
        return out

    @staticmethod
    def _check_spline(obj, pts):
        got = _spline_points(obj)
        if len(got) != len(pts):
            return [f"{len(got)} through-points, wanted {len(pts)}"]
        bad = [f"{_fmt_mm(g)} instead of {_fmt_mm(w)}" for g, w in zip(got, pts) if not _near(g, w)]
        return [f"spline points {'; '.join(bad)} mm"] if bad else []

    @staticmethod
    def _check_polygon(obj, cx, cy, vertex_r, n):
        """Regular polygon: n sides, every vertex vertex_r from (cx, cy), one
        vertex straight to the right of the centre (how it is drawn)."""
        lines = [s for s in _segment_list(obj)
                 if com_prop(s, "GetType") == 0 and not com_prop(s, "ConstructionGeometry")]
        if len(lines) != n:
            return [f"{len(lines)} sides, wanted {n}"]
        verts = [_xy(com_prop(s, p)) for s in lines for p in ("GetStartPoint2", "GetEndPoint2")]
        off = [v for v in verts if abs(math.hypot(v[0] - cx, v[1] - cy) - vertex_r) > _DRAW_TOL_M]
        out = []
        if off:
            out.append(f"vertex {_fmt_mm(off[0])} mm is not {vertex_r * 1000:.4f} mm from the "
                       f"centre {_fmt_mm((cx, cy))}")
        if not any(_near(v, (cx + vertex_r, cy)) for v in verts):
            out.append(f"no vertex at {_fmt_mm((cx + vertex_r, cy))} mm")
        return out

    @staticmethod
    def _check_slot(segs, p1, p2, width):
        """Straight slot: two end arcs of radius width/2 centred on p1, p2 and
        two side lines whose ends lie on those arcs."""
        arcs = [s for s in segs if com_prop(s, "GetType") == 1 and not com_prop(s, "ConstructionGeometry")]
        lines = [s for s in segs if com_prop(s, "GetType") == 0 and not com_prop(s, "ConstructionGeometry")]
        if len(arcs) != 2 or len(lines) != 2:
            return [f"{len(arcs)} end arcs and {len(lines)} side lines found, wanted 2 and 2"]
        r = width / 2
        out = []
        centres = [_xy(com_prop(a, "GetCenterPoint2")) for a in arcs]
        if not ((_near(centres[0], p1) and _near(centres[1], p2)) or
                (_near(centres[0], p2) and _near(centres[1], p1))):
            out.append(f"end-arc centres {_fmt_mm(centres[0])}, {_fmt_mm(centres[1])} mm, "
                       f"wanted {_fmt_mm(p1)}, {_fmt_mm(p2)}")
        for a in arcs:
            got_r = float(com_prop(a, "GetRadius"))
            if abs(got_r - r) > _DRAW_TOL_M:
                out.append(f"end-arc radius {got_r * 1000:.4f} mm, wanted {r * 1000:.4f}")
                break
        for ln in lines:
            for p in (_xy(com_prop(ln, "GetStartPoint2")), _xy(com_prop(ln, "GetEndPoint2"))):
                if min(abs(math.hypot(p[0] - c[0], p[1] - c[1]) - r) for c in (p1, p2)) > _DRAW_TOL_M:
                    out.append(f"side-line end {_fmt_mm(p)} mm is off the slot outline")
                    return out
        return out

    def _new_segments(self, doc, call):
        """Run call() and return the sketch segments it added (by geometry
        signature), or None if the call returned None."""
        from collections import Counter
        sk = doc.SketchManager.ActiveSketch
        before = Counter(_seg_sig(s) for s in (com_prop(sk, "GetSketchSegments") or []))
        if call() is None:
            return None
        new = []
        for s in (com_prop(sk, "GetSketchSegments") or []):
            k = _seg_sig(s)
            if before[k] > 0:
                before[k] -= 1
            else:
                new.append(s)
        return new

    def sketch_rectangle(self, args: dict) -> str:
        """Draw rectangle with spatial awareness"""
        doc = self.connection.get_active_doc()
        if not doc:
            raise Exception("No active document")
        self._require_active_sketch(doc)

        # Determine dimensions
        corner_center = None
        if "width" in args and "height" in args:
            width = args["width"]
            height = args["height"]
        elif "x1" in args and "y1" in args and "x2" in args and "y2" in args:
            width = abs(args["x2"] - args["x1"])
            height = abs(args["y2"] - args["y1"])
            # Corner form specifies the position too — don't let the spatial
            # positioning default drag the rectangle back to the origin.
            corner_center = ((args["x1"] + args["x2"]) / 2,
                             (args["y1"] + args["y2"]) / 2)
        else:
            raise Exception("Must provide either (width, height) or (x1, y1, x2, y2)")

        # Calculate position (with spatial awareness); explicit centerX/centerY
        # still wins over the corner-derived center for back-compat
        if corner_center and "centerX" not in args and "centerY" not in args:
            center_x, center_y = corner_center
        else:
            center_x, center_y = self._calculate_position(args, width, height)
        
        # Convert to meters and calculate corners
        center_x_m = center_x / 1000.0
        center_y_m = center_y / 1000.0
        width_m = width / 1000.0
        height_m = height / 1000.0
        
        x1 = center_x_m - width_m / 2
        y1 = center_y_m - height_m / 2
        x2 = center_x_m + width_m / 2
        y2 = center_y_m + height_m / 2
        
        # Draw rectangle
        self._draw_checked(
            doc, f"rectangle {width}mm x {height}mm",
            lambda sm: sm.CreateCornerRectangle(x1, y1, 0.0, x2, y2, 0.0),
            lambda obj: self._check_rectangle(obj, x1, y1, x2, y2))

        # Track this shape
        shape_info = {
            "type": "rectangle",
            "width": width,
            "height": height,
            "centerX": center_x,
            "centerY": center_y,
            "left": center_x - width / 2,
            "right": center_x + width / 2,
            "bottom": center_y - height / 2,
            "top": center_y + height / 2
        }
        coordinates = {
            "x1": center_x - width / 2, "y1": center_y - height / 2,
            "x2": center_x + width / 2, "y2": center_y + height / 2,
        }

        entity_id = ""
        if self.tracker:
            entity_id = self.tracker.register_entity("rect", coordinates, shape_info)
            self._sync_spatial()
        else:
            self.created_shapes.append(shape_info)
            self.last_shape = shape_info

        logger.info(f"Rectangle: {width}mm x {height}mm at ({center_x:.1f}, {center_y:.1f})")
        msg = f"✓ Rectangle {width}mm x {height}mm at position ({center_x:.1f}, {center_y:.1f})"
        sketch_id = f"sketch:{self.current_sketch_name}" if self.current_sketch_name else ""
        return self._json_result(msg, id=entity_id, type="rectangle", sketchId=sketch_id)
    
    def sketch_circle(self, args: dict) -> str:
        """Draw circle with spatial awareness"""
        doc = self.connection.get_active_doc()
        if not doc:
            raise Exception("No active document")
        self._require_active_sketch(doc)

        radius = args["radius"]
        
        # Calculate position (with spatial awareness)
        center_x, center_y = self._calculate_position(args, radius * 2, radius * 2)
        
        # Convert to meters
        cx_m = center_x / 1000.0
        cy_m = center_y / 1000.0
        r_m = radius / 1000.0
        
        # Draw circle
        self._draw_checked(
            doc, f"circle radius {radius}mm",
            lambda sm: sm.CreateCircleByRadius(cx_m, cy_m, 0.0, r_m),
            lambda obj: self._check_circle(obj, cx_m, cy_m, r_m))
        
        # Track this shape
        shape_info = {
            "type": "circle",
            "radius": radius,
            "centerX": center_x,
            "centerY": center_y,
            "left": center_x - radius,
            "right": center_x + radius,
            "bottom": center_y - radius,
            "top": center_y + radius
        }
        coordinates = {"centerX": center_x, "centerY": center_y, "radius": radius}

        entity_id = ""
        if self.tracker:
            entity_id = self.tracker.register_entity("circle", coordinates, shape_info)
            self._sync_spatial()
        else:
            self.created_shapes.append(shape_info)
            self.last_shape = shape_info

        logger.info(f"Circle: radius {radius}mm at ({center_x:.1f}, {center_y:.1f})")
        msg = f"✓ Circle radius {radius}mm at position ({center_x:.1f}, {center_y:.1f})"
        sketch_id = f"sketch:{self.current_sketch_name}" if self.current_sketch_name else ""
        return self._json_result(msg, id=entity_id, type="circle", sketchId=sketch_id)

    def sketch_line(self, args: dict) -> str:
        """Draw a line segment in the active sketch"""
        doc = self.connection.get_active_doc()
        if not doc:
            raise Exception("No active document")
        self._require_active_sketch(doc)

        for key in ["x1", "y1", "x2", "y2"]:
            if key not in args:
                raise Exception(f"sketch_line requires {key}")

        x1, y1 = args["x1"], args["y1"]
        x2, y2 = args["x2"], args["y2"]

        m = (x1 / 1000.0, y1 / 1000.0, x2 / 1000.0, y2 / 1000.0)
        self._draw_checked(
            doc, f"line ({x1}, {y1}) -> ({x2}, {y2})",
            lambda sm: sm.CreateLine(m[0], m[1], 0.0, m[2], m[3], 0.0),
            lambda obj: self._check_line(obj, *m))

        shape_info = {
            "type": "line",
            "centerX": (x1 + x2) / 2,
            "centerY": (y1 + y2) / 2,
            "left": min(x1, x2),
            "right": max(x1, x2),
            "bottom": min(y1, y2),
            "top": max(y1, y2),
            "width": abs(x2 - x1),
            "height": abs(y2 - y1),
            "x1": x1, "y1": y1, "x2": x2, "y2": y2
        }
        coordinates = {"x1": x1, "y1": y1, "x2": x2, "y2": y2}

        entity_id = ""
        if self.tracker:
            entity_id = self.tracker.register_entity("line", coordinates, shape_info)
            self._sync_spatial()
        else:
            self.created_shapes.append(shape_info)
            self.last_shape = shape_info

        logger.info(f"Line: ({x1}, {y1}) to ({x2}, {y2}) mm")
        msg = f"✓ Line from ({x1:.1f}, {y1:.1f}) to ({x2:.1f}, {y2:.1f}) mm"
        sketch_id = f"sketch:{self.current_sketch_name}" if self.current_sketch_name else ""
        return self._json_result(msg, id=entity_id, type="line", sketchId=sketch_id)

    @staticmethod
    def _null_callout():
        import pythoncom
        import win32com.client
        return win32com.client.VARIANT(pythoncom.VT_DISPATCH, None)

    def sketch_profile(self, args: dict) -> str:
        """Draw a chained profile (lines / tangent arcs / center arcs) plus
        optional corner fillets in ONE call. Composite tool: collapses the
        10-20 agent turns a complex profile costs into a single turn."""
        doc = self.connection.get_active_doc()
        if not doc:
            raise Exception("No active document")
        self._require_active_sketch(doc)
        segments = args.get("segments") or []
        if "startX" not in args or "startY" not in args:
            raise Exception("sketch_profile requires startX/startY")
        if not segments:
            raise Exception("sketch_profile requires a non-empty segments list")

        sm = doc.SketchManager
        prev_add_to_db = None
        try:
            prev_add_to_db = sm.AddToDB
            sm.AddToDB = True  # suppress inference snapping during the batch
        except Exception:
            pass

        start = (float(args["startX"]), float(args["startY"]))
        cur = start
        drawn = []
        drawn_objs = []   # (segment, type, from_mm, to_mm) for the read-back check
        xs, ys = [start[0]], [start[1]]
        try:
            for i, seg in enumerate(segments):
                stype = (seg.get("type") or "LINE").upper()
                if "x" not in seg or "y" not in seg:
                    raise Exception(f"segment {i} missing x/y")
                end = (float(seg["x"]), float(seg["y"]))
                if stype == "LINE":
                    obj = sm.CreateLine(cur[0] / 1000.0, cur[1] / 1000.0, 0.0,
                                        end[0] / 1000.0, end[1] / 1000.0, 0.0)
                elif stype == "TANGENT_ARC":
                    # swTangentArcTypes: 1=Forward (tangent continuation)
                    obj = sm.CreateTangentArc(
                        cur[0] / 1000.0, cur[1] / 1000.0, 0.0,
                        end[0] / 1000.0, end[1] / 1000.0, 0.0,
                        int(seg.get("tangentType", 1)))
                elif stype == "ARC":
                    if "cx" not in seg or "cy" not in seg:
                        raise Exception(f"ARC segment {i} requires cx/cy")
                    obj = sm.CreateArc(
                        float(seg["cx"]) / 1000.0, float(seg["cy"]) / 1000.0, 0.0,
                        cur[0] / 1000.0, cur[1] / 1000.0, 0.0,
                        end[0] / 1000.0, end[1] / 1000.0, 0.0,
                        int(seg.get("direction", 1)))
                else:
                    raise Exception(f"segment {i}: unknown type {stype!r} "
                                    "(LINE | TANGENT_ARC | ARC)")
                if obj is None:
                    raise Exception(f"segment {i} ({stype} to {end}) failed — "
                                    f"profile aborted after {len(drawn)} segments")
                drawn.append(stype)
                drawn_objs.append((obj, stype, cur, end))
                xs.append(end[0]); ys.append(end[1])
                cur = end

            closed = False
            if args.get("close", True) and (abs(cur[0] - start[0]) > 1e-9
                                            or abs(cur[1] - start[1]) > 1e-9):
                obj = sm.CreateLine(cur[0] / 1000.0, cur[1] / 1000.0, 0.0,
                                    start[0] / 1000.0, start[1] / 1000.0, 0.0)
                if obj is None:
                    raise Exception("closing line failed")
                drawn.append("LINE(close)")
                drawn_objs.append((obj, "LINE", cur, start))
                closed = True

            # Read every segment back (snapping can move an endpoint onto
            # nearby geometry while the call still succeeds). No redraw with
            # snapping off here: snapping is what joins the chained segments,
            # so the whole profile is removed and the call fails instead.
            problems = []
            for k, (o, st, a, b) in enumerate(drawn_objs):
                a_m, b_m = (a[0] / 1000.0, a[1] / 1000.0), (b[0] / 1000.0, b[1] / 1000.0)
                p = (self._check_line(o, a_m[0], a_m[1], b_m[0], b_m[1]) if st == "LINE"
                     else self._check_arc_ends(o, a_m, b_m))
                problems += [f"segment {k} ({st}): {x}" for x in p]
            if problems:
                self._delete_drawn(doc, [o for o, *_ in drawn_objs])
                raise Exception(
                    "Profile not drawn as requested -- SolidWorks snapped it onto nearby "
                    f"geometry ({'; '.join(problems)}). The profile was removed. Draw it "
                    "with separate sketch_line/sketch_arc calls (each is checked and "
                    "redrawn exactly), or zoom in on the area first.")

            fillet_notes = []
            for f in (args.get("cornerFillets") or []):
                fx, fy, fr = float(f["x"]), float(f["y"]), float(f["radius"])
                doc.ClearSelection2(True)
                ok = doc.Extension.SelectByID2(
                    "", "SKETCHPOINT", fx / 1000.0, fy / 1000.0, 0.0,
                    False, 0, self._null_callout(), 0)
                seg_obj = None
                if ok:
                    # swSketchFilletType: 1 = constrained corners kept
                    seg_obj = sm.CreateFillet(fr / 1000.0, 1)
                fillet_notes.append(
                    f"R{fr}@({fx},{fy}): {'OK' if seg_obj is not None else 'FAILED'}")
                doc.ClearSelection2(True)
        finally:
            if prev_add_to_db is not None:
                try:
                    sm.AddToDB = prev_add_to_db
                except Exception:
                    pass

        shape_info = {
            "type": "profile",
            "centerX": (min(xs) + max(xs)) / 2, "centerY": (min(ys) + max(ys)) / 2,
            "left": min(xs), "right": max(xs),
            "bottom": min(ys), "top": max(ys),
            "width": max(xs) - min(xs), "height": max(ys) - min(ys),
        }
        entity_id = ""
        if self.tracker:
            entity_id = self.tracker.register_entity(
                "profile", {"segments": len(drawn), "start": list(start)}, shape_info)
            self._sync_spatial()
        else:
            self.created_shapes.append(shape_info)
            self.last_shape = shape_info

        logger.info(f"Profile: {len(drawn)} segments, closed={closed}, "
                    f"fillets={args.get('cornerFillets') and len(args['cornerFillets']) or 0}")
        msg = (f"✓ Profile: {len(drawn)} segments drawn"
               + (", closed" if closed else "")
               + (f"; fillets: {'; '.join(fillet_notes)}" if fillet_notes else ""))
        sketch_id = f"sketch:{self.current_sketch_name}" if self.current_sketch_name else ""
        return self._json_result(msg, id=entity_id, type="profile", sketchId=sketch_id)

    def sketch_fillet(self, args: dict) -> str:
        """Round a corner of existing sketch geometry with exact tangency —
        SolidWorks computes the arc; no coordinate math needed."""
        doc = self.connection.get_active_doc()
        if not doc:
            raise Exception("No active document")
        for key in ("x", "y", "radius"):
            if key not in args:
                raise Exception(f"sketch_fillet requires {key}")
        fx, fy, fr = float(args["x"]), float(args["y"]), float(args["radius"])

        doc.ClearSelection2(True)
        ok = doc.Extension.SelectByID2(
            "", "SKETCHPOINT", fx / 1000.0, fy / 1000.0, 0.0,
            False, 0, self._null_callout(), 0)
        seg = None
        if ok:
            # swSketchFilletType 1 = keep constrained corners
            seg = doc.SketchManager.CreateFillet(fr / 1000.0, 1)
        doc.ClearSelection2(True)
        if seg is None:
            raise Exception(
                f"sketch fillet R{fr} failed at ({fx}, {fy}) — the point must "
                f"be the CORNER VERTEX where two sketch segments meet (their "
                f"shared endpoint), not a point on a segment")
        logger.info(f"Sketch fillet R{fr} at ({fx}, {fy})")
        sketch_id = f"sketch:{self.current_sketch_name}" if self.current_sketch_name else ""
        return self._json_result(
            f"✓ Sketch fillet R{fr}mm at corner ({fx}, {fy})",
            type="sketch_fillet", sketchId=sketch_id)

    def sketch_offset(self, args: dict) -> str:
        """Offset existing sketch geometry by a distance (SketchOffsetEntities2)
        — the standard way to make hollow/wall profiles (e.g. 'offset from
        outer edge') without recomputing every segment."""
        doc = self.connection.get_active_doc()
        if not doc:
            raise Exception("No active document")
        if "distance" not in args or "x" not in args or "y" not in args:
            raise Exception("sketch_offset requires x, y (a point on the "
                            "geometry to offset) and distance (mm; negative "
                            "offsets to the other side)")
        px, py = float(args["x"]), float(args["y"])
        dist = float(args["distance"]) / 1000.0

        doc.ClearSelection2(True)
        ok = doc.Extension.SelectByID2(
            "", "SKETCHSEGMENT", px / 1000.0, py / 1000.0, 0.0,
            False, 0, self._null_callout(), 0)
        if not ok:
            raise Exception(f"no sketch segment found at ({px}, {py})")
        chain = bool(args.get("chain", True))
        # ISketchManager::SketchOffset2(Offset, BothDirections, Chain, CapEnds,
        # MakeConstruction, AddDimensions) — the legacy IModelDoc2 offset
        # methods return False and do nothing on SW2025 (probed live)
        result = doc.SketchManager.SketchOffset2(dist, False, chain, 0, False, False)
        doc.ClearSelection2(True)
        if not result:
            raise Exception(
                f"offset failed (distance {args['distance']}mm) — try the "
                f"opposite sign, or chain=false for a single segment")
        logger.info(f"Sketch offset {args['distance']}mm from ({px}, {py}), chain={chain}")
        sketch_id = f"sketch:{self.current_sketch_name}" if self.current_sketch_name else ""
        return self._json_result(
            f"✓ Offset {'chain' if chain else 'segment'} at ({px}, {py}) by "
            f"{args['distance']}mm (negative distance = other side)",
            type="sketch_offset", sketchId=sketch_id)

    def sketch_centerline(self, args: dict) -> str:
        """Draw a construction centerline in the active sketch"""
        doc = self.connection.get_active_doc()
        if not doc:
            raise Exception("No active document")
        self._require_active_sketch(doc)

        for key in ["x1", "y1", "x2", "y2"]:
            if key not in args:
                raise Exception(f"sketch_centerline requires {key}")

        x1, y1 = args["x1"], args["y1"]
        x2, y2 = args["x2"], args["y2"]

        p1, p2 = (x1 / 1000.0, y1 / 1000.0), (x2 / 1000.0, y2 / 1000.0)
        self._draw_checked(
            doc, f"centerline ({x1}, {y1}) -> ({x2}, {y2})",
            lambda sm: sm.CreateCenterLine(p1[0], p1[1], 0.0, p2[0], p2[1], 0.0),
            lambda o: self._check_line(o, p1[0], p1[1], p2[0], p2[1]))

        # Centerlines are construction geometry - don't update last_shape
        coordinates = {"x1": x1, "y1": y1, "x2": x2, "y2": y2}
        entity_id = ""
        if self.tracker:
            entity_id = self.tracker.register_entity("centerline", coordinates, {}, update_spatial=False)

        logger.info(f"Centerline: ({x1}, {y1}) to ({x2}, {y2}) mm")
        msg = f"✓ Centerline from ({x1:.1f}, {y1:.1f}) to ({x2:.1f}, {y2:.1f}) mm"
        sketch_id = f"sketch:{self.current_sketch_name}" if self.current_sketch_name else ""
        return self._json_result(msg, id=entity_id, type="centerline", sketchId=sketch_id)

    def sketch_point(self, args: dict) -> str:
        """Create a sketch point"""
        doc = self.connection.get_active_doc()
        if not doc:
            raise Exception("No active document")
        self._require_active_sketch(doc)

        for key in ["x", "y"]:
            if key not in args:
                raise Exception(f"sketch_point requires {key}")

        x, y = args["x"], args["y"]

        self._draw_checked(
            doc, f"point at ({x}, {y})",
            lambda sm: sm.CreatePoint(x / 1000.0, y / 1000.0, 0.0),
            lambda o: self._check_point(o, x / 1000.0, y / 1000.0))

        # Points are zero-dimensional - don't update last_shape
        coordinates = {"x": x, "y": y}
        entity_id = ""
        if self.tracker:
            entity_id = self.tracker.register_entity("point", coordinates, {}, update_spatial=False)

        logger.info(f"Point: ({x}, {y}) mm")
        msg = f"✓ Point at ({x:.1f}, {y:.1f}) mm"
        sketch_id = f"sketch:{self.current_sketch_name}" if self.current_sketch_name else ""
        return self._json_result(msg, id=entity_id, type="point", sketchId=sketch_id)

    def sketch_arc(self, args: dict) -> str:
        """Draw an arc in the active sketch (3-point or center-point mode)"""
        doc = self.connection.get_active_doc()
        if not doc:
            raise Exception("No active document")
        self._require_active_sketch(doc)

        mode = args.get("mode", "3point")

        if mode == "3point":
            for key in ["x1", "y1", "x2", "y2", "x3", "y3"]:
                if key not in args:
                    raise Exception(f"3-point arc requires {key}")

            x1, y1 = args["x1"], args["y1"]
            x2, y2 = args["x2"], args["y2"]
            x3, y3 = args["x3"], args["y3"]

            p1, p2, p3 = (x1 / 1000.0, y1 / 1000.0), (x2 / 1000.0, y2 / 1000.0), (x3 / 1000.0, y3 / 1000.0)
            self._draw_checked(
                doc, "3-point arc",
                lambda sm: sm.Create3PointArc(p1[0], p1[1], 0.0, p2[0], p2[1], 0.0, p3[0], p3[1], 0.0),
                lambda o: self._check_arc_3pt(o, p1, p2, p3))

            all_x = [x1, x2, x3]
            all_y = [y1, y2, y3]
            shape_info = {
                "type": "arc",
                "centerX": (min(all_x) + max(all_x)) / 2,
                "centerY": (min(all_y) + max(all_y)) / 2,
                "left": min(all_x),
                "right": max(all_x),
                "bottom": min(all_y),
                "top": max(all_y),
            }
            coordinates = {"x1": x1, "y1": y1, "x2": x2, "y2": y2, "x3": x3, "y3": y3, "mode": "3point"}

            entity_id = ""
            if self.tracker:
                entity_id = self.tracker.register_entity("arc", coordinates, shape_info)
                self._sync_spatial()
            else:
                self.created_shapes.append(shape_info)
                self.last_shape = shape_info

            logger.info(f"3-point arc: ({x1},{y1}), ({x2},{y2}), ({x3},{y3}) mm")
            msg = f"✓ 3-point arc through ({x1:.1f},{y1:.1f}), ({x2:.1f},{y2:.1f}), ({x3:.1f},{y3:.1f}) mm"
            sketch_id = f"sketch:{self.current_sketch_name}" if self.current_sketch_name else ""
            return self._json_result(msg, id=entity_id, type="arc", sketchId=sketch_id)

        elif mode == "center":
            for key in ["centerX", "centerY", "x1", "y1", "x2", "y2"]:
                if key not in args:
                    raise Exception(f"Center-point arc requires {key}")

            cx, cy = args["centerX"], args["centerY"]
            x1, y1 = args["x1"], args["y1"]
            x2, y2 = args["x2"], args["y2"]

            c, p1, p2 = (cx / 1000.0, cy / 1000.0), (x1 / 1000.0, y1 / 1000.0), (x2 / 1000.0, y2 / 1000.0)
            self._draw_checked(
                doc, "center-point arc",
                lambda sm: sm.CreateArc(c[0], c[1], 0.0, p1[0], p1[1], 0.0, p2[0], p2[1], 0.0,
                                        1),  # direction: 1 = counter-clockwise
                lambda o: self._check_arc_center(o, c, p1, p2))

            radius = math.sqrt((x1 - cx) ** 2 + (y1 - cy) ** 2)
            shape_info = {
                "type": "arc",
                "centerX": cx,
                "centerY": cy,
                "radius": radius,
                "left": cx - radius,
                "right": cx + radius,
                "bottom": cy - radius,
                "top": cy + radius,
            }
            coordinates = {"centerX": cx, "centerY": cy, "x1": x1, "y1": y1, "x2": x2, "y2": y2, "mode": "center"}

            entity_id = ""
            if self.tracker:
                entity_id = self.tracker.register_entity("arc", coordinates, shape_info)
                self._sync_spatial()
            else:
                self.created_shapes.append(shape_info)
                self.last_shape = shape_info

            logger.info(f"Center-point arc: center ({cx},{cy}), radius {radius:.1f} mm")
            msg = f"✓ Center-point arc at ({cx:.1f},{cy:.1f}), radius {radius:.1f}mm"
            sketch_id = f"sketch:{self.current_sketch_name}" if self.current_sketch_name else ""
            return self._json_result(msg, id=entity_id, type="arc", sketchId=sketch_id)

        else:
            raise Exception(f"Unknown arc mode: {mode}. Use '3point' or 'center'")

    def sketch_polygon(self, args: dict) -> str:
        """Draw a regular polygon in the active sketch"""
        doc = self.connection.get_active_doc()
        if not doc:
            raise Exception("No active document")
        self._require_active_sketch(doc)

        if "radius" not in args:
            raise Exception("sketch_polygon requires radius")
        if "numSides" not in args:
            raise Exception("sketch_polygon requires numSides")

        radius = args["radius"]
        num_sides = args["numSides"]
        inscribed = args.get("inscribed", False)

        # Use _calculate_position for relative positioning support
        center_x, center_y = self._calculate_position(args, radius * 2, radius * 2)

        cx_m = center_x / 1000.0
        cy_m = center_y / 1000.0

        # CreatePolygon(XC, YC, Zc, Xp, Yp, Zp, Sides, Inscribed): probed on
        # SW2022, the point (Xp, Yp) ALWAYS becomes a vertex; Inscribed only
        # chooses whether the construction circle is drawn inside the polygon
        # (radius = apothem) or through the vertices. So for an inscribed
        # radius (center to side midpoint) the vertex must be placed at
        # radius / cos(pi / n).
        vertex_r = radius / math.cos(math.pi / num_sides) if inscribed else radius
        r_m = vertex_r / 1000.0

        # Edge point: a vertex directly to the right of the center
        edge_x = cx_m + r_m
        edge_y = cy_m

        self._draw_checked(
            doc, f"{num_sides}-sided polygon",
            lambda sm: sm.CreatePolygon(cx_m, cy_m, 0.0, edge_x, edge_y, 0.0, num_sides, inscribed),
            lambda o: self._check_polygon(o, cx_m, cy_m, r_m, num_sides))

        shape_info = {
            "type": "polygon",
            "centerX": center_x,
            "centerY": center_y,
            "radius": radius,
            "numSides": num_sides,
            "inscribed": inscribed,
            "left": center_x - vertex_r,
            "right": center_x + vertex_r,
            "bottom": center_y - vertex_r,
            "top": center_y + vertex_r,
        }
        coordinates = {"centerX": center_x, "centerY": center_y, "radius": radius, "numSides": num_sides}

        entity_id = ""
        if self.tracker:
            entity_id = self.tracker.register_entity("polygon", coordinates, shape_info)
            self._sync_spatial()
        else:
            self.created_shapes.append(shape_info)
            self.last_shape = shape_info

        logger.info(f"Polygon: {num_sides} sides, radius {radius}mm at ({center_x:.1f}, {center_y:.1f})")
        msg = f"✓ {num_sides}-sided polygon, radius {radius}mm at ({center_x:.1f}, {center_y:.1f})"
        sketch_id = f"sketch:{self.current_sketch_name}" if self.current_sketch_name else ""
        return self._json_result(msg, id=entity_id, type="polygon", sketchId=sketch_id)

    def sketch_ellipse(self, args: dict) -> str:
        """Draw an ellipse in the active sketch"""
        doc = self.connection.get_active_doc()
        if not doc:
            raise Exception("No active document")
        self._require_active_sketch(doc)

        for key in ["centerX", "centerY", "majorRadius", "minorRadius"]:
            if key not in args:
                raise Exception(f"sketch_ellipse requires {key}")

        cx = args["centerX"]
        cy = args["centerY"]
        major_r = args["majorRadius"]
        minor_r = args["minorRadius"]
        angle_deg = args.get("angle", 0)
        angle_rad = math.radians(angle_deg)

        cx_m = cx / 1000.0
        cy_m = cy / 1000.0
        major_r_m = major_r / 1000.0
        minor_r_m = minor_r / 1000.0

        # Major axis endpoint rotated by angle
        major_x = cx_m + major_r_m * math.cos(angle_rad)
        major_y = cy_m + major_r_m * math.sin(angle_rad)

        # Minor axis endpoint perpendicular to major axis
        minor_x = cx_m + minor_r_m * math.cos(angle_rad + math.pi / 2)
        minor_y = cy_m + minor_r_m * math.sin(angle_rad + math.pi / 2)

        self._draw_checked(
            doc, f"ellipse {major_r}mm x {minor_r}mm",
            lambda sm: sm.CreateEllipse(cx_m, cy_m, 0.0, major_x, major_y, 0.0, minor_x, minor_y, 0.0),
            lambda o: self._check_ellipse(o, (cx_m, cy_m), major_r_m, minor_r_m, angle_rad))

        extent = max(major_r, minor_r)
        shape_info = {
            "type": "ellipse",
            "centerX": cx,
            "centerY": cy,
            "majorRadius": major_r,
            "minorRadius": minor_r,
            "left": cx - extent,
            "right": cx + extent,
            "bottom": cy - extent,
            "top": cy + extent,
        }
        coordinates = {"centerX": cx, "centerY": cy, "majorRadius": major_r, "minorRadius": minor_r}

        entity_id = ""
        if self.tracker:
            entity_id = self.tracker.register_entity("ellipse", coordinates, shape_info)
            self._sync_spatial()
        else:
            self.created_shapes.append(shape_info)
            self.last_shape = shape_info

        logger.info(f"Ellipse: {major_r}x{minor_r}mm at ({cx:.1f}, {cy:.1f}), angle {angle_deg}")
        msg = f"✓ Ellipse {major_r}mm x {minor_r}mm at ({cx:.1f}, {cy:.1f})"
        sketch_id = f"sketch:{self.current_sketch_name}" if self.current_sketch_name else ""
        return self._json_result(msg, id=entity_id, type="ellipse", sketchId=sketch_id)

    def sketch_spline(self, args: dict) -> str:
        """Draw a spline through a series of points"""
        doc = self.connection.get_active_doc()
        if not doc:
            raise Exception("No active document")
        self._require_active_sketch(doc)

        if "points" not in args:
            raise Exception("sketch_spline requires points array")

        points = args["points"]
        if len(points) < 2:
            raise Exception("Spline requires at least 2 points")

        import win32com.client
        import pythoncom

        # Build flat coordinate array: [x1,y1,z1, x2,y2,z2, ...]
        point_data = []
        for p in points:
            point_data.extend([p["x"] / 1000.0, p["y"] / 1000.0, 0.0])

        point_array = win32com.client.VARIANT(
            pythoncom.VT_ARRAY | pythoncom.VT_R8, point_data
        )
        want = [(p["x"] / 1000.0, p["y"] / 1000.0) for p in points]
        self._draw_checked(
            doc, f"spline through {len(points)} points",
            lambda sm: sm.CreateSpline2(point_array, True),
            lambda o: self._check_spline(o, want))

        xs = [p["x"] for p in points]
        ys = [p["y"] for p in points]
        shape_info = {
            "type": "spline",
            "centerX": (min(xs) + max(xs)) / 2,
            "centerY": (min(ys) + max(ys)) / 2,
            "left": min(xs),
            "right": max(xs),
            "bottom": min(ys),
            "top": max(ys),
            "width": max(xs) - min(xs),
            "height": max(ys) - min(ys),
        }
        coordinates = {"points": [{"x": p["x"], "y": p["y"]} for p in points]}

        entity_id = ""
        if self.tracker:
            entity_id = self.tracker.register_entity("spline", coordinates, shape_info)
            self._sync_spatial()
        else:
            self.created_shapes.append(shape_info)
            self.last_shape = shape_info

        logger.info(f"Spline: {len(points)} points")
        msg = f"✓ Spline through {len(points)} points"
        sketch_id = f"sketch:{self.current_sketch_name}" if self.current_sketch_name else ""
        return self._json_result(msg, id=entity_id, type="spline", sketchId=sketch_id)

    def sketch_slot(self, args: dict) -> str:
        """Draw a slot shape in the active sketch"""
        doc = self.connection.get_active_doc()
        if not doc:
            raise Exception("No active document")
        self._require_active_sketch(doc)

        for key in ["x1", "y1", "x2", "y2", "width"]:
            if key not in args:
                raise Exception(f"sketch_slot requires {key}")

        x1, y1 = args["x1"], args["y1"]
        x2, y2 = args["x2"], args["y2"]
        width = args["width"]

        # CreateSketchSlot returns an ISketchSlot, not segments: the segments it
        # added are found by comparing the sketch before and after.
        p1, p2 = (x1 / 1000.0, y1 / 1000.0), (x2 / 1000.0, y2 / 1000.0)
        self._draw_checked(
            doc, f"slot width {width}mm",
            lambda sm: self._new_segments(doc, lambda: sm.CreateSketchSlot(
                0,                                   # slotType: straight (center line)
                0,                                   # lengthType: center-to-center
                width / 1000.0,                      # slot width in meters
                p1[0], p1[1], 0.0,                   # first center point
                p2[0], p2[1], 0.0,                   # second center point
                0.0, 0.0, 0.0,                       # third point (unused for straight)
                1,                                   # centerArcDirection
                False)),                             # addDimension
            lambda segs: self._check_slot(segs, p1, p2, width / 1000.0))

        half_w = width / 2
        shape_info = {
            "type": "slot",
            "centerX": (x1 + x2) / 2,
            "centerY": (y1 + y2) / 2,
            "left": min(x1, x2) - half_w,
            "right": max(x1, x2) + half_w,
            "bottom": min(y1, y2) - half_w,
            "top": max(y1, y2) + half_w,
            "width": max(x1, x2) - min(x1, x2) + width,
            "height": max(y1, y2) - min(y1, y2) + width,
        }
        coordinates = {"x1": x1, "y1": y1, "x2": x2, "y2": y2, "width": width}

        entity_id = ""
        if self.tracker:
            entity_id = self.tracker.register_entity("slot", coordinates, shape_info)
            self._sync_spatial()
        else:
            self.created_shapes.append(shape_info)
            self.last_shape = shape_info

        logger.info(f"Slot: ({x1},{y1}) to ({x2},{y2}), width {width}mm")
        msg = f"✓ Slot from ({x1:.1f},{y1:.1f}) to ({x2:.1f},{y2:.1f}), width {width}mm"
        sketch_id = f"sketch:{self.current_sketch_name}" if self.current_sketch_name else ""
        return self._json_result(msg, id=entity_id, type="slot", sketchId=sketch_id)

    def sketch_text(self, args: dict) -> str:
        """Insert sketch text in the active sketch"""
        doc = self.connection.get_active_doc()
        if not doc:
            raise Exception("No active document")
        self._require_active_sketch(doc)

        for key in ["x", "y", "text", "height"]:
            if key not in args:
                raise Exception(f"sketch_text requires {key}")

        x, y = args["x"], args["y"]
        text = args["text"]
        height = args["height"]
        angle_rad = math.radians(args.get("angle", 0))

        # InsertSketchText signature (9 params):
        #   Ptx, Pty, Ptz, Text, Alignment, FlipDirection,
        #   HorizontalMirror, WidthFactor, SpaceBetweenChars
        # Height and angle are set separately via ITextFormat
        sketch_text_obj = doc.InsertSketchText(
            x / 1000.0, y / 1000.0, 0.0,
            text,
            1,     # Alignment: left
            0,     # FlipDirection
            0,     # HorizontalMirror
            1,     # WidthFactor
            0      # SpaceBetweenChars
        )

        self._check_drawn(sketch_text_obj, f"text {text!r}")
        if sketch_text_obj:
            try:
                tf = sketch_text_obj.GetTextFormat()
                if tf:
                    tf.CharHeight = height / 1000.0
                    if angle_rad != 0:
                        tf.Escapement = angle_rad
                    sketch_text_obj.SetTextFormat(False, tf)
            except Exception:
                # GetTextFormat not available in dynamic COM dispatch
                logger.warning("Could not set text format (ITextFormat not available via dynamic COM)")

        # Text doesn't update last_shape
        coordinates = {"x": x, "y": y, "text": text, "height": height}
        entity_id = ""
        if self.tracker:
            entity_id = self.tracker.register_entity("text", coordinates, {}, update_spatial=False)

        logger.info(f"Text: '{text}' at ({x}, {y}) mm, height {height}mm")
        msg = f"✓ Text '{text}' at ({x:.1f}, {y:.1f}) mm, height {height}mm"
        sketch_id = f"sketch:{self.current_sketch_name}" if self.current_sketch_name else ""
        return self._json_result(msg, id=entity_id, type="text", sketchId=sketch_id)

    @staticmethod
    def _convert_dimension_value(dim_display, user_value: float) -> tuple:
        """Convert user-supplied dimension value to SolidWorks system units.
        Angular (Type2==3): degrees -> radians. All others: mm -> meters.

        swDisplayDimensionType_e verified live: 3 = swAngularDimension
        (many online references incorrectly list 1, which is
        swRadialDimension). Raises if Type2 can't be read \u2014 guessing
        linear on an angular dimension would apply value/1000 as radians
        (~57000x error) while still reporting \u2713.
        """
        SW_ANGULAR_DIMENSION = 3  # swDisplayDimensionType_e.swAngularDimension
        try:
            dim_type = dim_display.Type2
        except Exception as e:
            raise Exception(
                f"Could not read dimension type (IDisplayDimension.Type2): {e}. "
                f"Refusing to guess linear vs angular \u2014 the value was NOT set."
            )
        if dim_type == SW_ANGULAR_DIMENSION:
            return math.radians(user_value), "\u00b0"
        else:
            return user_value / 1000.0, "mm"

    def sketch_dimension(self, args: dict) -> str:
        """Add a smart dimension to selected sketch entities"""
        doc = self.connection.get_active_doc()
        if not doc:
            raise Exception("No active document")

        if "entityPoints" not in args:
            raise Exception("sketch_dimension requires entityPoints")
        for key in ["dimX", "dimY"]:
            if key not in args:
                raise Exception(f"sketch_dimension requires {key}")

        import win32com.client
        import pythoncom

        entity_points = args["entityPoints"]
        entity_types = args.get("entityTypes", ["SKETCHSEGMENT"] * len(entity_points))

        doc.ClearSelection2(True)
        callout = win32com.client.VARIANT(pythoncom.VT_DISPATCH, None)

        for i, pt in enumerate(entity_points):
            append = i > 0
            success = doc.Extension.SelectByID2(
                "", entity_types[i],
                pt["x"] / 1000.0, pt["y"] / 1000.0, 0.0,
                append, 0, callout, 0
            )
            if not success:
                raise Exception(f"Failed to select entity at ({pt['x']}, {pt['y']}) mm")

        # AddDimension2 opens a blocking "Modify" dialog while the user
        # preference "Input dimension value" is on. Switch it off for this
        # call (language-independent); the background dismisser stays as a
        # safety net in case the preference could not be changed.
        dismiss_thread = threading.Thread(target=dismiss_modify_dialog, daemon=True)
        dismiss_thread.start()

        sm = doc.SketchManager
        sm.AddToDB = True
        sm.DisplayWhenAdded = False

        try:
            with no_dimension_value_prompt(self.connection.app):
                dim_display = doc.AddDimension2(
                    args["dimX"] / 1000.0,
                    args["dimY"] / 1000.0,
                    0.0
                )

            if not dim_display:
                raise Exception("Failed to add dimension. Ensure valid entities are selected.")

            result_msg = f"Dimension added at ({args['dimX']:.1f}, {args['dimY']:.1f}) mm"

            # The dimension's addressable name (e.g. 'D1@Sketch1') — without
            # it, set_parameter targets had to be GUESSED from creation order
            param_name = None
            try:
                from .com_utils import com_prop
                dim_obj = dim_display.GetDimension2(0)
                if dim_obj is not None:
                    full = str(com_prop(dim_obj, "FullName") or "")
                    param_name = "@".join(full.split("@")[:2]) or None
                    if param_name:
                        result_msg += f" ({param_name})"
            except Exception as e:
                logger.debug(f"dimension name read failed: {e}")

            # Optionally set the dimension value to drive geometry
            if "value" in args:
                system_value, unit_label = self._convert_dimension_value(dim_display, args["value"])
                dim = dim_display.GetDimension2(0)
                if dim:
                    dim.SetSystemValue3(system_value, 2, "")
                    result_msg += f", value set to {args['value']}{unit_label}"

            doc.ClearSelection2(True)
        finally:
            sm.AddToDB = False
            sm.DisplayWhenAdded = True
            dismiss_thread.join(timeout=2)

        logger.info(result_msg)
        return self._json_result(f"✓ {result_msg}", type="dimension",
                                 parameter=param_name)

    def set_dimension_value(self, args: dict) -> str:
        """Set the value of an existing dimension by selecting it"""
        doc = self.connection.get_active_doc()
        if not doc:
            raise Exception("No active document")

        for key in ["dimX", "dimY", "value"]:
            if key not in args:
                raise Exception(f"set_dimension_value requires {key}")

        import win32com.client
        import pythoncom

        doc.ClearSelection2(True)
        callout = win32com.client.VARIANT(pythoncom.VT_DISPATCH, None)

        success = doc.Extension.SelectByID2(
            "", "DIMENSION",
            args["dimX"] / 1000.0, args["dimY"] / 1000.0, 0.0,
            False, 0, callout, 0
        )
        if not success:
            raise Exception(f"No dimension found at ({args['dimX']}, {args['dimY']}) mm")

        sel_mgr = doc.SelectionManager
        dim_display = sel_mgr.GetSelectedObject6(1, -1)
        dim = dim_display.GetDimension2(0)

        system_value, unit_label = self._convert_dimension_value(dim_display, args["value"])
        dim.SetSystemValue3(system_value, 2, "")
        doc.ClearSelection2(True)

        logger.info(f"Dimension value set to {args['value']}{unit_label} at ({args['dimX']}, {args['dimY']})")
        return self._json_result(f"✓ Dimension value set to {args['value']}{unit_label}", type="set_dimension")

    # Constraint type mapping: user-friendly name -> SolidWorks API string
    CONSTRAINT_MAP = {
        "COINCIDENT": "sgCOINCIDENT",
        "CONCENTRIC": "sgCONCENTRIC",
        "TANGENT": "sgTANGENT",
        "PARALLEL": "sgPARALLEL",
        "PERPENDICULAR": "sgPERPENDICULAR",
        "HORIZONTAL": "sgHORIZONTAL2D",
        "VERTICAL": "sgVERTICAL2D",
        "EQUAL": "sgEQUAL",
        "SYMMETRIC": "sgSYMMETRIC",
        "MIDPOINT": "sgMIDPOINT",
        "COLLINEAR": "sgCOLLINEAR",
        "CORADIAL": "sgCORADIAL",
    }

    def sketch_constraint(self, args: dict) -> str:
        """Add a geometric constraint between selected sketch entities"""
        doc = self.connection.get_active_doc()
        if not doc:
            raise Exception("No active document")

        if "constraintType" not in args:
            raise Exception("sketch_constraint requires constraintType")
        if "entityPoints" not in args:
            raise Exception("sketch_constraint requires entityPoints")

        constraint_type = args["constraintType"]
        if constraint_type not in self.CONSTRAINT_MAP:
            raise Exception(
                f"Unknown constraint type: {constraint_type}. "
                f"Valid: {list(self.CONSTRAINT_MAP.keys())}"
            )

        import win32com.client
        import pythoncom

        entity_points = args["entityPoints"]
        entity_types = args.get("entityTypes", ["SKETCHSEGMENT"] * len(entity_points))

        doc.ClearSelection2(True)
        callout = win32com.client.VARIANT(pythoncom.VT_DISPATCH, None)

        for i, pt in enumerate(entity_points):
            append = i > 0
            success = doc.Extension.SelectByID2(
                "", entity_types[i],
                pt["x"] / 1000.0, pt["y"] / 1000.0, 0.0,
                append, 0, callout, 0
            )
            if not success:
                raise Exception(f"Failed to select entity at ({pt['x']}, {pt['y']}) mm")

        doc.SketchAddConstraints(self.CONSTRAINT_MAP[constraint_type])

        logger.info(f"Constraint '{constraint_type}' applied")
        return self._json_result(f"✓ Constraint '{constraint_type}' applied", type="constraint")

    def toggle_construction(self, args: dict) -> str:
        """Toggle construction geometry flag on a sketch entity"""
        doc = self.connection.get_active_doc()
        if not doc:
            raise Exception("No active document")

        for key in ["x", "y"]:
            if key not in args:
                raise Exception(f"toggle_construction requires {key}")

        import win32com.client
        import pythoncom

        doc.ClearSelection2(True)
        callout = win32com.client.VARIANT(pythoncom.VT_DISPATCH, None)

        success = doc.Extension.SelectByID2(
            "", "SKETCHSEGMENT",
            args["x"] / 1000.0, args["y"] / 1000.0, 0.0,
            False, 0, callout, 0
        )
        if not success:
            raise Exception(f"No sketch segment found at ({args['x']}, {args['y']}) mm")

        sel_mgr = doc.SelectionManager
        sketch_seg = sel_mgr.GetSelectedObject6(1, -1)

        current = sketch_seg.ConstructionGeometry
        sketch_seg.ConstructionGeometry = not current

        new_state = "construction" if not current else "normal"
        logger.info(f"Entity at ({args['x']}, {args['y']}) mm toggled to {new_state}")
        return self._json_result(
            f"✓ Entity at ({args['x']:.1f}, {args['y']:.1f}) mm is now {new_state} geometry",
            type="toggle_construction"
        )

    def get_last_shape_info(self) -> str:
        """Get information about the last created shape"""
        last = self.tracker.last_shape if self.tracker else self.last_shape
        if not last:
            return self._json_result("❌ No shapes have been created yet")

        return json.dumps({"result": "✓ Last shape info", "shapeInfo": last})
    
    def exit_sketch(self) -> str:
        """Exit sketch mode"""
        doc = self.connection.get_active_doc()
        if not doc:
            raise Exception("No active document")

        doc.SketchManager.InsertSketch(True)

        # Find the actual sketch name from the feature tree
        actual_name = None
        try:
            actual_name = newest_sketch_name(doc)
        except Exception as e:
            logger.warning(f"Could not verify sketch name: {e}")

        if actual_name:
            self.current_sketch_name = actual_name
            sketch_id = ""
            if self.tracker:
                sketch_id = self.tracker.close_sketch(actual_name)
                if sketch_id is None:
                    logger.info("Exited an empty sketch; SOLIDWORKS discarded it")
                    return self._json_result(
                        "✓ Exited sketch mode — the sketch was empty, so "
                        "SOLIDWORKS discarded it (nothing was created)",
                        id=None, type="exit_sketch", discarded=True)
            logger.info(f"Exited sketch: {actual_name}")
            return self._json_result(
                f"✓ Exited sketch mode ({actual_name})",
                id=sketch_id, type="exit_sketch"
            )
        else:
            logger.warning("Exited sketch but could not find it in feature tree")
            return self._json_result("✓ Exited sketch mode", type="exit_sketch")

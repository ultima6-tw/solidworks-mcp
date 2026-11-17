"""
SolidWorks MCP - Unified Test Suite

Combines all tests from test_solidworks.py and the integration suite.

Usage:
    python test.py                          # Run all tests
    python test.py --gui                    # Interactive CLI test picker
    python test.py --category "Sketch Tools"  # Run one category
    python test.py --test sketch_line       # Run one test by name
    python test.py --list                   # List all available tests
"""

import win32com.client
import pythoncom
import glob
import json
import os
import shutil
import sys
import tempfile
import traceback
import time
import argparse
import math
from dataclasses import dataclass, field
from typing import Callable, Optional

from solidworks import selection_helpers as sel

# Force UTF-8 output so Unicode symbols survive the Windows console
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if sys.stderr.encoding and sys.stderr.encoding.lower() != "utf-8":
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


# ---------------------------------------------------------------------------
# Test registry
# ---------------------------------------------------------------------------

@dataclass
class TestEntry:
    name: str
    display_name: str
    category: str
    description: str
    func: Callable
    order: int = 0

TEST_REGISTRY: list[TestEntry] = []

CATEGORY_ORDER = ["Basic", "Sketch Tools", "Feature Tools", "MCP Tools", "Assembly", "Integration"]


def register_test(name, display_name, category, description, order=0):
    """Decorator to register a test function."""
    def decorator(func):
        TEST_REGISTRY.append(TestEntry(
            name=name,
            display_name=display_name,
            category=category,
            description=description,
            func=func,
            order=order,
        ))
        return func
    return decorator


# ---------------------------------------------------------------------------
# Global result tracking
# ---------------------------------------------------------------------------

PASS = 0
FAIL = 0
RESULTS = []  # list of (label, ok, detail)


def _result(label, ok, detail=""):
    global PASS, FAIL
    RESULTS.append((label, ok, detail))
    if ok:
        PASS += 1
        print(f"  \u2713 {label}{' \u2014 ' + detail if detail else ''}")
    else:
        FAIL += 1
        print(f"  \u2717 {label}{' \u2014 ' + detail if detail else ''}")
    return ok


def log(message, level="INFO"):
    prefix = "\u2713" if level == "SUCCESS" else "\u274c" if level == "ERROR" else "\u2192"
    print(f"  {prefix} {message}")


def section(title):
    print(f"\n{'='*60}")
    print(f"  {title}")
    print(f"{'='*60}")


def subsection(title):
    print(f"\n  [{title}]")


# ---------------------------------------------------------------------------
# SolidWorks connection helpers
# ---------------------------------------------------------------------------

def connect_to_solidworks():
    """Connect to an existing or new SolidWorks instance."""
    try:
        sw = win32com.client.GetActiveObject("SldWorks.Application")
        print("  \u2192 Attached to existing SolidWorks instance")
    except Exception:
        print("  \u2192 Launching new SolidWorks instance\u2026")
        sw = win32com.client.Dispatch("SldWorks.Application")
        sw.Visible = True
        for i in range(30):
            try:
                _ = sw.RevisionNumber
                break
            except Exception:
                time.sleep(1)
        else:
            raise RuntimeError("SolidWorks failed to start within 30 s")
    return sw


def find_template():
    """Discover the Part template file."""
    patterns = [
        r"C:\ProgramData\SOLIDWORKS\SOLIDWORKS *\templates\Part.prtdot",
        r"C:\ProgramData\SOLIDWORKS\SOLIDWORKS *\templates\Part.PRTDOT",
    ]
    for pattern in patterns:
        hits = glob.glob(pattern)
        if hits:
            return hits[0]
    return None


def close_all_docs(sw):
    """Close all open SolidWorks documents without saving."""
    try:
        sw.CloseAllDocuments(True)
    except Exception:
        # Fallback: close one-by-one (bounded — QuitDoc can decline to close)
        last_title = None
        for _ in range(50):
            doc = sw.ActiveDoc
            if not doc:
                break
            title = doc.GetTitle
            if title == last_title:
                break
            sw.QuitDoc(title)
            last_title = title


# ---------------------------------------------------------------------------
# Sketch / modeling helpers
# ---------------------------------------------------------------------------

def new_part(sw, template):
    doc = sw.NewDocument(template, 0, 0, 0)
    if not doc:
        raise RuntimeError("NewDocument returned None")
    return doc


# ---------------------------------------------------------------------------
# Language-independent feature names
# ---------------------------------------------------------------------------
# SOLIDWORKS names new features in the UI language ("Sketch1" is "草圖1" on a
# Chinese UI), and the standard plane names come from the part template. The
# tests are written with the English default names; find_feature()/N() map such a
# name to the real feature by type + creation order, so the suite also runs
# on non-English installs.
_DEFAULT_NAME_TYPES = {
    "Sketch": ("ProfileFeature",),
    "Boss-Extrude": ("Extrusion",),
    "Cut-Extrude": ("ICE", "Cut"),
    "Fillet": ("Fillet",),
    "Chamfer": ("Chamfer",),
    "Coordinate System": ("CoordSys",),
}


def _features_in_order(doc):
    out, f = [], doc.FirstFeature
    while f is not None and len(out) < 5000:
        out.append(f)
        f = f.GetNextFeature
    return out


def find_feature(doc, name):
    """doc.FeatureByName(name), falling back to type+ordinal for default
    English names on localized SOLIDWORKS. Returns None if not found."""
    f = doc.FeatureByName(name)
    if f is not None:
        return f
    key = sel.standard_plane_key(name)
    if key:
        return sel.resolve_plane_feature(doc, key)
    import re
    m = re.fullmatch(r"(.*?)(\d+)", name)
    if not m or m.group(1) not in _DEFAULT_NAME_TYPES:
        return None
    types, n = _DEFAULT_NAME_TYPES[m.group(1)], int(m.group(2))
    seen = []
    for f in _features_in_order(doc):
        try:
            if f.GetTypeName2 in types and f.Name not in seen:
                seen.append(f.Name)
                if len(seen) == n:
                    return f
        except Exception:
            continue
    return None


def N(doc, name):
    """Actual (possibly localized) name of the feature the English default
    `name` refers to; the input unchanged if it cannot be resolved."""
    f = find_feature(doc, name)
    return f.Name if f is not None else name


def is_standard_plane(doc, f):
    return f.Name in sel.get_standard_plane_names(doc).values()


def new_sketch_on_front(sw, template):
    """Create a new part and open a sketch on the Front Plane."""
    model = sw.NewDocument(template, 0, 0, 0)
    front_plane = find_feature(model, "Front Plane")
    model.ClearSelection2(True)
    front_plane.Select2(False, 0)
    model.SketchManager.InsertSketch(True)
    return model


def select_plane(doc, plane_name):
    """Select a plane — delegates to selection_helpers.select_plane()."""
    ok = sel.select_plane(doc, plane_name)
    if not ok:
        raise RuntimeError(f"Plane not found: {plane_name}")
    return ok


def create_sketch_on_plane(doc, plane_name):
    """Select a plane and open a sketch on it."""
    select_plane(doc, plane_name)
    doc.SketchManager.InsertSketch(True)


def create_sketch_on_face(doc, x_mm, y_mm, z_mm):
    """Select a solid face at (x,y,z) in mm and open a sketch on it."""
    ok = sel.select_face(doc, x_mm, y_mm, z_mm)
    if not ok:
        raise RuntimeError(f"Could not select face at ({x_mm},{y_mm},{z_mm}) mm")
    doc.SketchManager.InsertSketch(True)


def exit_sketch(doc):
    doc.ClearSelection2(True)
    doc.SketchManager.InsertSketch(True)


def select_sketch(doc, sketch_name):
    """Select a sketch by name — delegates to selection_helpers.select_sketch()."""
    ok = sel.select_sketch(doc, sketch_name)
    if not ok:
        raise RuntimeError(f"Sketch not found: {sketch_name}")
    return ok


def get_latest_sketch_name(doc):
    """Mirror the fallback logic in modeling.py._get_latest_sketch_name()."""
    try:
        features = doc.FeatureManager.GetFeatures(True)
        if features:
            for feature in reversed(features):
                if feature.GetTypeName2 == "ProfileFeature":
                    return feature.Name
    except Exception:
        pass
    return "Sketch1"


def extrude(doc, sketch_name, depth_mm, cut=False, reverse=False):
    """Select sketch and create an extrusion or cut-extrusion."""
    select_sketch(doc, sketch_name)
    depth_m = depth_mm / 1000.0

    if cut:
        # Flip (param 2) = side to cut, Dir (param 3) = direction
        feature = doc.FeatureManager.FeatureCut4(
            True, False, reverse, 0, 0, depth_m, 0.0,
            False, False, False, False, 0.0, 0.0,
            False, False, False, False, False, False, True,
            False, True, False, 0, 0.0, False, False
        )
        if not feature:
            raise RuntimeError("FeatureCut4 returned None for cut-extrusion")
    else:
        # Flip (param 2) only applies to cuts; direction is Dir (param 3)
        feature = doc.FeatureManager.FeatureExtrusion2(
            True, False, reverse, 0, 0, depth_m, 0.0,
            False, False, False, False, 0.0, 0.0,
            False, False, False, False, True, True, True, 0, 0, False
        )
        if not feature:
            raise RuntimeError("FeatureExtrusion2 returned None for extrusion")
    return feature


def make_cube(sw, template, size_mm=100):
    """Create a part with a cube of given size. Returns the model."""
    size_m = size_mm / 1000.0
    model = sw.NewDocument(template, 0, 0, 0)
    front_plane = find_feature(model, "Front Plane")
    model.ClearSelection2(True)
    front_plane.Select2(False, 0)
    model.SketchManager.InsertSketch(True)
    model.SketchManager.CreateCornerRectangle(0.0, 0.0, 0.0, size_m, size_m, 0.0)
    exit_sketch(model)
    sketch1 = find_feature(model, "Sketch1")
    model.ClearSelection2(True)
    sketch1.Select2(False, 0)
    feat = model.FeatureManager.FeatureExtrusion2(
        True, False, False, 0, 0, size_m, 0.0,
        False, False, False, False, 0.0, 0.0,
        False, False, False, False, True, True, True,
        0, 0, False
    )
    if not feat:
        raise Exception("Failed to create base cube for test")
    return model


# ===========================================================================
# TEST FUNCTIONS — Basic
# ===========================================================================

@register_test("basic_cube", "Basic Cube (100mm)", "Basic",
               "Create a 100mm cube end-to-end: part, sketch, extrude", order=0)
def test_basic_cube(sw, template):
    try:
        model = sw.NewDocument(template, 0, 0, 0)
        front_plane = find_feature(model, "Front Plane")
        model.ClearSelection2(True)
        front_plane.Select2(False, 0)
        model.SketchManager.InsertSketch(True)
        model.SketchManager.CreateCornerRectangle(0.0, 0.0, 0.0, 0.1, 0.1, 0.0)
        exit_sketch(model)

        sketch1 = find_feature(model, "Sketch1")
        model.ClearSelection2(True)
        sketch1.Select2(False, 0)
        feat = model.FeatureManager.FeatureExtrusion2(
            True, False, False, 0, 0, 0.1, 0.0,
            False, False, False, False, 0.0, 0.0,
            False, False, False, False, True, True, True,
            0, 0, False
        )
        if not feat:
            log("Extrusion returned None", "ERROR")
            return False

        model.ViewZoomtofit2()
        log("100mm cube created", "SUCCESS")
        return True
    except Exception as e:
        log(f"basic_cube FAILED: {e}", "ERROR")
        traceback.print_exc()
        return False


# ===========================================================================
# TEST FUNCTIONS — Sketch Tools
# ===========================================================================

@register_test("sketch_line", "Sketch Line", "Sketch Tools",
               "Draw a line from (0,0) to (50,50)mm", order=0)
def test_sketch_line(sw, template):
    try:
        model = new_sketch_on_front(sw, template)
        model.SketchManager.CreateLine(0.0, 0.0, 0.0, 0.05, 0.05, 0.0)
        log("Line created", "SUCCESS")
        exit_sketch(model)
        model.ViewZoomtofit2()
        return True
    except Exception as e:
        log(f"sketch_line FAILED: {e}", "ERROR")
        return False


@register_test("sketch_centerline", "Sketch Centerline", "Sketch Tools",
               "Draw a vertical centerline", order=1)
def test_sketch_centerline(sw, template):
    try:
        model = new_sketch_on_front(sw, template)
        model.SketchManager.CreateCenterLine(0.0, -0.05, 0.0, 0.0, 0.05, 0.0)
        log("Centerline created", "SUCCESS")
        exit_sketch(model)
        model.ViewZoomtofit2()
        return True
    except Exception as e:
        log(f"sketch_centerline FAILED: {e}", "ERROR")
        return False


@register_test("sketch_point", "Sketch Point", "Sketch Tools",
               "Create a sketch point at (25,25)mm", order=2)
def test_sketch_point(sw, template):
    try:
        model = new_sketch_on_front(sw, template)
        model.SketchManager.CreatePoint(0.025, 0.025, 0.0)
        log("Point created", "SUCCESS")
        exit_sketch(model)
        model.ViewZoomtofit2()
        return True
    except Exception as e:
        log(f"sketch_point FAILED: {e}", "ERROR")
        return False


@register_test("sketch_arc", "Sketch Arc", "Sketch Tools",
               "Draw a 3-point arc and a center-point arc", order=3)
def test_sketch_arc(sw, template):
    try:
        model = new_sketch_on_front(sw, template)

        model.SketchManager.Create3PointArc(
            0.0, 0.0, 0.0,
            0.05, 0.0, 0.0,
            0.025, 0.02, 0.0
        )
        log("3-point arc created", "SUCCESS")

        model.SketchManager.CreateArc(
            0.0, -0.03, 0.0,
            0.02, -0.03, 0.0,
            -0.02, -0.03, 0.0,
            1
        )
        log("Center-point arc created", "SUCCESS")

        exit_sketch(model)
        model.ViewZoomtofit2()
        return True
    except Exception as e:
        log(f"sketch_arc FAILED: {e}", "ERROR")
        traceback.print_exc()
        return False


@register_test("sketch_polygon", "Sketch Polygon", "Sketch Tools",
               "Draw a hexagon and extrude it", order=4)
def test_sketch_polygon(sw, template):
    try:
        model = new_sketch_on_front(sw, template)

        model.SketchManager.CreatePolygon(
            0.0, 0.0, 0.0,
            0.025, 0.0, 0.0,
            6, False
        )
        log("Hexagon created", "SUCCESS")

        exit_sketch(model)

        sketch1 = find_feature(model, "Sketch1")
        model.ClearSelection2(True)
        sketch1.Select2(False, 0)
        feat = model.FeatureManager.FeatureExtrusion2(
            True, False, False, 0, 0, 0.05, 0.0,
            False, False, False, False, 0.0, 0.0,
            False, False, False, False, True, True, True,
            0, 0, False
        )
        if not feat:
            log("Polygon extrusion returned None", "ERROR")
            return False

        log("Hexagon extruded (50mm)", "SUCCESS")
        model.ViewZoomtofit2()
        return True
    except Exception as e:
        log(f"sketch_polygon FAILED: {e}", "ERROR")
        traceback.print_exc()
        return False


@register_test("sketch_ellipse", "Sketch Ellipse", "Sketch Tools",
               "Draw a 30x20mm ellipse and extrude it", order=5)
def test_sketch_ellipse(sw, template):
    try:
        model = new_sketch_on_front(sw, template)

        cx_m, cy_m = 0.0, 0.0
        major_r_m = 0.03
        minor_r_m = 0.02
        angle = 0.0

        major_x = cx_m + major_r_m * math.cos(angle)
        major_y = cy_m + major_r_m * math.sin(angle)
        minor_x = cx_m + minor_r_m * math.cos(angle + math.pi / 2)
        minor_y = cy_m + minor_r_m * math.sin(angle + math.pi / 2)

        model.SketchManager.CreateEllipse(
            cx_m, cy_m, 0.0,
            major_x, major_y, 0.0,
            minor_x, minor_y, 0.0
        )
        log("Ellipse created (30x20mm)", "SUCCESS")

        exit_sketch(model)

        sketch1 = find_feature(model, "Sketch1")
        model.ClearSelection2(True)
        sketch1.Select2(False, 0)
        feat = model.FeatureManager.FeatureExtrusion2(
            True, False, False, 0, 0, 0.04, 0.0,
            False, False, False, False, 0.0, 0.0,
            False, False, False, False, True, True, True,
            0, 0, False
        )
        if not feat:
            log("Ellipse extrusion returned None", "ERROR")
            return False

        log("Ellipse extruded (40mm)", "SUCCESS")
        model.ViewZoomtofit2()
        return True
    except Exception as e:
        log(f"sketch_ellipse FAILED: {e}", "ERROR")
        traceback.print_exc()
        return False


@register_test("sketch_slot", "Sketch Slot", "Sketch Tools",
               "Draw a 50mm slot and extrude it", order=6)
def test_sketch_slot(sw, template):
    try:
        model = new_sketch_on_front(sw, template)

        model.SketchManager.CreateSketchSlot(
            0, 0, 0.02,
            -0.025, 0.0, 0.0,
            0.025, 0.0, 0.0,
            0.0, 0.0, 0.0,
            1, False
        )
        log("Slot created (50mm long, 20mm wide)", "SUCCESS")

        exit_sketch(model)

        sketch1 = find_feature(model, "Sketch1")
        model.ClearSelection2(True)
        sketch1.Select2(False, 0)
        feat = model.FeatureManager.FeatureExtrusion2(
            True, False, False, 0, 0, 0.01, 0.0,
            False, False, False, False, 0.0, 0.0,
            False, False, False, False, True, True, True,
            0, 0, False
        )
        if not feat:
            log("Slot extrusion returned None", "ERROR")
            return False

        log("Slot extruded (10mm)", "SUCCESS")
        model.ViewZoomtofit2()
        return True
    except Exception as e:
        log(f"sketch_slot FAILED: {e}", "ERROR")
        traceback.print_exc()
        return False


@register_test("sketch_spline", "Sketch Spline", "Sketch Tools",
               "Draw a spline through 4 points", order=7)
def test_sketch_spline(sw, template):
    try:
        model = new_sketch_on_front(sw, template)

        point_data = [
            0.0, 0.0, 0.0,
            0.02, 0.01, 0.0,
            0.04, -0.01, 0.0,
            0.06, 0.0, 0.0,
        ]
        point_array = win32com.client.VARIANT(
            pythoncom.VT_ARRAY | pythoncom.VT_R8, point_data
        )
        model.SketchManager.CreateSpline2(point_array, True)
        log("Spline created through 4 points", "SUCCESS")

        exit_sketch(model)
        model.ViewZoomtofit2()
        return True
    except Exception as e:
        log(f"sketch_spline FAILED: {e}", "ERROR")
        traceback.print_exc()
        return False


@register_test("sketch_text", "Sketch Text", "Sketch Tools",
               "Insert sketch text 'HELLO'", order=8)
def test_sketch_text(sw, template):
    try:
        model = new_sketch_on_front(sw, template)

        sketch_text_obj = model.InsertSketchText(
            0.0, 0.0, 0.0, "HELLO", 1, 0, 0, 1, 0
        )
        if not sketch_text_obj:
            log("InsertSketchText returned None", "ERROR")
            return False
        log("Sketch text 'HELLO' created", "SUCCESS")

        exit_sketch(model)
        model.ViewZoomtofit2()
        return True
    except Exception as e:
        log(f"sketch_text FAILED: {e}", "ERROR")
        traceback.print_exc()
        return False


@register_test("sketch_constraint", "Sketch Constraint", "Sketch Tools",
               "Draw two lines and apply a parallel constraint", order=9)
def test_sketch_constraint(sw, template):
    try:
        model = new_sketch_on_front(sw, template)

        model.SketchManager.CreateLine(0.0, 0.0, 0.0, 0.05, 0.02, 0.0)
        model.SketchManager.CreateLine(0.0, 0.03, 0.0, 0.05, 0.04, 0.0)
        log("Two lines drawn", "SUCCESS")

        callout = win32com.client.VARIANT(pythoncom.VT_DISPATCH, None)
        model.ClearSelection2(True)
        model.Extension.SelectByID2(
            "", "SKETCHSEGMENT", 0.025, 0.01, 0.0,
            False, 0, callout, 0
        )
        model.Extension.SelectByID2(
            "", "SKETCHSEGMENT", 0.025, 0.035, 0.0,
            True, 0, callout, 0
        )
        model.SketchAddConstraints("sgPARALLEL")
        log("Parallel constraint applied", "SUCCESS")

        exit_sketch(model)
        model.ViewZoomtofit2()
        return True
    except Exception as e:
        log(f"sketch_constraint FAILED: {e}", "ERROR")
        traceback.print_exc()
        return False


@register_test("toggle_construction", "Toggle Construction Geometry", "Sketch Tools",
               "Draw a line and toggle it to construction geometry", order=10)
def test_toggle_construction(sw, template):
    try:
        model = new_sketch_on_front(sw, template)

        model.SketchManager.CreateLine(0.0, 0.0, 0.0, 0.05, 0.0, 0.0)
        log("Line drawn", "SUCCESS")

        callout = win32com.client.VARIANT(pythoncom.VT_DISPATCH, None)
        model.ClearSelection2(True)
        ok = model.Extension.SelectByID2(
            "", "SKETCHSEGMENT", 0.025, 0.0, 0.0,
            False, 0, callout, 0
        )
        if not ok:
            log("Could not select line", "ERROR")
            return False

        sel_mgr = model.SelectionManager
        sketch_seg = sel_mgr.GetSelectedObject6(1, -1)
        was_construction = sketch_seg.ConstructionGeometry
        sketch_seg.ConstructionGeometry = not was_construction
        is_construction = sketch_seg.ConstructionGeometry

        if is_construction == was_construction:
            log("Construction flag did not toggle", "ERROR")
            return False

        log(f"Toggled: was {was_construction}, now {is_construction}", "SUCCESS")

        exit_sketch(model)
        model.ViewZoomtofit2()
        return True
    except Exception as e:
        log(f"toggle_construction FAILED: {e}", "ERROR")
        traceback.print_exc()
        return False


@register_test("sketch_dimension", "Sketch Dimension", "Sketch Tools",
               "Draw a line, add a dimension, set its value, then modify it", order=11)
def test_sketch_dimension(sw, template):
    import threading
    from solidworks.sketching import dismiss_modify_dialog, no_dimension_value_prompt

    try:
        model = new_sketch_on_front(sw, template)

        # Draw a horizontal line 50mm long
        model.SketchManager.CreateLine(0.0, 0.0, 0.0, 0.05, 0.0, 0.0)
        log("Line created from (0,0) to (50,0) mm", "SUCCESS")

        # Select the line for dimensioning
        callout = win32com.client.VARIANT(pythoncom.VT_DISPATCH, None)
        model.ClearSelection2(True)
        ok = model.Extension.SelectByID2(
            "", "SKETCHSEGMENT", 0.025, 0.0, 0.0,
            False, 0, callout, 0
        )
        if not ok:
            log("Failed to select line for dimensioning", "ERROR")
            return False

        # Start thread to auto-dismiss the Modify dialog
        t = threading.Thread(target=dismiss_modify_dialog, daemon=True)
        t.start()

        sm = model.SketchManager
        sm.AddToDB = True
        sm.DisplayWhenAdded = False
        try:
            with no_dimension_value_prompt(sw):
                dim_display = model.AddDimension2(0.025, -0.01, 0.0)
        finally:
            sm.AddToDB = False
            sm.DisplayWhenAdded = True
            t.join(timeout=2)

        if not dim_display:
            log("AddDimension2 returned None", "ERROR")
            return False
        log("Dimension added (dialog auto-dismissed)", "SUCCESS")

        # Set dimension value to 80mm
        dim = dim_display.GetDimension2(0)
        if not dim:
            log("GetDimension2 returned None", "ERROR")
            return False
        dim.SetSystemValue3(0.08, 2, "")  # 80mm in meters
        model.ForceRebuild3(True)
        log("Dimension value set to 80mm", "SUCCESS")

        # Verify the dimension value was applied
        current_val = dim.GetSystemValue3(2, "")
        if isinstance(current_val, tuple):
            current_mm = current_val[0] * 1000.0
        else:
            current_mm = current_val * 1000.0
        if abs(current_mm - 80.0) > 0.01:
            log(f"Dimension value mismatch: expected 80, got {current_mm:.2f}", "ERROR")
            return False
        log(f"Dimension value verified: {current_mm:.1f}mm", "SUCCESS")

        # Test modifying the dimension value using the existing dim object
        dim.SetSystemValue3(0.06, 2, "")  # Change to 60mm
        model.ForceRebuild3(True)

        current_val2 = dim.GetSystemValue3(2, "")
        if isinstance(current_val2, tuple):
            current_mm2 = current_val2[0] * 1000.0
        else:
            current_mm2 = current_val2 * 1000.0
        if abs(current_mm2 - 60.0) > 0.01:
            log(f"Modified dimension mismatch: expected 60, got {current_mm2:.2f}", "ERROR")
            return False
        log(f"Dimension value modified and verified: {current_mm2:.1f}mm", "SUCCESS")

        exit_sketch(model)
        model.ViewZoomtofit2()
        return True
    except Exception as e:
        log(f"sketch_dimension FAILED: {e}", "ERROR")
        traceback.print_exc()
        return False


# ===========================================================================
# TEST FUNCTIONS — Feature Tools
# ===========================================================================

@register_test("mass_properties", "Mass Properties", "Feature Tools",
               "Create 100mm cube and verify volume/surface area", order=0)
def test_mass_properties(sw, template):
    try:
        model = sw.NewDocument(template, 0, 0, 0)
        front_plane = find_feature(model, "Front Plane")
        model.ClearSelection2(True)
        front_plane.Select2(False, 0)
        model.SketchManager.InsertSketch(True)
        model.SketchManager.CreateCornerRectangle(0.0, 0.0, 0.0, 0.1, 0.1, 0.0)
        exit_sketch(model)

        sketch1 = find_feature(model, "Sketch1")
        model.ClearSelection2(True)
        sketch1.Select2(False, 0)
        feat = model.FeatureManager.FeatureExtrusion2(
            True, False, False, 0, 0, 0.1, 0.0,
            False, False, False, False, 0.0, 0.0,
            False, False, False, False, True, True, True,
            0, 0, False
        )
        if not feat:
            log("Extrusion failed for mass properties test", "ERROR")
            return False
        log("100mm cube created", "SUCCESS")

        model.ForceRebuild3(True)
        props = model.GetMassProperties
        if not props or len(props) < 12:
            log("GetMassProperties returned invalid data", "ERROR")
            return False

        volume_mm3 = props[3] * 1e9
        surface_area_mm2 = props[4] * 1e6
        com_x = props[0] * 1000.0
        com_y = props[1] * 1000.0
        com_z = props[2] * 1000.0

        log(f"Volume: {volume_mm3:.0f} mm^3 (expected 1000000)", "SUCCESS")
        log(f"Surface Area: {surface_area_mm2:.0f} mm^2 (expected 60000)", "SUCCESS")
        log(f"Center of Mass: ({com_x:.1f}, {com_y:.1f}, {com_z:.1f}) mm", "SUCCESS")

        if abs(volume_mm3 - 1_000_000) > 1:
            log(f"Volume mismatch: expected 1000000, got {volume_mm3:.2f}", "ERROR")
            return False
        if abs(surface_area_mm2 - 60_000) > 1:
            log(f"Surface area mismatch: expected 60000, got {surface_area_mm2:.2f}", "ERROR")
            return False

        log("Mass properties validated", "SUCCESS")
        model.ViewZoomtofit2()
        return True
    except Exception as e:
        log(f"mass_properties FAILED: {e}", "ERROR")
        traceback.print_exc()
        return False


@register_test("cut_extrusion", "Cut Extrusion", "Feature Tools",
               "Create a cube then cut a circular hole in it", order=1)
def test_cut_extrusion(sw, template):
    try:
        model = sw.NewDocument(template, 0, 0, 0)
        front_plane = find_feature(model, "Front Plane")
        model.ClearSelection2(True)
        front_plane.Select2(False, 0)
        model.SketchManager.InsertSketch(True)
        model.SketchManager.CreateCornerRectangle(0.0, 0.0, 0.0, 0.1, 0.1, 0.0)
        exit_sketch(model)

        sketch1 = find_feature(model, "Sketch1")
        model.ClearSelection2(True)
        sketch1.Select2(False, 0)
        feat = model.FeatureManager.FeatureExtrusion2(
            True, False, False, 0, 0, 0.1, 0.0,
            False, False, False, False, 0.0, 0.0,
            False, False, False, False, True, True, True,
            0, 0, False
        )
        if not feat:
            log("Base extrusion failed", "ERROR")
            return False
        log("100mm cube created", "SUCCESS")

        callout = win32com.client.VARIANT(pythoncom.VT_DISPATCH, None)
        model.ClearSelection2(True)
        ok = model.Extension.SelectByID2(
            "", "FACE", 0.05, 0.05, 0.0,
            False, 0, callout, 0
        )
        if not ok:
            log("Could not select front face", "ERROR")
            return False

        model.SketchManager.InsertSketch(True)
        model.SketchManager.CreateCircleByRadius(0.05, 0.05, 0.0, 0.02)
        log("Circle drawn on front face for cut", "SUCCESS")

        model.ClearSelection2(True)
        model.SketchManager.InsertSketch(True)

        sketch2 = find_feature(model, "Sketch2")
        if not sketch2:
            log("Could not find Sketch2", "ERROR")
            return False
        model.ClearSelection2(True)
        sketch2.Select2(False, 0)

        cut_feat = model.FeatureManager.FeatureCut4(
            True, True, False, 0, 0, 0.05, 0.0,
            False, False, False, False, 0.0, 0.0,
            False, False, False, False, False, False, True,
            False, True, False, 0, 0.0, False, False
        )
        if not cut_feat:
            log("Cut-extrusion returned None", "ERROR")
            return False

        log("Cut-extrusion created (50mm deep hole)", "SUCCESS")
        model.ViewZoomtofit2()
        return True
    except Exception as e:
        log(f"cut_extrusion FAILED: {e}", "ERROR")
        traceback.print_exc()
        return False


@register_test("fillet", "Fillet", "Feature Tools",
               "Create a cube and fillet one edge (5mm)", order=2)
def test_fillet(sw, template):
    try:
        make_cube(sw, template, 100)
        model = sw.ActiveDoc
        model.ForceRebuild3(True)

        # Select a Z-direction edge (front-right, midpoint at z=0.05)
        callout = win32com.client.VARIANT(pythoncom.VT_DISPATCH, None)
        model.ClearSelection2(True)
        ok = model.Extension.SelectByID2(
            "", "EDGE", 0.1, 0.0, 0.05,
            False, 1, callout, 0
        )
        if not ok:
            log("Could not select edge for fillet", "ERROR")
            return False

        # Options=195 required for SolidWorks 2025+
        feature = model.FeatureManager.FeatureFillet3(
            195, 0.005, 0.0, 0.0, 0, 0, 0, 0,
        )
        if not feature:
            log("FeatureFillet3 returned None", "ERROR")
            return False

        log("Fillet created (5mm radius)", "SUCCESS")
        model.ViewZoomtofit2()
        return True
    except Exception as e:
        log(f"fillet FAILED: {e}", "ERROR")
        traceback.print_exc()
        return False


@register_test("chamfer", "Chamfer", "Feature Tools",
               "Create a cube and chamfer one edge (5mm)", order=3)
def test_chamfer(sw, template):
    try:
        make_cube(sw, template, 100)
        model = sw.ActiveDoc
        model.ForceRebuild3(True)

        # Select a Z-direction edge (front-right, midpoint at z=0.05)
        callout = win32com.client.VARIANT(pythoncom.VT_DISPATCH, None)
        model.ClearSelection2(True)
        ok = model.Extension.SelectByID2(
            "", "EDGE", 0.1, 0.0, 0.05,
            False, 0, callout, 0
        )
        if not ok:
            log("Could not select edge for chamfer", "ERROR")
            return False

        # 8 params: Options, ChamferType, Width, Angle, OtherDist, 3x vertex dist.
        # ChamferType 1 = distance-angle (45° == equal distance). Type 0/16
        # create a feature that removes no material on SW2022, so the volume
        # is checked — a non-None feature alone proved nothing.
        feature = model.FeatureManager.InsertFeatureChamfer(
            4, 1, 0.005, 0.785398, 0.005, 0, 0, 0,
        )
        if not feature:
            log("InsertFeatureChamfer returned None", "ERROR")
            return False

        model.ForceRebuild3(True)
        vol_mm3 = model.Extension.CreateMassProperty.Volume * 1e9
        expected = 100 ** 3 - 0.5 * 5 * 5 * 100
        if abs(vol_mm3 - expected) > 1:
            log(f"Chamfer volume {vol_mm3:.1f} != expected {expected:.1f}", "ERROR")
            return False

        log("Chamfer created (5mm), volume verified", "SUCCESS")
        model.ViewZoomtofit2()
        return True
    except Exception as e:
        log(f"chamfer FAILED: {e}", "ERROR")
        traceback.print_exc()
        return False


@register_test("shell", "Shell", "Feature Tools",
               "Create a cube and shell it (remove top face, 3mm)", order=4)
def test_shell(sw, template):
    try:
        make_cube(sw, template, 100)
        model = sw.ActiveDoc
        model.ForceRebuild3(True)

        # Select top face (y=100mm, center of face)
        callout = win32com.client.VARIANT(pythoncom.VT_DISPATCH, None)
        model.ClearSelection2(True)
        ok = model.Extension.SelectByID2(
            "", "FACE", 0.05, 0.1, 0.05,
            False, 0, callout, 0
        )
        if not ok:
            log("Could not select top face for shell", "ERROR")
            return False

        # In SW2025, InsertFeatureShell lives on the model (IModelDoc2),
        # NOT on FeatureManager. It may return None even on success,
        # so verify by checking the feature tree.
        model._FlagAsMethod("InsertFeatureShell")
        model.InsertFeatureShell(0.003, False)
        model.ForceRebuild3(True)

        # Verify shell was created by checking feature tree
        features = model.FeatureManager.GetFeatures(True)
        shell_found = False
        if features:
            for f in features:
                if f.GetTypeName2 == "Shell":  # type name is language-independent
                    shell_found = True
                    break
        if not shell_found:
            log("Shell feature not found in feature tree", "ERROR")
            return False

        log("Shell created (3mm thickness, top face removed)", "SUCCESS")
        model.ViewZoomtofit2()
        return True
    except Exception as e:
        log(f"shell FAILED: {e}", "ERROR")
        traceback.print_exc()
        return False


@register_test("revolve", "Revolve", "Feature Tools",
               "Create a half-profile with centerline and revolve 360 deg", order=5)
def test_revolve(sw, template):
    try:
        model = new_sketch_on_front(sw, template)

        model.SketchManager.CreateCenterLine(0.0, -0.03, 0.0, 0.0, 0.03, 0.0)
        model.SketchManager.CreateLine(0.01, -0.02, 0.0, 0.02, -0.02, 0.0)
        model.SketchManager.CreateLine(0.02, -0.02, 0.0, 0.02, 0.02, 0.0)
        model.SketchManager.CreateLine(0.02, 0.02, 0.0, 0.01, 0.02, 0.0)
        model.SketchManager.CreateLine(0.01, 0.02, 0.0, 0.01, -0.02, 0.0)
        log("Half-profile and centerline drawn", "SUCCESS")

        exit_sketch(model)

        sketch1 = find_feature(model, "Sketch1")
        model.ClearSelection2(True)
        sketch1.Select2(False, 0)

        feature = model.FeatureManager.FeatureRevolve2(
            True, True, False, False, False, False,
            0, 0,
            2 * math.pi,
            0.0,
            False, False, 0.0, 0.0,
            0, 0.0, 0.0,
            True, True, True,
        )
        if not feature:
            log("FeatureRevolve2 returned None", "ERROR")
            return False

        log("Revolve created (360\u00b0)", "SUCCESS")
        model.ViewZoomtofit2()
        return True
    except Exception as e:
        log(f"revolve FAILED: {e}", "ERROR")
        traceback.print_exc()
        return False


@register_test("ref_plane", "Reference Plane", "Feature Tools",
               "Create a 50mm offset reference plane from Front", order=6)
def test_ref_plane(sw, template):
    try:
        make_cube(sw, template, 100)
        model = sw.ActiveDoc
        model.ForceRebuild3(True)

        # In SW2025, SelectByID2 with type "PLANE" works (not "DATUMPLANE")
        callout = win32com.client.VARIANT(pythoncom.VT_DISPATCH, None)
        model.ClearSelection2(True)
        ok = model.Extension.SelectByID2(N(model, "Front Plane"), "PLANE", 0, 0, 0,
            False, 0, callout, 0
        )
        if not ok:
            log("Could not select Front Plane", "ERROR")
            return False

        # swRefPlaneReferenceConstraints_e is a bit-flag enum (swconst.tlb):
        # Parallel=1, Coincident=4, Distance=8. The old value 5 meant
        # Parallel+Coincident — the plane landed ON Front and the distance
        # was ignored, which silently broke the loft test below.
        feature = model.FeatureManager.InsertRefPlane(8, 0.05, 0, 0, 0, 0)

        # InsertRefPlane may return None even on success; check feature tree
        if not feature:
            model.ForceRebuild3(True)
            features = model.FeatureManager.GetFeatures(True)
            if features:
                for f in features:
                    if f.GetTypeName2 == "RefPlane" and not is_standard_plane(model, f):
                        feature = f
                        break

        if not feature:
            log("InsertRefPlane returned None and no new plane in feature tree", "ERROR")
            return False

        # Verify the offset really is 50mm (a coincident plane also "exists")
        from solidworks.com_utils import com_prop
        params = list(com_prop(com_prop(feature, "GetSpecificFeature2"), "GetRefPlaneParams"))
        z_mm = abs(params[2]) * 1000.0  # origin z; Front plane normal is model Z
        if abs(z_mm - 50.0) > 0.01:
            log(f"Reference plane is at {z_mm:.3f}mm from Front, expected 50mm", "ERROR")
            return False

        log("Reference plane created (50mm offset from Front)", "SUCCESS")
        model.ViewZoomtofit2()
        return True
    except Exception as e:
        log(f"ref_plane FAILED: {e}", "ERROR")
        traceback.print_exc()
        return False


@register_test("loft", "Loft (Frustum)", "Feature Tools",
               "Create a frustum via loft between two circles on offset planes", order=7)
def test_loft(sw, template):
    try:
        model = sw.NewDocument(template, 0, 0, 0)

        # Sketch 1: large circle on Front Plane
        create_sketch_on_plane(model, "Front Plane")
        model.SketchManager.CreateCircleByRadius(0.0, 0.0, 0.0, 0.025)  # 25mm radius
        exit_sketch(model)
        sketch1_name = get_latest_sketch_name(model)
        log(f"Sketch 1: {sketch1_name}", "SUCCESS")

        # Create offset reference plane (80mm from Front)
        # Use SelectByID2 with "PLANE" type and flags=8 (Distance)
        callout = win32com.client.VARIANT(pythoncom.VT_DISPATCH, None)
        model.ClearSelection2(True)
        ok = model.Extension.SelectByID2(N(model, "Front Plane"), "PLANE", 0, 0, 0, False, 0, callout, 0
        )
        if not ok:
            log("Could not select Front Plane for ref plane", "ERROR")
            return False
        ref_feat = model.FeatureManager.InsertRefPlane(8, 0.08, 0, 0, 0, 0)
        if not ref_feat:
            # Fallback: check feature tree for a new plane
            model.ForceRebuild3(True)
            features = model.FeatureManager.GetFeatures(True)
            if features:
                for f in features:
                    if f.GetTypeName2 == "RefPlane" and not is_standard_plane(model, f):
                        ref_feat = f
                        break
        if not ref_feat:
            log("Failed to create reference plane", "ERROR")
            return False
        custom_plane_name = ref_feat.Name
        log(f"Reference plane: {custom_plane_name}", "SUCCESS")

        # Sketch 2: small circle on custom plane
        plane_feat = model.FeatureByName(custom_plane_name)
        if not plane_feat:
            log(f"Could not find plane: {custom_plane_name}", "ERROR")
            return False
        model.ClearSelection2(True)
        plane_feat.Select2(False, 0)
        model.SketchManager.InsertSketch(True)
        model.SketchManager.CreateCircleByRadius(0.0, 0.0, 0.0, 0.01)  # 10mm radius
        exit_sketch(model)
        sketch2_name = get_latest_sketch_name(model)
        log(f"Sketch 2: {sketch2_name}", "SUCCESS")

        # Select both sketches for loft (mark=1)
        callout = win32com.client.VARIANT(pythoncom.VT_DISPATCH, None)
        model.ClearSelection2(True)
        ok1 = model.Extension.SelectByID2(
            sketch1_name, "SKETCH", 0, 0, 0, False, 1, callout, 0
        )
        ok2 = model.Extension.SelectByID2(
            sketch2_name, "SKETCH", 0, 0, 0, True, 1, callout, 0
        )
        if not ok1 or not ok2:
            log(f"Sketch selection failed: {sketch1_name}={ok1}, {sketch2_name}={ok2}", "ERROR")
            return False

        # Create loft using InsertProtrusionBlend2 (18 params for SW2025)
        feature = model.FeatureManager.InsertProtrusionBlend2(
            False,  # Closed
            True,   # KeepTangency
            True,   # ForceNonRational
            1.0,    # TessToleranceFactor
            0,      # StartMatchingType (None)
            0,      # EndMatchingType (None)
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
            log("InsertProtrusionBlend2 returned None", "ERROR")
            return False

        log("Loft (frustum) created successfully", "SUCCESS")
        model.ViewZoomtofit2()
        return True
    except Exception as e:
        log(f"loft FAILED: {e}", "ERROR")
        traceback.print_exc()
        return False


@register_test("list_features", "List Features", "Feature Tools",
               "Create a cube and list the feature tree", order=8)
def test_list_features(sw, template):
    try:
        model = make_cube(sw, template, 100)

        features = model.FeatureManager.GetFeatures(True)
        if not features:
            log("GetFeatures returned None", "ERROR")
            return False

        found_extrude = False
        for feature in features:
            name = feature.Name
            type_name = feature.GetTypeName2
            if "Extrusion" in type_name or "Extrude" in name:
                found_extrude = True

        if not found_extrude:
            log("Could not find extrusion feature in tree", "ERROR")
            return False

        log(f"Feature tree has {len(features)} features, extrusion found", "SUCCESS")
        return True
    except Exception as e:
        log(f"list_features FAILED: {e}", "ERROR")
        traceback.print_exc()
        return False


# ===========================================================================
# TEST FUNCTIONS — MCP Tools (exercise the full MCP _route_tool dispatch)
# ===========================================================================


def create_mcp_server():
    """Create a SolidWorksMCPServer instance for MCP-level testing."""
    from server import SolidWorksMCPServer
    return SolidWorksMCPServer()


def mcp_make_cube(server, size_mm=100):
    """Create a cube end-to-end via MCP tool calls. Returns list of result strings.

    Note: create_extrusion exits sketch mode internally, so we do NOT call
    exit_sketch before it (that would toggle sketch mode back on).
    """
    results = []
    results.append(server._route_tool("solidworks_new_part", {}))
    results.append(server._route_tool("solidworks_create_sketch", {"plane": "Front"}))
    results.append(server._route_tool("solidworks_sketch_rectangle", {
        "centerX": size_mm / 2, "centerY": size_mm / 2,
        "width": size_mm, "height": size_mm
    }))
    results.append(server._route_tool("solidworks_create_extrusion", {"depth": size_mm}))
    return results


def _model_to_sketch_xy(sketch_result_json, mx, my, mz):
    """Convert a model-space point (mm) to sketch coordinates using the
    sketchFrame returned by create_sketch (face sketches can have mirrored or
    rotated axes)."""
    frame = json.loads(sketch_result_json).get("sketchFrame")
    if not frame:
        return mx, my  # plane sketches without frame info: assume aligned
    o = frame["originModel_mm"]
    d = (mx - o[0], my - o[1], mz - o[2])
    xa, ya = frame["xAxisModel"], frame["yAxisModel"]
    sx = xa[0] * d[0] + xa[1] * d[1] + xa[2] * d[2]
    sy = ya[0] * d[0] + ya[1] * d[1] + ya[2] * d[2]
    return sx, sy


def _mcp_ok(r):
    """True if an MCP result indicates success.
    Tools return JSON strings {"result": "✓ ..."}; errors are plain ❌ strings."""
    if r.startswith("❌"):
        return False
    if r.startswith("{"):
        try:
            return json.loads(r).get("result", "").startswith("✓")
        except Exception:
            return False
    return r.startswith("✓")


def mcp_check_results(results):
    """Verify all MCP results indicate success. Returns True if all pass."""
    for r in results:
        if not _mcp_ok(r):
            log(f"MCP call failed: {r}", "ERROR")
            return False
    return True


@register_test("mcp_basic_workflow", "MCP Basic Workflow", "MCP Tools",
               "Create a 100mm cube end-to-end via MCP tool calls", order=0)
def test_mcp_basic_workflow(sw, template):
    try:
        server = create_mcp_server()
        results = mcp_make_cube(server, 100)
        if not mcp_check_results(results):
            return False

        extrude_result = results[-1]
        if "Boss-Extrude" not in extrude_result and "Extrusion" not in extrude_result:
            log(f"Extrusion result missing feature name: {extrude_result}", "ERROR")
            return False

        log("MCP basic workflow (cube) succeeded", "SUCCESS")
        return True
    except Exception as e:
        log(f"mcp_basic_workflow FAILED: {e}", "ERROR")
        traceback.print_exc()
        return False


@register_test("mcp_fillet", "MCP Fillet", "MCP Tools",
               "Create cube via MCP, then fillet an edge", order=1)
def test_mcp_fillet(sw, template):
    try:
        server = create_mcp_server()
        results = mcp_make_cube(server, 100)
        if not mcp_check_results(results):
            return False

        r = server._route_tool("solidworks_fillet", {
            "radius": 5,
            "edges": [{"x": 100, "y": 0, "z": 50}]
        })
        if not _mcp_ok(r):
            log(f"Fillet failed: {r}", "ERROR")
            return False
        if "Fillet" not in r:
            log(f"Fillet result missing feature name: {r}", "ERROR")
            return False

        log("MCP fillet succeeded", "SUCCESS")
        return True
    except Exception as e:
        log(f"mcp_fillet FAILED: {e}", "ERROR")
        traceback.print_exc()
        return False


@register_test("mcp_chamfer", "MCP Chamfer", "MCP Tools",
               "Create cube via MCP, then chamfer an edge", order=2)
def test_mcp_chamfer(sw, template):
    try:
        server = create_mcp_server()
        results = mcp_make_cube(server, 100)
        if not mcp_check_results(results):
            return False

        r = server._route_tool("solidworks_chamfer", {
            "distance": 5,
            "edges": [{"x": 100, "y": 0, "z": 50}]
        })
        if not _mcp_ok(r):
            log(f"Chamfer failed: {r}", "ERROR")
            return False
        vol = _mcp_json(server._route_tool("solidworks_get_mass_properties", {}))["volume_mm3"]
        expected = 100 ** 3 - 0.5 * 5 * 5 * 100
        if abs(vol - expected) > 1:
            log(f"Chamfer volume {vol} != expected {expected}", "ERROR")
            return False

        log("MCP chamfer succeeded, volume verified", "SUCCESS")
        return True
    except Exception as e:
        log(f"mcp_chamfer FAILED: {e}", "ERROR")
        traceback.print_exc()
        return False


@register_test("mcp_shell", "MCP Shell", "MCP Tools",
               "Create cube via MCP, then shell (remove top face)", order=3)
def test_mcp_shell(sw, template):
    try:
        server = create_mcp_server()
        results = mcp_make_cube(server, 100)
        if not mcp_check_results(results):
            return False

        r = server._route_tool("solidworks_shell", {
            "thickness": 3,
            "facesToRemove": [{"x": 50, "y": 100, "z": 50}]
        })
        if not _mcp_ok(r):
            log(f"Shell failed: {r}", "ERROR")
            return False
        if "Shell" not in r:
            log(f"Shell result missing feature name: {r}", "ERROR")
            return False

        log("MCP shell succeeded", "SUCCESS")
        return True
    except Exception as e:
        log(f"mcp_shell FAILED: {e}", "ERROR")
        traceback.print_exc()
        return False


@register_test("mcp_cut_extrusion", "MCP Cut-Extrusion", "MCP Tools",
               "Create cube via MCP, then cut a circle through it", order=4)
def test_mcp_cut_extrusion(sw, template):
    try:
        server = create_mcp_server()
        results = mcp_make_cube(server, 100)
        if not mcp_check_results(results):
            return False

        # Sketch on the back face of the cube (z=0 plane) — its sketch X axis is
        # mirrored, so convert the model target through the returned sketchFrame
        sk_r = server._route_tool("solidworks_create_sketch", {
            "faceX": 50, "faceY": 50, "faceZ": 0
        })
        if not _mcp_ok(sk_r):
            log(f"Sketch on face failed: {sk_r}", "ERROR")
            return False

        cx, cy = _model_to_sketch_xy(sk_r, 50, 50, 0)
        r = server._route_tool("solidworks_sketch_circle", {
            "centerX": cx, "centerY": cy, "radius": 20
        })
        if not _mcp_ok(r):
            log(f"Circle sketch failed: {r}", "ERROR")
            return False

        # exit_sketch is required before create_cut_extrusion
        r = server._route_tool("solidworks_exit_sketch", {})
        if not _mcp_ok(r):
            log(f"Exit sketch failed: {r}", "ERROR")
            return False

        # Direction is auto-flipped into the body when needed
        r = server._route_tool("solidworks_create_cut_extrusion", {"depth": 50})
        if not _mcp_ok(r):
            log(f"Cut-extrusion failed: {r}", "ERROR")
            return False
        if "Cut" not in r:
            log(f"Cut result missing feature name: {r}", "ERROR")
            return False

        # The cut must have removed the cylinder's volume (catches wrong-side cuts)
        mp = _mcp_json(server._route_tool("solidworks_get_mass_properties", {}))
        expected = 100**3 - math.pi * 20**2 * 50
        if abs(mp["volume_mm3"] - expected) > 1:
            log(f"Cut volume wrong: {mp['volume_mm3']} != {expected:.0f}", "ERROR")
            return False

        log("MCP cut-extrusion succeeded with correct volume", "SUCCESS")
        return True
    except Exception as e:
        log(f"mcp_cut_extrusion FAILED: {e}", "ERROR")
        traceback.print_exc()
        return False


@register_test("mcp_mass_properties", "MCP Mass Properties", "MCP Tools",
               "Create cube via MCP, then verify volume via mass properties", order=5)
def test_mcp_mass_properties(sw, template):
    try:
        server = create_mcp_server()
        results = mcp_make_cube(server, 100)
        if not mcp_check_results(results):
            return False

        r = server._route_tool("solidworks_get_mass_properties", {})
        text = json.loads(r)["result"] if r.startswith("{") else r
        if "Volume" not in text:
            log(f"Mass properties missing volume: {text}", "ERROR")
            return False

        for line in text.split("\n"):
            if "Volume:" in line:
                vol_str = line.split(":")[1].strip().split(" ")[0]
                volume = float(vol_str)
                if abs(volume - 1_000_000) > 1:
                    log(f"Volume mismatch: expected ~1000000, got {volume}", "ERROR")
                    return False
                log(f"Volume verified: {volume:.0f} mm^3", "SUCCESS")
                break

        log("MCP mass properties succeeded", "SUCCESS")
        return True
    except Exception as e:
        log(f"mcp_mass_properties FAILED: {e}", "ERROR")
        traceback.print_exc()
        return False


@register_test("mcp_list_features", "MCP List Features", "MCP Tools",
               "Create cube via MCP, then list features and verify extrusion exists", order=6)
def test_mcp_list_features(sw, template):
    try:
        server = create_mcp_server()
        results = mcp_make_cube(server, 100)
        if not mcp_check_results(results):
            return False

        r = server._route_tool("solidworks_list_features", {})
        if "Feature Tree" not in r and "feature" not in r.lower():
            log(f"Unexpected list_features result: {r}", "ERROR")
            return False
        if "Extrusion" not in r and "Extrude" not in r:
            log(f"Feature tree missing extrusion: {r}", "ERROR")
            return False

        log("MCP list features succeeded", "SUCCESS")
        return True
    except Exception as e:
        log(f"mcp_list_features FAILED: {e}", "ERROR")
        traceback.print_exc()
        return False


@register_test("mcp_full_integration", "MCP Full Integration", "MCP Tools",
               "Multi-step MCP workflow: cube + fillet + cut + list_features + mass_properties",
               order=7)
def test_mcp_full_integration(sw, template):
    try:
        server = create_mcp_server()

        # Step 1: Create cube
        results = mcp_make_cube(server, 100)
        if not mcp_check_results(results):
            return False
        log("Step 1: Cube created", "SUCCESS")

        # Step 2: Fillet one edge
        r = server._route_tool("solidworks_fillet", {
            "radius": 5,
            "edges": [{"x": 100, "y": 0, "z": 50}]
        })
        if not _mcp_ok(r):
            log(f"Fillet failed: {r}", "ERROR")
            return False
        log("Step 2: Fillet created", "SUCCESS")

        # Step 3: Cut-extrusion (hole on top face). The top face's sketch frame is
        # rotated (sketch Y = model -Z), so convert the model target through it.
        sk_r = server._route_tool("solidworks_create_sketch", {
            "faceX": 50, "faceY": 100, "faceZ": 50
        })
        if not _mcp_ok(sk_r):
            log(f"Sketch on face failed: {sk_r}", "ERROR")
            return False

        cx, cy = _model_to_sketch_xy(sk_r, 50, 100, 50)
        r = server._route_tool("solidworks_sketch_circle", {
            "centerX": cx, "centerY": cy, "radius": 15
        })
        if not _mcp_ok(r):
            log(f"Circle failed: {r}", "ERROR")
            return False

        r = server._route_tool("solidworks_exit_sketch", {})
        if not _mcp_ok(r):
            log(f"Exit sketch failed: {r}", "ERROR")
            return False

        # Cut into the body (direction auto-flips if needed)
        r = server._route_tool("solidworks_create_cut_extrusion", {"depth": 50})
        if not _mcp_ok(r):
            log(f"Cut-extrusion failed: {r}", "ERROR")
            return False
        log("Step 3: 50mm hole cut created", "SUCCESS")

        # Step 4: List features (should have Boss-Extrude, Fillet, Cut-Extrude)
        r = server._route_tool("solidworks_list_features", {})
        if "Fillet" not in r:
            log(f"Feature tree missing Fillet: {r}", "ERROR")
            return False
        log("Step 4: Feature tree verified", "SUCCESS")

        # Step 5: Mass properties (volume should be less than 1,000,000)
        r = server._route_tool("solidworks_get_mass_properties", {})
        text = json.loads(r)["result"] if r.startswith("{") else r
        if "Volume" not in text:
            log(f"Mass properties missing volume: {text}", "ERROR")
            return False
        for line in text.split("\n"):
            if "Volume:" in line:
                vol_str = line.split(":")[1].strip().split(" ")[0]
                volume = float(vol_str)
                if volume >= 1_000_000:
                    log(f"Volume should be < 1M after cuts, got {volume}", "ERROR")
                    return False
                log(f"Step 5: Volume {volume:.0f} mm^3 (correctly less than original)", "SUCCESS")
                break

        log("MCP full integration passed", "SUCCESS")
        return True
    except Exception as e:
        log(f"mcp_full_integration FAILED: {e}", "ERROR")
        traceback.print_exc()
        return False


# ===========================================================================
# TEST FUNCTIONS — MCP Geometry Query Tools
# ===========================================================================


@register_test("mcp_get_body_info", "MCP Get Body Info", "MCP Tools",
               "Create cube via MCP, query bounding box and counts", order=8)
def test_mcp_get_body_info(sw, template):
    try:
        server = create_mcp_server()
        results = mcp_make_cube(server, 100)
        if not mcp_check_results(results):
            return False

        r = server._route_tool("solidworks_get_body_info", {})
        if not _mcp_ok(r):
            log(f"get_body_info failed: {r}", "ERROR")
            return False

        # Verify counts (structured JSON return)
        d = json.loads(r)
        if d.get("faces") != 6:
            log(f"Expected 6 faces, got: {d.get('faces')}", "ERROR")
            return False
        if d.get("edges") != 12:
            log(f"Expected 12 edges, got: {d.get('edges')}", "ERROR")
            return False
        if d.get("vertices") != 8:
            log(f"Expected 8 vertices, got: {d.get('vertices')}", "ERROR")
            return False
        if d.get("size", {}).get("x") != 100.0:
            log(f"Expected 100mm bounding box, got: {d.get('size')}", "ERROR")
            return False

        log("Body info: 6 faces, 12 edges, 8 vertices confirmed", "SUCCESS")
        return True
    except Exception as e:
        log(f"mcp_get_body_info FAILED: {e}", "ERROR")
        traceback.print_exc()
        return False


@register_test("mcp_get_faces", "MCP Get Faces", "MCP Tools",
               "Create cube via MCP, enumerate all faces", order=9)
def test_mcp_get_faces(sw, template):
    try:
        server = create_mcp_server()
        results = mcp_make_cube(server, 100)
        if not mcp_check_results(results):
            return False

        r = server._route_tool("solidworks_get_faces", {})
        if not _mcp_ok(r):
            log(f"get_faces failed: {r}", "ERROR")
            return False

        faces = json.loads(r)["faces"]
        if len(faces) != 6:
            log(f"Expected 6 total faces, got {len(faces)}", "ERROR")
            return False

        planar_count = sum(1 for f in faces if f.get("surfaceType") == "Planar")
        if planar_count != 6:
            log(f"Expected 6 planar faces, found {planar_count}", "ERROR")
            return False

        point_count = sum(1 for f in faces if "point" in f)
        if point_count != 6:
            log(f"Expected 6 sample points, found {point_count}", "ERROR")
            return False

        if not all(f.get("feature") for f in faces):
            log("Faces missing feature tags", "ERROR")
            return False

        log("6 planar faces with sample points and feature tags confirmed", "SUCCESS")
        return True
    except Exception as e:
        log(f"mcp_get_faces FAILED: {e}", "ERROR")
        traceback.print_exc()
        return False


@register_test("mcp_get_edges", "MCP Get Edges", "MCP Tools",
               "Create cube via MCP, enumerate all edges", order=10)
def test_mcp_get_edges(sw, template):
    try:
        server = create_mcp_server()
        results = mcp_make_cube(server, 100)
        if not mcp_check_results(results):
            return False

        r = server._route_tool("solidworks_get_edges", {})
        if not _mcp_ok(r):
            log(f"get_edges failed: {r}", "ERROR")
            return False

        edges = json.loads(r)["edges"]
        if len(edges) != 12:
            log(f"Expected 12 total edges, got {len(edges)}", "ERROR")
            return False

        line_count = sum(1 for e in edges if e.get("edgeType") == "Line")
        if line_count != 12:
            log(f"Expected 12 line edges, found {line_count}", "ERROR")
            return False

        mid_count = sum(1 for e in edges if "midpoint" in e)
        if mid_count != 12:
            log(f"Expected 12 midpoints, found {mid_count}", "ERROR")
            return False

        log("12 line edges with midpoints confirmed", "SUCCESS")
        return True
    except Exception as e:
        log(f"mcp_get_edges FAILED: {e}", "ERROR")
        traceback.print_exc()
        return False


@register_test("mcp_get_face_edges", "MCP Get Face Edges", "MCP Tools",
               "Create cube via MCP, query edges of the top face", order=11)
def test_mcp_get_face_edges(sw, template):
    try:
        server = create_mcp_server()
        results = mcp_make_cube(server, 100)
        if not mcp_check_results(results):
            return False

        # Query top face (y=100 for a cube sketched at center=(50,50) extruded 100mm)
        r = server._route_tool("solidworks_get_face_edges", {
            "x": 50, "y": 100, "z": 50
        })
        if not _mcp_ok(r):
            log(f"get_face_edges failed: {r}", "ERROR")
            return False

        face = json.loads(r)["face"]
        if len(face.get("edges", [])) != 4:
            log(f"Expected 4 edges on top face: {face.get('edges')}", "ERROR")
            return False

        if face.get("surfaceType") != "Planar":
            log(f"Expected Planar face type: {face.get('surfaceType')}", "ERROR")
            return False

        log("Top face: Planar with 4 edges confirmed", "SUCCESS")
        return True
    except Exception as e:
        log(f"mcp_get_face_edges FAILED: {e}", "ERROR")
        traceback.print_exc()
        return False


@register_test("mcp_geometry_guided_fillet", "MCP Geometry-Guided Fillet", "MCP Tools",
               "Create cube, use get_edges to find edge coordinates, then fillet using those coords",
               order=12)
def test_mcp_geometry_guided_fillet(sw, template):
    """Integration test: proves geometry query tools provide coordinates
    that actually work for downstream selection operations."""
    try:
        server = create_mcp_server()
        results = mcp_make_cube(server, 100)
        if not mcp_check_results(results):
            return False
        log("Step 1: Cube created", "SUCCESS")

        # Step 2: Query edges to find midpoints
        r = server._route_tool("solidworks_get_edges", {})
        if not _mcp_ok(r):
            log(f"get_edges failed: {r}", "ERROR")
            return False
        log("Step 2: Edges queried", "SUCCESS")

        # Step 3: Parse midpoints and pick one that's clearly on an edge midpoint
        # (avoid vertex intersections where multiple edges meet at a corner)
        all_mids = [e["midpoint"] for e in json.loads(r)["edges"] if "midpoint" in e]
        if not all_mids:
            log("Could not parse any midpoints from get_edges result", "ERROR")
            return False

        # Find a midpoint that's clearly on an edge but NOT at the origin
        # (origin planes can interfere with edge selection at x=0 or y=0)
        mid_x, mid_y, mid_z = None, None, None
        for mid in all_mids:
            coords = [mid["x"], mid["y"], mid["z"]]
            # Skip points where any coordinate is 0 (near origin planes)
            if any(abs(c) < 1 for c in coords):
                continue
            mid_x, mid_y, mid_z = coords[0], coords[1], coords[2]
            break

        if mid_x is None:
            # Fallback: use a known-good edge midpoint for a 100mm cube
            mid_x, mid_y, mid_z = 100.0, 50.0, 100.0

        log(f"Step 3: Parsed midpoint ({mid_x:.2f}, {mid_y:.2f}, {mid_z:.2f})", "SUCCESS")

        # Step 4: Use that midpoint to fillet the edge
        r = server._route_tool("solidworks_fillet", {
            "radius": 5,
            "edges": [{"x": mid_x, "y": mid_y, "z": mid_z}]
        })
        if not _mcp_ok(r):
            log(f"Fillet failed: {r}", "ERROR")
            return False
        log("Step 4: Fillet created using geometry-queried coordinates", "SUCCESS")

        log("Geometry-guided fillet workflow passed!", "SUCCESS")
        return True
    except Exception as e:
        log(f"mcp_geometry_guided_fillet FAILED: {e}", "ERROR")
        traceback.print_exc()
        return False


@register_test("mcp_sketch_dimension", "MCP Sketch Dimension", "MCP Tools",
               "Use SketchingTools methods to draw, dimension, and modify", order=13)
def test_mcp_sketch_dimension(sw, template):
    from solidworks.connection import SolidWorksConnection
    from solidworks.sketching import SketchingTools

    try:
        # Wire up a SolidWorksConnection that reuses the existing sw instance
        conn = SolidWorksConnection()
        conn.app = sw
        conn.template_path = template
        sketching = SketchingTools(conn)

        # create_sketch auto-creates a part when none is open
        result = sketching.create_sketch({"plane": "Front"})
        log(result, "SUCCESS")

        # Draw a 50mm horizontal line via sketch_line
        result = sketching.sketch_line({"x1": 0, "y1": 0, "x2": 50, "y2": 0})
        log(result, "SUCCESS")

        # Add a dimension on the line (no value yet — keeps geometry stable)
        result = sketching.sketch_dimension({
            "entityPoints": [{"x": 25, "y": 0}],
            "dimX": 25, "dimY": -10,
        })
        log(result, "SUCCESS")

        # Modify the dimension to 80mm via set_dimension_value
        result = sketching.set_dimension_value({
            "dimX": 25, "dimY": -10,
            "value": 80
        })
        log(result, "SUCCESS")

        if "80" not in result:
            log(f"Expected '80' in result: {result}", "ERROR")
            return False

        # Exit sketch
        result = sketching.exit_sketch()
        log(result, "SUCCESS")

        model = conn.get_active_doc()
        model.ViewZoomtofit2()
        return True
    except Exception as e:
        log(f"mcp_sketch_dimension FAILED: {e}", "ERROR")
        traceback.print_exc()
        return False


@register_test("mcp_angular_dimension", "MCP Angular Dimension", "MCP Tools",
               "Draw two lines, add angular dimension in degrees, verify conversion", order=14)
def test_mcp_angular_dimension(sw, template):
    from solidworks.connection import SolidWorksConnection
    from solidworks.sketching import SketchingTools

    try:
        conn = SolidWorksConnection()
        conn.app = sw
        conn.template_path = template
        sketching = SketchingTools(conn)

        # Create sketch on Front plane
        result = sketching.create_sketch({"plane": "Front"})
        log(result, "SUCCESS")

        # Draw two lines from origin — roughly 60 degrees apart
        # Line 1: along X axis
        result = sketching.sketch_line({"x1": 0, "y1": 0, "x2": 50, "y2": 0})
        log(result, "SUCCESS")

        # Line 2: from origin at ~60 degrees
        result = sketching.sketch_line({"x1": 0, "y1": 0, "x2": 25, "y2": 43.3})
        log(result, "SUCCESS")

        # Add angular dimension between the two lines, set to 45 degrees
        result = sketching.sketch_dimension({
            "entityPoints": [
                {"x": 25, "y": 0},       # point on line 1
                {"x": 12.5, "y": 21.65}  # point on line 2
            ],
            "dimX": 20, "dimY": 15,
            "value": 45
        })
        log(result, "SUCCESS")

        # Verify the result message says degrees (°), not mm.
        # Results are JSON strings with unicode escaped -- parse before checking.
        result_text = json.loads(result)["result"] if result.startswith("{") else result
        if "\u00b0" not in result_text:
            log(f"Expected '\u00b0' in angular dimension result: {result_text}", "ERROR")
            return False
        log("Angular dimension correctly reports degrees", "SUCCESS")

        # Exit sketch
        result = sketching.exit_sketch()
        log(result, "SUCCESS")

        model = conn.get_active_doc()
        model.ViewZoomtofit2()
        return True
    except Exception as e:
        log(f"mcp_angular_dimension FAILED: {e}", "ERROR")
        traceback.print_exc()
        return False


@register_test("mcp_linear_pattern", "MCP Linear Pattern", "MCP Tools",
               "Plate + hole, pattern 3x in X and 2x in Y by axis name, verify volume", order=15)
def test_mcp_linear_pattern(sw, template):
    """Covers FeatureLinearPattern4 (20-param signature, probed live on
    SW2025) and axis-name direction selection — this tool shipped broken
    once because nothing in the suite exercised it."""
    try:
        server = create_mcp_server()

        # 80x40x5 plate with a 5mm hole at center. Both pattern directions
        # fit on the plate regardless of which way SolidWorks runs them.
        results = [
            server._route_tool("solidworks_new_part", {}),
            server._route_tool("solidworks_create_sketch", {"plane": "Front"}),
            server._route_tool("solidworks_sketch_rectangle", {
                "width": 80, "height": 40, "centerX": 0, "centerY": 0}),
            server._route_tool("solidworks_create_extrusion", {"depth": 5}),
            server._route_tool("solidworks_create_sketch", {"plane": "Front"}),
            server._route_tool("solidworks_sketch_circle", {
                "radius": 2.5, "centerX": 0, "centerY": 0}),
        ]
        cut_r = server._route_tool("solidworks_create_cut_extrusion", {
            "depth": 5, "endCondition": "THROUGH_ALL"})
        results.append(cut_r)
        if not mcp_check_results(results):
            return False
        # Feature numbering is shared across the Extrude family (the cut may
        # be Cut-Extrude2) — always use the returned id, never assume a name.
        cut_id = _mcp_json(cut_r)["id"]

        r = server._route_tool("solidworks_linear_pattern", {
            "features": [cut_id],
            "direction1": {"axis": "X"}, "spacing1": 12, "count1": 3,
            "direction2": {"axis": "Y"}, "spacing2": 12, "count2": 2,
        })
        if not _mcp_ok(r):
            log(f"Linear pattern failed: {r}", "ERROR")
            return False

        # 6 holes total -> 6 cylindrical faces
        faces = _mcp_json(server._route_tool("solidworks_get_faces",
                                             {"surfaceType": "CYLINDER"}))["faces"]
        if len(faces) != 6:
            log(f"Expected 6 cylindrical faces after 3x2 pattern, got {len(faces)}", "ERROR")
            return False

        # Volume: 80*40*5 minus 6 holes of pi * 2.5^2 * 5
        expected = 80 * 40 * 5 - 6 * math.pi * 2.5 ** 2 * 5
        vol = _mcp_json(server._route_tool("solidworks_get_mass_properties", {}))["volume_mm3"]
        if abs(vol - expected) > 1.0:
            log(f"Volume {vol} != expected {expected:.2f}", "ERROR")
            return False

        log(f"Linear pattern 3x2 by axis names: 6 holes, volume {vol} ≈ {expected:.2f}", "SUCCESS")
        return True
    except Exception as e:
        log(f"mcp_linear_pattern FAILED: {e}", "ERROR")
        traceback.print_exc()
        return False


# ===========================================================================

# ===========================================================================
# TEST FUNCTIONS — Assembly (document lifecycle + assembly tools via MCP routing)
# ===========================================================================


@register_test("mcp_extrusion_reverse", "MCP Extrusion Reverse", "MCP Tools",
               "reverse=true must put the body BEHIND the sketch plane (centroid z<0)", order=16)
def test_mcp_extrusion_reverse(sw, template):
    """reverse used to be passed to FeatureExtrusion2's Flip (a cut-only
    option), so the body silently stayed in front of the plane."""
    try:
        server = create_mcp_server()
        results = [
            server._route_tool("solidworks_new_part", {}),
            server._route_tool("solidworks_create_sketch", {"plane": "Front"}),
            server._route_tool("solidworks_sketch_rectangle", {
                "width": 40, "height": 40, "centerX": 0, "centerY": 0}),
            server._route_tool("solidworks_create_extrusion", {"depth": 50, "reverse": True}),
        ]
        if not mcp_check_results(results):
            return False
        mp = _mcp_json(server._route_tool("solidworks_get_mass_properties", {}))
        z = mp["centerOfMass_mm"]["z"]
        if abs(mp["volume_mm3"] - 80000) > 1 or abs(z + 25) > 0.01:
            log(f"Reverse extrusion wrong: volume {mp['volume_mm3']}, centroid z {z} (expected 80000, -25)", "ERROR")
            return False
        log("Reverse extrusion went behind the Front plane", "SUCCESS")
        return True
    except Exception as e:
        log(f"mcp_extrusion_reverse FAILED: {e}", "ERROR")
        traceback.print_exc()
        return False


@register_test("mcp_polygon_inscribed", "MCP Polygon Inscribed vs Circumscribed", "MCP Tools",
               "inscribed=true measures the radius to the side midpoint; verify both by volume", order=17)
def test_mcp_polygon_inscribed(sw, template):
    """inscribed used to have no effect: CreatePolygon always treats the
    given point as a vertex."""
    try:
        n, r, h = 5, 25.0, 15.0
        expected = {
            False: 0.5 * n * r * r * math.sin(2 * math.pi / n) * h,   # r = vertex
            True: n * r * r * math.tan(math.pi / n) * h,              # r = apothem
        }
        for inscribed, want in expected.items():
            server = create_mcp_server()
            results = [
                server._route_tool("solidworks_new_part", {}),
                server._route_tool("solidworks_create_sketch", {"plane": "Front"}),
                server._route_tool("solidworks_sketch_polygon", {
                    "radius": r, "numSides": n, "centerX": 0, "centerY": 0,
                    "inscribed": inscribed}),
                server._route_tool("solidworks_create_extrusion", {"depth": h}),
            ]
            if not mcp_check_results(results):
                return False
            vol = _mcp_json(server._route_tool("solidworks_get_mass_properties", {}))["volume_mm3"]
            if abs(vol - want) > 1:
                log(f"inscribed={inscribed}: volume {vol:.1f} != expected {want:.1f}", "ERROR")
                return False
            server._route_tool("solidworks_close_document", {})
        log("Polygon inscribed/circumscribed volumes correct", "SUCCESS")
        return True
    except Exception as e:
        log(f"mcp_polygon_inscribed FAILED: {e}", "ERROR")
        traceback.print_exc()
        return False


@register_test("mcp_small_entities", "MCP Small Sketch Entities", "MCP Tools",
               "A radius-1mm circle (fails without AddToDB at default zoom) extrudes correctly", order=18)
def test_mcp_small_entities(sw, template):
    try:
        server = create_mcp_server()
        results = [
            server._route_tool("solidworks_new_part", {}),
            server._route_tool("solidworks_create_sketch", {"plane": "Front"}),
            server._route_tool("solidworks_sketch_circle", {"radius": 1, "centerX": 0, "centerY": 0}),
            server._route_tool("solidworks_create_extrusion", {"depth": 80}),
        ]
        if not mcp_check_results(results):
            return False
        vol = _mcp_json(server._route_tool("solidworks_get_mass_properties", {}))["volume_mm3"]
        want = math.pi * 1 * 1 * 80
        if abs(vol - want) > 0.5:
            log(f"Small rod volume {vol:.2f} != expected {want:.2f}", "ERROR")
            return False
        log("Radius-1mm circle drawn and extruded", "SUCCESS")
        return True
    except Exception as e:
        log(f"mcp_small_entities FAILED: {e}", "ERROR")
        traceback.print_exc()
        return False


@register_test("mcp_draft", "MCP Draft (inward + outward)", "MCP Tools",
               "10-degree draft on two faces of a 40x40x50 block, verify volume both ways", order=19)
def test_mcp_draft(sw, template):
    """draft used to call IFeatureManager.InsertFeatureDraft, which does not
    exist; it now uses InsertMultiFaceDraft (neutral plane mark 1, faces
    mark 2)."""
    try:
        t = math.tan(math.radians(10))
        expected = {False: 40 * (40 * 50 - t * 50 ** 2),   # faces taper inward
                    True: 40 * (40 * 50 + t * 50 ** 2)}    # outward
        for outward, want in expected.items():
            server = create_mcp_server()
            results = [
                server._route_tool("solidworks_new_part", {}),
                server._route_tool("solidworks_create_sketch", {"plane": "Front"}),
                server._route_tool("solidworks_sketch_rectangle", {
                    "width": 40, "height": 40, "centerX": 0, "centerY": 0}),
                server._route_tool("solidworks_create_extrusion", {"depth": 50}),
                server._route_tool("solidworks_draft", {
                    "angle": 10, "neutralPlane": "Front", "outward": outward,
                    "facesToDraft": [{"x": -20, "y": 0, "z": 25}, {"x": 20, "y": 0, "z": 25}]}),
            ]
            if not mcp_check_results(results):
                return False
            vol = _mcp_json(server._route_tool("solidworks_get_mass_properties", {}))["volume_mm3"]
            if abs(vol - want) > 1:
                log(f"draft outward={outward}: volume {vol:.1f} != expected {want:.1f}", "ERROR")
                return False
            server._route_tool("solidworks_close_document", {})
        log("Draft volumes correct in both directions", "SUCCESS")
        return True
    except Exception as e:
        log(f"mcp_draft FAILED: {e}", "ERROR")
        traceback.print_exc()
        return False


@register_test("mcp_ref_axis", "MCP Reference Axis", "MCP Tools",
               "Axis from a cylindrical face and from a straight edge; circular edge gives a clear error", order=20)
def test_mcp_ref_axis(sw, template):
    """ref_axis used to call IFeatureManager.InsertRefAxis, which does not
    exist; it now uses IModelDoc2.InsertAxis2 and finds the new RefAxis."""
    try:
        server = create_mcp_server()
        results = [
            server._route_tool("solidworks_new_part", {}),
            server._route_tool("solidworks_create_sketch", {"plane": "Front"}),
            server._route_tool("solidworks_sketch_circle", {"radius": 20, "centerX": 0, "centerY": 0}),
            server._route_tool("solidworks_create_extrusion", {"depth": 40}),
        ]
        if not mcp_check_results(results):
            return False
        r = server._route_tool("solidworks_ref_axis", {
            "type": "CYLINDRICAL_FACE", "face": {"x": 20, "y": 0, "z": 20}})
        if not _mcp_ok(r):
            log(f"ref_axis from cylinder failed: {r}", "ERROR")
            return False
        # _route_tool raises on tool errors (call_tool turns them into ❌ text)
        try:
            r_bad = server._route_tool("solidworks_ref_axis", {
                "type": "EDGE", "edge": {"x": 20, "y": 0, "z": 0}})
        except Exception as e:
            r_bad = f"❌ {e}"
        if _mcp_ok(r_bad) or "CYLINDRICAL_FACE" not in r_bad:
            log(f"Circular edge should fail with a CYLINDRICAL_FACE hint, got: {r_bad}", "ERROR")
            return False
        server._route_tool("solidworks_close_document", {})

        results = [
            server._route_tool("solidworks_new_part", {}),
            server._route_tool("solidworks_create_sketch", {"plane": "Front"}),
            server._route_tool("solidworks_sketch_rectangle", {"width": 40, "height": 40, "centerX": 0, "centerY": 0}),
            server._route_tool("solidworks_create_extrusion", {"depth": 40}),
        ]
        if not mcp_check_results(results):
            return False
        r2 = server._route_tool("solidworks_ref_axis", {
            "type": "EDGE", "edge": {"x": 20, "y": 20, "z": 20}})
        if not _mcp_ok(r2):
            log(f"ref_axis from straight edge failed: {r2}", "ERROR")
            return False
        tree = _mcp_json(server._route_tool("solidworks_get_feature_tree", {}))["tree"]
        if not any(n.get("type") == "RefAxis" for n in tree):
            log(f"No RefAxis in feature tree: {tree}", "ERROR")
            return False
        log("Reference axes created; circular edge rejected with a hint", "SUCCESS")
        return True
    except Exception as e:
        log(f"mcp_ref_axis FAILED: {e}", "ERROR")
        traceback.print_exc()
        return False


@register_test("mcp_circular_pattern", "MCP Circular Pattern", "MCP Tools",
               "Pattern a hole around a disk: by ref-axis ID, by circular edge, and with explicit spacing", order=21)
def test_mcp_circular_pattern(sw, template):
    """circular_pattern passed FeatureCircularPattern4's arguments in the
    wrong order (EqualSpacing ended up False, so 360 deg became the gap
    between instances) and always failed. There was no test for it."""
    try:
        disk = math.pi * 40 ** 2 * 10
        hole = math.pi * 4 ** 2 * 10
        cases = [
            ("axis = ref-axis ID, 6 over 360", None, {"count": 6, "angle": 360}, 6),
            ("axisEdge = outer circular edge, 6 over 360", {"x": 40, "y": 0, "z": 10}, {"count": 6, "angle": 360}, 6),
            ("equalSpacing=false, 4 at 90", None, {"count": 4, "angle": 90, "equalSpacing": False}, 4),
        ]
        for label, edge, params, holes in cases:
            server = create_mcp_server()
            results = [
                server._route_tool("solidworks_new_part", {}),
                server._route_tool("solidworks_create_sketch", {"plane": "Front"}),
                server._route_tool("solidworks_sketch_circle", {"radius": 40, "centerX": 0, "centerY": 0}),
                server._route_tool("solidworks_create_extrusion", {"depth": 10}),
                server._route_tool("solidworks_create_sketch", {"faceX": 0, "faceY": 0, "faceZ": 10}),
                server._route_tool("solidworks_sketch_circle", {"radius": 4, "centerX": 25, "centerY": 0}),
            ]
            cut = server._route_tool("solidworks_create_cut_extrusion", {"depth": 10})
            results.append(cut)
            axis = server._route_tool("solidworks_ref_axis", {
                "type": "CYLINDRICAL_FACE", "face": {"x": 40, "y": 0, "z": 5}})
            results.append(axis)
            if not mcp_check_results(results):
                return False
            args = dict(params, features=[_mcp_json(cut)["id"]])
            if edge:
                args["axisEdge"] = edge
            else:
                args["axis"] = _mcp_json(axis)["id"]
            r = server._route_tool("solidworks_circular_pattern", args)
            if not _mcp_ok(r):
                log(f"circular_pattern ({label}) failed: {r}", "ERROR")
                return False
            vol = _mcp_json(server._route_tool("solidworks_get_mass_properties", {}))["volume_mm3"]
            want = disk - holes * hole
            if abs(vol - want) > 1:
                log(f"circular_pattern ({label}): volume {vol:.1f} != expected {want:.1f}", "ERROR")
                return False
            log(f"{label}: volume {vol:.1f} OK", "SUCCESS")
            server._route_tool("solidworks_close_document", {})
        return True
    except Exception as e:
        log(f"mcp_circular_pattern FAILED: {e}", "ERROR")
        traceback.print_exc()
        return False


@register_test("mcp_ref_point", "MCP Reference Point (all modes)", "MCP Tools",
               "Reference points by coordinates, arc center, face center and along an edge; check locations", order=22)
def test_mcp_ref_point(sw, template):
    """ref_point used wrong swRefPointType_e codes (1 = Undefined, 0 = Invalid)
    and a 5-argument InsertReferencePoint with coordinates that the API
    does not have. COORDINATES now goes through a 3D-sketch point."""
    try:
        server = create_mcp_server()
        results = [
            server._route_tool("solidworks_new_part", {}),
            server._route_tool("solidworks_create_sketch", {"plane": "Front"}),
            server._route_tool("solidworks_sketch_circle", {"radius": 10, "centerX": 0, "centerY": 0}),
            server._route_tool("solidworks_create_extrusion", {"depth": 30}),
        ]
        if not mcp_check_results(results):
            return False
        cases = [
            ({"type": "COORDINATES", "x": 30, "y": 30, "z": 30}, (30, 30, 30)),
            ({"type": "ARC_CENTER", "edge": {"x": 10, "y": 0, "z": 0}}, (0, 0, 0)),
            ({"type": "FACE_CENTER", "face": {"x": 0, "y": 0, "z": 30}}, (0, 0, 30)),
            # half way round the r=10 circle from its start point (+X) is -X
            ({"type": "ON_EDGE", "edge": {"x": 10, "y": 0, "z": 0}, "percent": 50}, (-10, 0, 0)),
        ]
        for args, want in cases:
            r = server._route_tool("solidworks_ref_point", args)
            loc = _mcp_json(r).get("location_mm")
            if not loc or max(abs(a - b) for a, b in zip(loc, want)) > 0.01:
                log(f"ref_point {args['type']}: location {loc} != expected {want}", "ERROR")
                return False
            log(f"ref_point {args['type']} at {loc}", "SUCCESS")
        vol = _mcp_json(server._route_tool("solidworks_get_mass_properties", {}))["volume_mm3"]
        if abs(vol - math.pi * 100 * 30) > 1:
            log(f"Reference points changed the body volume: {vol}", "ERROR")
            return False
        return True
    except Exception as e:
        log(f"mcp_ref_point FAILED: {e}", "ERROR")
        traceback.print_exc()
        return False


@register_test("mcp_thread", "MCP Cosmetic Thread", "MCP Tools",
               "Cosmetic thread on a cylinder edge; read back minor diameter and depth, volume unchanged", order=23)
def test_mcp_thread(sw, template):
    """thread called IModelDoc2.InsertCosmeticThread3, which does not exist."""
    try:
        server = create_mcp_server()
        results = [
            server._route_tool("solidworks_new_part", {}),
            server._route_tool("solidworks_create_sketch", {"plane": "Front"}),
            server._route_tool("solidworks_sketch_circle", {"radius": 10, "centerX": 0, "centerY": 0}),
            server._route_tool("solidworks_create_extrusion", {"depth": 30}),
        ]
        if not mcp_check_results(results):
            return False
        r = server._route_tool("solidworks_thread", {
            "edge": {"x": 10, "y": 0, "z": 0}, "depth": 25, "diameter": 8})
        if not _mcp_ok(r):
            log(f"thread failed: {r}", "ERROR")
            return False
        name = _mcp_json(r)["id"].split(":", 1)[1]
        from solidworks.com_utils import com_prop
        fd = com_prop(sw.ActiveDoc.FeatureByName(name), "GetDefinition")
        dia, depth = fd.Diameter * 1000, fd.BlindDepth * 1000
        if abs(dia - 8) > 1e-6 or abs(depth - 25) > 1e-6:
            log(f"Thread definition: diameter {dia} (want 8), depth {depth} (want 25)", "ERROR")
            return False
        vol = _mcp_json(server._route_tool("solidworks_get_mass_properties", {}))["volume_mm3"]
        if abs(vol - math.pi * 100 * 30) > 1:
            log(f"Cosmetic thread changed the volume: {vol}", "ERROR")
            return False
        log(f"Thread: minor diameter {dia:g} mm, depth {depth:g} mm, volume unchanged", "SUCCESS")
        return True
    except Exception as e:
        log(f"mcp_thread FAILED: {e}", "ERROR")
        traceback.print_exc()
        return False


@register_test("mcp_boundary", "MCP Boundary Boss / Cut", "MCP Tools",
               "Boundary boss frustum (with and without a guide curve) and boundary cut; check volumes", order=24)
def test_mcp_boundary(sw, template):
    """boundary_boss / boundary_cut called InsertBoundaryBoss/InsertBoundaryCut,
    which do not exist; they now use InsertNetBlend2. The surfaces are
    approximations, so volumes are checked to 0.01%."""
    frustum = lambda R, r, h: math.pi * h / 3 * (R * R + R * r + r * r)

    def profiles(server, r1):
        R = server._route_tool
        s1 = _mcp_json(R("solidworks_create_sketch", {"plane": "Front"}))["id"]
        R("solidworks_sketch_circle", {"radius": r1, "centerX": 0, "centerY": 0})
        R("solidworks_exit_sketch", {})
        pl = _mcp_json(R("solidworks_ref_plane", {"type": "OFFSET", "referencePlane": "Front", "offset": 80}))["id"]
        s2 = _mcp_json(R("solidworks_create_sketch", {"plane": pl}))["id"]
        R("solidworks_sketch_circle", {"radius": 10, "centerX": 0, "centerY": 0})
        R("solidworks_exit_sketch", {})
        return [s1, s2]

    def check(server, label, want):
        vol = _mcp_json(server._route_tool("solidworks_get_mass_properties", {}))["volume_mm3"]
        if abs(vol - want) > want * 1e-4:
            log(f"{label}: volume {vol:.1f} != expected {want:.1f}", "ERROR")
            return False
        log(f"{label}: volume {vol:.1f} (exact {want:.1f})", "SUCCESS")
        return True

    try:
        # 1. boss from two circles (r30 at z=0, r10 at z=80)
        server = create_mcp_server()
        server._route_tool("solidworks_new_part", {})
        r = server._route_tool("solidworks_boundary_boss", {"profiles": profiles(server, 30)})
        if not _mcp_ok(r) or not check(server, "boundary boss", frustum(30, 10, 80)):
            log(f"boundary_boss: {r}", "ERROR")
            return False
        server._route_tool("solidworks_close_document", {})

        # 2. same boss with a straight guide curve on the Top plane joining
        #    the two circles at +X; the shape stays a frustum
        server = create_mcp_server()
        server._route_tool("solidworks_new_part", {})
        sk = profiles(server, 30)
        g = _mcp_json(server._route_tool("solidworks_create_sketch", {"plane": "Top"}))
        # map model points (30,0,0) and (10,0,80) into this sketch's frame
        o, ax, ay = g["sketchFrame"]["originModel_mm"], g["sketchFrame"]["xAxisModel"], g["sketchFrame"]["yAxisModel"]
        to_sk = lambda p: (sum((p[k] - o[k]) * ax[k] for k in range(3)), sum((p[k] - o[k]) * ay[k] for k in range(3)))
        (x1, y1), (x2, y2) = to_sk((30, 0, 0)), to_sk((10, 0, 80))
        server._route_tool("solidworks_sketch_line", {"x1": x1, "y1": y1, "x2": x2, "y2": y2})
        server._route_tool("solidworks_exit_sketch", {})
        r = server._route_tool("solidworks_boundary_boss", {"profiles": sk, "guideCurves": [g["id"]]})
        if not _mcp_ok(r) or not check(server, "boundary boss + guide", frustum(30, 10, 80)):
            log(f"boundary_boss with guide: {r}", "ERROR")
            return False
        # same volume either way, so also prove the guide was used:
        # IBoundaryBossFeatureData.GetCurvesCount(1 = swBoundaryBossDirection_Second)
        from solidworks.com_utils import com_prop
        name = _mcp_json(r)["id"].split(":", 1)[1]
        bd = com_prop(sw.ActiveDoc.FeatureByName(name), "GetDefinition")
        n1, n2 = bd.GetCurvesCount(0), bd.GetCurvesCount(1)
        if (n1, n2) != (2, 1):
            log(f"boundary boss curves: direction 1 = {n1}, direction 2 = {n2} (want 2, 1)", "ERROR")
            return False
        log("guide curve is used (direction 1: 2 profiles, direction 2: 1 guide)", "SUCCESS")
        server._route_tool("solidworks_close_document", {})

        # 3. cut a r20->r10 frustum out of a r40 x 80 cylinder
        server = create_mcp_server()
        results = [
            server._route_tool("solidworks_new_part", {}),
            server._route_tool("solidworks_create_sketch", {"plane": "Front"}),
            server._route_tool("solidworks_sketch_circle", {"radius": 40, "centerX": 0, "centerY": 0}),
            server._route_tool("solidworks_create_extrusion", {"depth": 80}),
        ]
        if not mcp_check_results(results):
            return False
        r = server._route_tool("solidworks_boundary_cut", {"profiles": profiles(server, 20)})
        if not _mcp_ok(r) or not check(server, "boundary cut", math.pi * 40 ** 2 * 80 - frustum(20, 10, 80)):
            log(f"boundary_cut: {r}", "ERROR")
            return False
        return True
    except Exception as e:
        log(f"mcp_boundary FAILED: {e}", "ERROR")
        traceback.print_exc()
        return False


# ---------------------------------------------------------------------------
# (d) feature tools that had no test at all. Every test checks geometry
# (volume against an analytic value), not just the tool's success message.
# ---------------------------------------------------------------------------

def _frame_mapper(sketch_result):
    """model point (mm) -> (x, y) in the sketch, using create_sketch's sketchFrame."""
    fr = _mcp_json(sketch_result)["sketchFrame"]
    o, ax, ay = fr["originModel_mm"], fr["xAxisModel"], fr["yAxisModel"]
    return lambda p: (sum((p[k] - o[k]) * ax[k] for k in range(3)),
                      sum((p[k] - o[k]) * ay[k] for k in range(3)))


def _volume(server):
    return _mcp_json(server._route_tool("solidworks_get_mass_properties", {}))["volume_mm3"]


def _expect_volume(server, label, want, rel=1e-6, abs_tol=0.5):
    vol = _volume(server)
    if abs(vol - want) > max(abs_tol, want * rel):
        log(f"{label}: volume {vol:.2f} != expected {want:.2f}", "ERROR")
        return False
    log(f"{label}: volume {vol:.2f} OK", "SUCCESS")
    return True


def _call(server, tool, args=None):
    """Route a tool call; tool errors come back as a '❌ ...' string."""
    try:
        return server._route_tool(tool, args or {})
    except Exception as e:
        return f"❌ {e}"


def _box(server, w, h, d, cx=0, cy=0):
    return [server._route_tool("solidworks_new_part", {}),
            server._route_tool("solidworks_create_sketch", {"plane": "Front"}),
            server._route_tool("solidworks_sketch_rectangle", {"width": w, "height": h, "centerX": cx, "centerY": cy}),
            server._route_tool("solidworks_create_extrusion", {"depth": d})]


@register_test("mcp_rib", "MCP Rib", "MCP Tools",
               "Rib between the two legs of an L-bracket; check the added triangle volume", order=25)
def test_mcp_rib(sw, template):
    # flipSide=True makes the first attempt point away from the walls, so it
    # exercises the delete-and-retry path as well
    for flip in (False, True):
        if not _rib_case(flip):
            return False
    return True


def _rib_case(flip):
    try:
        server = create_mcp_server()
        R = server._route_tool
        # L-bracket: base 80x10 (y 0..10) + wall 10x50 (x 0..10, y 10..60), 40 deep
        results = _box(server, 80, 10, 40, cx=40, cy=5)
        results += [R("solidworks_create_sketch", {"plane": "Front"}),
                    R("solidworks_sketch_rectangle", {"width": 10, "height": 50, "centerX": 5, "centerY": 35}),
                    R("solidworks_create_extrusion", {"depth": 40})]
        pl = _mcp_json(R("solidworks_ref_plane", {"type": "OFFSET", "referencePlane": "Front", "offset": 20}))["id"]
        sk = R("solidworks_create_sketch", {"plane": pl})
        results.append(sk)
        if not mcp_check_results(results):
            return False
        m = _frame_mapper(sk)
        (x1, y1), (x2, y2) = m((10, 50, 20)), m((70, 10, 20))
        R("solidworks_sketch_line", {"x1": x1, "y1": y1, "x2": x2, "y2": y2})
        r = _call(server, "solidworks_rib", {"thickness": 4, "flipSide": flip})
        if not _mcp_ok(r):
            log(f"rib (flipSide={flip}) failed: {r}", "ERROR")
            return False
        # triangle with legs 40 (y) and 60 (x), 4 thick
        ok = _expect_volume(server, f"rib flipSide={flip}", 80 * 10 * 40 + 10 * 50 * 40 + 0.5 * 40 * 60 * 4)
        ribs = [n for n in _mcp_json(server._route_tool("solidworks_get_feature_tree", {}))["tree"]
                if n.get("type") == "Rib"]
        if len(ribs) != 1:
            log(f"expected exactly one Rib feature, tree has {len(ribs)}", "ERROR")
            return False
        server._route_tool("solidworks_close_document", {})
        return ok
    except Exception as e:
        log(f"mcp_rib FAILED: {e}", "ERROR")
        traceback.print_exc()
        return False


@register_test("mcp_wrap", "MCP Wrap (emboss + deboss)", "MCP Tools",
               "Wrap a 10x10 square onto a r20 cylinder; check the emboss/deboss volume", order=26)
def test_mcp_wrap(sw, template):
    try:
        Rc, w, h, d = 20, 10, 10, 2
        theta = w / Rc
        expected = {"EMBOSS": theta / 2 * ((Rc + d) ** 2 - Rc ** 2) * h,
                    "DEBOSS": -theta / 2 * (Rc ** 2 - (Rc - d) ** 2) * h}
        base = math.pi * Rc ** 2 * 40
        for kind, delta in expected.items():
            server = create_mcp_server()
            R = server._route_tool
            results = [R("solidworks_new_part", {}),
                       R("solidworks_create_sketch", {"plane": "Front"}),
                       R("solidworks_sketch_circle", {"radius": Rc, "centerX": 0, "centerY": 0}),
                       R("solidworks_create_extrusion", {"depth": 40})]
            pl = _mcp_json(R("solidworks_ref_plane", {"type": "OFFSET", "referencePlane": "Right", "offset": Rc}))["id"]
            sk = R("solidworks_create_sketch", {"plane": pl})
            results.append(sk)
            if not mcp_check_results(results):
                return False
            cx, cy = _frame_mapper(sk)((Rc, 0, 20))
            R("solidworks_sketch_rectangle", {"width": w, "height": h, "centerX": cx, "centerY": cy})
            r = _call(server, "solidworks_wrap", {"type": kind, "face": {"x": Rc, "y": 0, "z": 20}, "depth": d})
            if not _mcp_ok(r):
                log(f"wrap {kind} failed: {r}", "ERROR")
                return False
            if not _expect_volume(server, f"wrap {kind}", base + delta, rel=1e-3):
                return False
            R("solidworks_close_document", {})
        return True
    except Exception as e:
        log(f"mcp_wrap FAILED: {e}", "ERROR")
        traceback.print_exc()
        return False


def _box_and_cylinder(server):
    """40x40x40 box at the origin + separate r10 cylinder centred on its +X face."""
    R = server._route_tool
    return _box(server, 40, 40, 40) + [
        R("solidworks_create_sketch", {"plane": "Front"}),
        R("solidworks_sketch_circle", {"radius": 10, "centerX": 20, "centerY": 0}),
        R("solidworks_create_extrusion", {"depth": 40, "merge": False})]


@register_test("mcp_combine_bodies", "MCP Combine Bodies (ADD/SUBTRACT/COMMON)", "MCP Tools",
               "Box + overlapping cylinder as separate bodies; check each boolean's volume", order=27)
def test_mcp_combine_bodies(sw, template):
    try:
        box, cyl = 40 ** 3, math.pi * 10 ** 2 * 40
        half = cyl / 2
        cases = {"ADD": [box + half], "COMMON": [half],
                 "SUBTRACT": [box - half, cyl - half]}   # main body may be either
        for op, wants in cases.items():
            server = create_mcp_server()
            if not mcp_check_results(_box_and_cylinder(server)):
                return False
            r = _call(server, "solidworks_combine_bodies", {"operation": op})
            if not _mcp_ok(r):
                log(f"combine {op} failed: {r}", "ERROR")
                return False
            vol = _volume(server)
            if not any(abs(vol - w) < 0.5 for w in wants):
                log(f"combine {op}: volume {vol:.2f} not in {[round(w, 2) for w in wants]}", "ERROR")
                return False
            log(f"combine {op}: volume {vol:.2f} OK", "SUCCESS")
            server._route_tool("solidworks_close_document", {})
        return True
    except Exception as e:
        log(f"mcp_combine_bodies FAILED: {e}", "ERROR")
        traceback.print_exc()
        return False


@register_test("mcp_intersect", "MCP Intersect", "MCP Tools",
               "Box + overlapping cylinder as separate bodies; intersect keeps the half cylinder", order=28)
def test_mcp_intersect(sw, template):
    try:
        server = create_mcp_server()
        if not mcp_check_results(_box_and_cylinder(server)):
            return False
        r = _call(server, "solidworks_intersect", {})
        if not _mcp_ok(r):
            log(f"intersect failed: {r}", "ERROR")
            return False
        return _expect_volume(server, "intersect", math.pi * 10 ** 2 * 40 / 2)
    except Exception as e:
        log(f"mcp_intersect FAILED: {e}", "ERROR")
        traceback.print_exc()
        return False


@register_test("mcp_cut_sweep", "MCP Cut Sweep", "MCP Tools",
               "Sweep a r5 circle along a straight 40mm path through a 40mm cube", order=29)
def test_mcp_cut_sweep(sw, template):
    try:
        server = create_mcp_server()
        R = server._route_tool
        results = _box(server, 40, 40, 40)
        prof = R("solidworks_create_sketch", {"plane": "Front"})
        results += [prof, R("solidworks_sketch_circle", {"radius": 5, "centerX": 0, "centerY": 0}),
                    R("solidworks_exit_sketch", {})]
        path = R("solidworks_create_sketch", {"plane": "Top"})
        results.append(path)
        if not mcp_check_results(results):
            return False
        m = _frame_mapper(path)
        (x1, y1), (x2, y2) = m((0, 0, 0)), m((0, 0, 40))
        R("solidworks_sketch_line", {"x1": x1, "y1": y1, "x2": x2, "y2": y2})
        R("solidworks_exit_sketch", {})
        r = _call(server, "solidworks_cut_sweep", {"profileSketch": _mcp_json(prof)["id"],
                                                   "pathSketch": _mcp_json(path)["id"]})
        if not _mcp_ok(r):
            log(f"cut_sweep failed: {r}", "ERROR")
            return False
        return _expect_volume(server, "cut sweep", 40 ** 3 - math.pi * 25 * 40)
    except Exception as e:
        log(f"mcp_cut_sweep FAILED: {e}", "ERROR")
        traceback.print_exc()
        return False


@register_test("mcp_cut_loft", "MCP Cut Loft", "MCP Tools",
               "Loft-cut a r15 -> r5 frustum through a 40mm cube", order=30)
def test_mcp_cut_loft(sw, template):
    try:
        server = create_mcp_server()
        R = server._route_tool
        results = _box(server, 40, 40, 40)
        s1 = R("solidworks_create_sketch", {"plane": "Front"})
        results += [s1, R("solidworks_sketch_circle", {"radius": 15, "centerX": 0, "centerY": 0}),
                    R("solidworks_exit_sketch", {})]
        pl = _mcp_json(R("solidworks_ref_plane", {"type": "OFFSET", "referencePlane": "Front", "offset": 40}))["id"]
        s2 = R("solidworks_create_sketch", {"plane": pl})
        results += [s2, R("solidworks_sketch_circle", {"radius": 5, "centerX": 0, "centerY": 0}),
                    R("solidworks_exit_sketch", {})]
        if not mcp_check_results(results):
            return False
        r = _call(server, "solidworks_cut_loft", {"profileSketches": [_mcp_json(s1)["id"], _mcp_json(s2)["id"]]})
        if not _mcp_ok(r):
            log(f"cut_loft failed: {r}", "ERROR")
            return False
        frustum = math.pi * 40 / 3 * (15 ** 2 + 15 * 5 + 5 ** 2)
        return _expect_volume(server, "cut loft", 40 ** 3 - frustum, rel=1e-4)
    except Exception as e:
        log(f"mcp_cut_loft FAILED: {e}", "ERROR")
        traceback.print_exc()
        return False


@register_test("mcp_hole_wizard", "MCP Hole Wizard", "MCP Tools",
               "ISO drilled (blind, through, 2 positions), counterbore, countersink and tapped holes; volumes checked", order=31)
def test_mcp_hole_wizard(sw, template):
    """hole_wizard used wrong enums and an 18-argument HoleWizard5 (27 on
    SW2022). Each case checks the removed volume against an analytic value."""
    from solidworks.com_utils import com_prop
    tip = lambda r: math.pi * r * r * (r / math.tan(math.radians(59))) / 3   # 118 deg drill point
    cyl = lambda r, h: math.pi * r * r * h

    def run(label, args, expected=None, check_def=None):
        server = create_mcp_server()
        if not mcp_check_results(_box(server, 40, 40, 20)):
            return False
        r = _call(server, "solidworks_hole_wizard", args)
        if not _mcp_ok(r):
            log(f"{label}: {r}", "ERROR")
            return False
        removed = _mcp_json(r)["removed_mm3"]
        ids = _mcp_json(r)["id"]
        first = (ids[0] if isinstance(ids, list) else ids).split(":", 1)[-1]
        fd = com_prop(sw.ActiveDoc.FeatureByName(first), "GetDefinition")
        if check_def is not None:
            expected = check_def(fd)
        server._route_tool("solidworks_close_document", {})
        if abs(removed - expected) > 0.05:
            log(f"{label}: removed {removed:.2f} != expected {expected:.2f}", "ERROR")
            return False
        log(f"{label}: removed {removed:.2f} OK", "SUCCESS")
        return True

    top = {"x": 0, "y": 0, "z": 20}
    hole = {"type": "HOLE", "size": "Ø10.0"}

    def countersink(fd):
        D, a = fd.CounterSinkDiameter * 1000, fd.CounterSinkAngle
        d = fd.ThruHoleDiameter * 1000 if fd.ThruHoleDiameter > 0 else fd.HoleDiameter * 1000
        h = (D - d) / 2 / math.tan(a / 2)
        log(f"countersink definition: csink Ø{D:g} x {math.degrees(a):g}°, through Ø{d:g}")
        return math.pi * h / 3 * ((D / 2) ** 2 + D * d / 4 + (d / 2) ** 2) + cyl(d / 2, 20 - h)

    return all([
        run("HOLE Ø10 blind 10", dict(hole, face=top, depth=10), cyl(5, 10) + tip(5)),
        # plain HOLE + THROUGH_ALL is built as a blind hole that exits the part
        run("HOLE Ø10 through all", dict(hole, face={"x": 5, "y": 0, "z": 20}, endCondition="THROUGH_ALL"), cyl(5, 20)),
        run("HOLE Ø10 at 2 positions", dict(hole, face=top, depth=10,
            positions=[{"x": 10, "y": 10, "z": 20}, {"x": -10, "y": -10, "z": 20}]), 2 * (cyl(5, 10) + tip(5))),
        # ISO M6 socket head cap screw: counterbore Ø11 x 6.4, clearance Ø6.6
        run("COUNTERBORE M6 through", {"type": "COUNTERBORE", "size": "M6", "face": top,
            "endCondition": "THROUGH_ALL"}, cyl(5.5, 6.4) + cyl(3.3, 20 - 6.4)),
        run("COUNTERSINK M6 through", {"type": "COUNTERSINK", "size": "M6", "face": top,
            "endCondition": "THROUGH_ALL"}, check_def=countersink),
        # M6 tap: thread modelled at the Ø6 major diameter for 12, then Ø5 drill to 15 + tip
        run("STRAIGHT_TAP M6 thread 12, drill 15", {"type": "STRAIGHT_TAP", "size": "M6", "face": top,
            "depth": 15, "threadDepth": 12}, cyl(3, 12) + cyl(2.5, 3) + tip(2.5)),
    ])


def _sketch_then_extrude(label, steps, want, depth=10):
    """New part, sketch on Front, run (tool, args) steps, extrude, check volume."""
    server = create_mcp_server()
    R = server._route_tool
    try:
        R("solidworks_new_part", {})
        R("solidworks_create_sketch", {"plane": "Front"})
        results = {}
        for tool, args in steps:
            r = _call(server, tool, args)
            if not _mcp_ok(r):
                log(f"{label}: {tool} -> {r}", "ERROR")
                return False
            results.setdefault(tool, []).append(r)
        if not _mcp_ok(_call(server, "solidworks_create_extrusion", {"depth": depth})):
            log(f"{label}: extrusion failed", "ERROR")
            return False
        ok = _expect_volume(server, label, want, rel=1e-5)
        return results if ok else False
    finally:
        _call(server, "solidworks_close_document", {})


@register_test("mcp_sketch_profile", "MCP Sketch Profile", "MCP Tools",
               "Chained lines + tangent arc + auto close + corner fillet; check extruded volume", order=32)
def test_mcp_sketch_profile(sw, template):
    # (0,0)->(40,0)->(40,20) then a forward tangent arc to (20,20): a half
    # circle R10 bulging upward; line to (0,20); auto-close to (0,0); R5 fillet
    # at (0,0) removes r^2 - pi r^2 / 4
    area = 40 * 20 + math.pi * 10 ** 2 / 2 - (25 - math.pi * 25 / 4)
    return bool(_sketch_then_extrude("profile", [("solidworks_sketch_profile", {
        "startX": 0, "startY": 0,
        "segments": [{"x": 40, "y": 0}, {"x": 40, "y": 20},
                     {"type": "TANGENT_ARC", "x": 20, "y": 20}, {"x": 0, "y": 20}],
        "cornerFillets": [{"x": 0, "y": 0, "radius": 5}]})], area * 10))


@register_test("mcp_sketch_fillet", "MCP Sketch Fillet", "MCP Tools",
               "R5 sketch fillet on a rectangle corner; check extruded volume", order=33)
def test_mcp_sketch_fillet(sw, template):
    area = 40 * 20 - (25 - math.pi * 25 / 4)
    return bool(_sketch_then_extrude("sketch_fillet", [
        ("solidworks_sketch_rectangle", {"width": 40, "height": 20, "centerX": 0, "centerY": 0}),
        ("solidworks_sketch_fillet", {"x": 20, "y": 10, "radius": 5})], area * 10))


@register_test("mcp_sketch_offset", "MCP Sketch Offset", "MCP Tools",
               "Offset a R10 circle by +5 and by -5; the ring areas tell which side each sign goes", order=34)
def test_mcp_sketch_offset(sw, template):
    ring = lambda a, b: math.pi * abs(a * a - b * b) * 10
    circle = ("solidworks_sketch_circle", {"centerX": 0, "centerY": 0, "radius": 10})
    plus = _sketch_then_extrude("offset +5 (outward, R15)", [
        circle, ("solidworks_sketch_offset", {"x": 10, "y": 0, "distance": 5})], ring(15, 10))
    minus = _sketch_then_extrude("offset -5 (inward, R5)", [
        circle, ("solidworks_sketch_offset", {"x": 10, "y": 0, "distance": -5})], ring(10, 5))
    return bool(plus) and bool(minus)


@register_test("mcp_set_dimension_value", "MCP Sketch Dimension + Set Dimension Value", "MCP Tools",
               "Dimension a rectangle edge to 60 (sketch_dimension), then 70 (set_dimension_value); check volumes", order=35)
def test_mcp_set_dimension_value(sw, template):
    rect = ("solidworks_sketch_rectangle", {"width": 40, "height": 20, "centerX": 0, "centerY": 0})
    dim = ("solidworks_sketch_dimension", {"entityPoints": [{"x": 0, "y": 10}], "dimX": 0, "dimY": 20, "value": 60})
    r = _sketch_then_extrude("sketch_dimension 60", [rect, dim], 60 * 20 * 10)
    if not r:
        return False
    param = _mcp_json(r["solidworks_sketch_dimension"][0]).get("parameter")
    if not param or not param.startswith("D"):
        log(f"sketch_dimension returned no parameter name: {param!r}", "ERROR")
        return False
    log(f"dimension name: {param}", "SUCCESS")
    return bool(_sketch_then_extrude("set_dimension_value 70", [
        rect, dim, ("solidworks_set_dimension_value", {"dimX": 0, "dimY": 20, "value": 70})], 70 * 20 * 10))


def _dimensioned_box(server, depth=20):
    """40 wide x 30 high box (not square: SOLIDWORKS infers an Equal relation
    on a square, so driving the width would drag the height along) whose width is the sketch dimension D1@<sketch> = 40.
    Returns (sketch_param, extrude_param) from list_parameters, or None."""
    R = server._route_tool
    R("solidworks_new_part", {})
    R("solidworks_create_sketch", {"plane": "Front"})
    R("solidworks_sketch_rectangle", {"width": 40, "height": 30, "centerX": 0, "centerY": 0})
    R("solidworks_sketch_dimension", {"entityPoints": [{"x": 0, "y": 15}], "dimX": 0, "dimY": 25, "value": 40})
    R("solidworks_create_extrusion", {"depth": depth})
    r = _call(server, "solidworks_list_parameters", {})
    if not _mcp_ok(r):
        log(f"list_parameters: {r}", "ERROR")
        return None
    params = _mcp_json(r)["parameters"]
    log(f"list_parameters: {[(p['parameter'], p.get('value')) for p in params]}")
    width = [p["parameter"] for p in params if abs(p.get("value", 0) - 40) < 1e-6]
    depth_ = [p["parameter"] for p in params if abs(p.get("value", 0) - depth) < 1e-6]
    if len(width) != 1 or len(depth_) != 1:
        log("list_parameters did not report exactly one 40 mm width and one depth dimension", "ERROR")
        return None
    return width[0], depth_[0]


@register_test("mcp_equations", "MCP Equations", "MCP Tools",
               "list_parameters names -> add_equation depth = width/4 -> drive width -> delete_equation; volumes checked", order=36)
def test_mcp_equations(sw, template):
    server = create_mcp_server()
    try:
        names = _dimensioned_box(server)
        if not names:
            return False
        width, depth = names
        r = _call(server, "solidworks_add_equation", {"equation": f'"{depth}" = "{width}" / 4'})
        if not _mcp_ok(r) or "⚠" in r:
            log(f"add_equation: {r}", "ERROR")
            return False
        if not _expect_volume(server, "depth = 40/4", 40 * 30 * 10):
            return False
        eqs = _mcp_json(_call(server, "solidworks_list_equations"))["equations"]
        log(f"list_equations: {eqs}")
        if len(eqs) != 1 or depth not in eqs[0]["equation"]:
            log("list_equations does not show the one equation", "ERROR")
            return False
        _call(server, "solidworks_set_parameter", {"parameter": width, "value": 80})
        if not _expect_volume(server, "width 80 -> depth 20 via equation", 80 * 30 * 20):
            return False
        if not _mcp_ok(_call(server, "solidworks_delete_equation", {"index": eqs[0]["index"]})):
            return False
        if _mcp_json(_call(server, "solidworks_list_equations"))["equations"]:
            log("equation still listed after delete", "ERROR")
            return False
        _call(server, "solidworks_set_parameter", {"parameter": width, "value": 40})
        return _expect_volume(server, "after delete: width 40, depth stays 20", 40 * 30 * 20)
    finally:
        _call(server, "solidworks_close_document", {})


@register_test("mcp_configurations", "MCP Configurations", "MCP Tools",
               "add_configuration B, set_config_parameter depth 30 in B only, switch back and forth; volumes checked", order=37)
def test_mcp_configurations(sw, template):
    server = create_mcp_server()
    try:
        names = _dimensioned_box(server)
        if not names:
            return False
        depth = names[1]
        cfgs = _mcp_json(_call(server, "solidworks_list_configurations"))
        base = cfgs["active"]
        log(f"list_configurations: {cfgs['configurations']} active={base}")
        if len(cfgs["configurations"]) != 1 or not base:
            return False
        if not _mcp_ok(_call(server, "solidworks_add_configuration", {"name": "B"})):
            return False
        r = _call(server, "solidworks_set_config_parameter", {"parameter": depth, "value": 30, "configuration": "B"})
        if not _mcp_ok(r) or "⚠" in r:
            log(f"set_config_parameter: {r}", "ERROR")
            return False
        if not _expect_volume(server, "config B: depth 30", 40 * 30 * 30):
            return False
        _call(server, "solidworks_switch_configuration", {"name": base})
        if not _expect_volume(server, f"config {base}: depth still 20", 40 * 30 * 20):
            return False
        _call(server, "solidworks_switch_configuration", {"name": "B"})
        if not _expect_volume(server, "back to B: depth 30", 40 * 30 * 30):
            return False
        cfgs = _mcp_json(_call(server, "solidworks_list_configurations"))
        if sorted(cfgs["configurations"]) != sorted([base, "B"]) or cfgs["active"] != "B":
            log(f"list_configurations after: {cfgs}", "ERROR")
            return False
        bad = _call(server, "solidworks_switch_configuration", {"name": "NoSuchConfig"})
        if _mcp_ok(bad):
            log("switching to a missing configuration did not report an error", "ERROR")
            return False
        log("missing configuration rejected", "SUCCESS")
        return True
    finally:
        _call(server, "solidworks_close_document", {})


_HOLE_REMOVED = math.pi * 25 * 10 + math.pi * 25 * (5 / math.tan(math.radians(59))) / 3   # ISO Ø10.0 blind 10


def _box_with_hole(server):
    """40x30x20 box + one ISO Ø10.0 x 10 drilled hole on the top face.
    Returns (extrude_name, hole_name) or None."""
    if not mcp_check_results(_box(server, 40, 30, 20)):
        return None
    r = _call(server, "solidworks_hole_wizard", {"type": "HOLE", "size": "Ø10.0", "depth": 10,
                                                 "face": {"x": 0, "y": 0, "z": 20}})
    if not _mcp_ok(r):
        log(f"hole_wizard: {r}", "ERROR")
        return None
    hole = _mcp_json(r)["id"].split(":", 1)[-1]
    top = [n["name"] for n in _tree(server) if not n.get("isFolder")]
    others = [n for n in top if n != hole]
    return others[-1], hole


def _tree(server):
    return _mcp_json(server._route_tool("solidworks_get_feature_tree", {}))["tree"]


def _folder(server, name):
    for n in _tree(server):
        if n.get("isFolder") and n["name"] == name:
            return [c["name"] for c in n.get("children", [])]
    return None


@register_test("mcp_feature_tree_ops", "MCP Feature Tree Ops", "MCP Tools",
               "rename, suppress/unsuppress, empty folder + move_to_folder (empty and append paths), folder around a feature, delete; tree and volume checked", order=38)
def test_mcp_feature_tree_ops(sw, template):
    server = create_mcp_server()
    full, holed = 40 * 30 * 20, 40 * 30 * 20 - _HOLE_REMOVED
    try:
        names = _box_with_hole(server)
        if not names:
            return False
        extrude, hole = names
        log(f"features: extrude={extrude!r} hole={hole!r}")

        # rename (+ the two documented refusals)
        if not _mcp_ok(_call(server, "solidworks_rename_feature", {"name": extrude, "newName": "Base Block"})):
            return False
        if "Base Block" not in [n["name"] for n in _tree(server)]:
            log("renamed feature not in the tree", "ERROR")
            return False
        for bad in ("Base Block", "a@b"):
            if _mcp_ok(_call(server, "solidworks_rename_feature", {"name": hole, "newName": bad})):
                log(f"rename to {bad!r} should have been refused", "ERROR")
                return False
        log("rename OK; duplicate name and '@' refused", "SUCCESS")

        # suppress / unsuppress
        _call(server, "solidworks_suppress_feature", {"feature": hole, "suppress": True})
        if not _expect_volume(server, "hole suppressed", full):
            return False
        _call(server, "solidworks_suppress_feature", {"feature": hole, "suppress": False})
        if not _expect_volume(server, "hole unsuppressed", holed):
            return False

        # empty folder; move the block in (empty-folder path), then the hole
        # (appended after the block, keeping the build order)
        r = _call(server, "solidworks_create_feature_folder", {"folderName": "Stock"})
        if not _mcp_ok(r) or _folder(server, "Stock") != []:
            log(f"empty folder: {r} / {_folder(server, 'Stock')}", "ERROR")
            return False
        for feats, want in ((["Base Block"], ["Base Block"]), ([hole], ["Base Block", hole])):
            r = _call(server, "solidworks_move_to_folder", {"folderName": "Stock", "features": feats})
            if not _mcp_ok(r) or _folder(server, "Stock") != want:
                log(f"move_to_folder {feats}: {r} / {_folder(server, 'Stock')}", "ERROR")
                return False
        # a second hole, wrapped in a new folder by create_feature_folder
        r = _call(server, "solidworks_hole_wizard", {"type": "HOLE", "size": "Ø10.0", "depth": 10,
                                                     "face": {"x": 12, "y": 7, "z": 20}})
        hole2 = _mcp_json(r)["id"].split(":", 1)[-1] if _mcp_ok(r) else None
        r = _call(server, "solidworks_create_feature_folder", {"folderName": "Holes", "features": [hole2]})
        if not hole2 or not _mcp_ok(r) or _folder(server, "Holes") != [hole2]:
            log(f"folder with features: {r} / {_folder(server, 'Holes')}", "ERROR")
            return False
        top = [n["name"] for n in _tree(server)]
        if top != ["Stock", "Holes"]:
            log(f"top level should be exactly the two folders: {top}", "ERROR")
            return False
        log(f"folders: Stock={_folder(server, 'Stock')} Holes={_folder(server, 'Holes')}", "SUCCESS")
        if not _expect_volume(server, "after folder moves (2 holes)", full - 2 * _HOLE_REMOVED):
            return False

        # delete; a name deleted through the tool is no longer known
        if not _mcp_ok(_call(server, "solidworks_delete_feature", {"feature": hole})):
            return False
        if not _expect_volume(server, "first hole deleted", holed):
            return False
        if _mcp_ok(_call(server, "solidworks_delete_feature", {"feature": hole})):
            log("deleting the same feature twice should report it is not found", "ERROR")
            return False
        # deleting the block takes its child (the second hole) with it; deleting
        # that tracked hole afterwards succeeds as alreadyDeleted
        if not _mcp_ok(_call(server, "solidworks_delete_feature", {"feature": "Base Block"})):
            return False
        again = _call(server, "solidworks_delete_feature", {"feature": hole2})
        if not _mcp_ok(again) or not _mcp_json(again).get("alreadyDeleted"):
            log(f"delete of a feature removed with its parent: {again}", "ERROR")
            return False
        log("delete OK; repeat delete refused; child removed with its parent reported alreadyDeleted", "SUCCESS")
        return True
    finally:
        _call(server, "solidworks_close_document", {})


@register_test("mcp_open_import", "MCP Open / Import / Recognize", "MCP Tools",
               "Save a holed box as SLDPRT and STEP; open_document, import_file (+ via open_document), recognize_features; volumes checked", order=39)
def test_mcp_open_import(sw, template):
    holed = 40 * 30 * 20 - _HOLE_REMOVED
    d = tempfile.mkdtemp(prefix="swmcp_doc_tests_")   # left in %TEMP%; never deleted by the test
    server = create_mcp_server()
    try:
        if not _box_with_hole(server):
            return False
        part = _mcp_json(server._route_tool("solidworks_save_document", {"path": os.path.join(d, "holed_box")}))["path"]
        step = _mcp_json(server._route_tool("solidworks_save_document", {"path": os.path.join(d, "holed_box.STEP")}))["path"]
        log(f"saved {part} and {step}")
    finally:
        _call(server, "solidworks_close_document", {})

    def check(label, tool, args, extra=None):
        server = create_mcp_server()
        try:
            r = _call(server, tool, args)
            if not _mcp_ok(r):
                log(f"{label}: {r}", "ERROR")
                return False
            ok = _expect_volume(server, label, holed, rel=1e-6)
            return ok and (extra(server) if extra else True)
        finally:
            _call(server, "solidworks_close_document", {})

    def has_hole_feature(server):
        types = [n["type"] for n in _tree(server)]
        log(f"opened part tree types: {types}")
        return "HoleWzd" in types

    def recognize(server):
        r = _call(server, "solidworks_recognize_features", {})
        if not _mcp_ok(r):
            log(f"recognize_features: {r}", "ERROR")
            return False
        n = _mcp_json(r).get("featuresRecognized", 0)
        types = [x["type"] for x in _tree(server)]
        log(f"recognized {n} feature(s); tree types now {types}")
        return n >= 2 and _expect_volume(server, "after recognition", holed, rel=1e-6)

    return all([
        check("open_document .SLDPRT", "solidworks_open_document", {"path": part}, has_hole_feature),
        check("import_file .STEP", "solidworks_import_file", {"path": step}),
        check("open_document .STEP (routed to import)", "solidworks_open_document", {"path": step}),
        check("import_file + recognize_features", "solidworks_import_file", {"path": step}, recognize),
    ])


def _png_box_aspect(path):
    """Width/height of the part in a screenshot, measured on its black
    silhouette edges (the SOLIDWORKS background is a gradient, so comparing
    against a background colour also picks up the blue-grey shading)."""
    from PIL import Image
    im = Image.open(path).convert("RGB")
    w, h = im.size
    px = im.load()
    xs, ys = [], []
    for y in range(h):
        for x in range(w):
            if sum(px[x, y]) < 120:          # near-black edge pixels
                xs.append(x); ys.append(y)
    if not xs:
        return None
    return (max(xs) - min(xs)) / max(1, (max(ys) - min(ys)))


@register_test("mcp_views", "MCP Views (capture_views / look_at_model)", "MCP Tools",
               "PNG per view; the drawn box's aspect ratio matches the view (front 40:30, top 40:20); look_at_model labels 6 faces", order=40)
def test_mcp_views(sw, template):
    server = create_mcp_server()
    d = tempfile.mkdtemp(prefix="swmcp_view_tests_")   # left in %TEMP%; never deleted by the test
    try:
        if not mcp_check_results(_box(server, 40, 30, 20)):
            return False
        r = _call(server, "solidworks_capture_views", {"views": ["ISOMETRIC", "FRONT", "TOP"], "outputDir": d})
        if not _mcp_ok(r):
            log(f"capture_views: {r}", "ERROR")
            return False
        files = _mcp_json(r)["files"]
        if len(files) != 3 or not all(os.path.getsize(f) > 1000 for f in files):
            log(f"capture_views files: {files}", "ERROR")
            return False
        ok = True
        for f, want, label in ((files[1], 40 / 30, "FRONT"), (files[2], 40 / 20, "TOP")):
            a = _png_box_aspect(f)
            good = a is not None and abs(a - want) / want < 0.08
            log(f"{label}: drawn aspect {a if a is None else round(a, 3)} (expected {want:.3f})", "SUCCESS" if good else "ERROR")
            ok = ok and good
        if not ok:
            return False

        r = _call(server, "solidworks_look_at_model", {"view": "front"})
        if not r.startswith("IMAGE_FILE:"):
            log(f"look_at_model: {r[:300]}", "ERROR")
            return False
        png, payload = r[len("IMAGE_FILE:"):].split("\n", 1)
        info = _mcp_json(payload)
        a = _png_box_aspect(png)
        good = info.get("labeled") and len(info.get("faces", [])) == 6 and a is not None
        log(f"look_at_model: {len(info.get('faces', []))} labelled faces, labeled={info.get('labeled')}, aspect {a}",
            "SUCCESS" if good else "ERROR")
        return bool(good)
    finally:
        _call(server, "solidworks_close_document", {})


@register_test("mcp_query_tools", "MCP Query Tools", "MCP Tools",
               "find_face / get_vertices / get_sketch_entities / get_entity on a 40x30x20 block with a Ø10 through hole", order=41)
def test_mcp_query_tools(sw, template):
    server = create_mcp_server()
    R = server._route_tool
    try:
        R("solidworks_new_part", {})
        sketch = _mcp_json(R("solidworks_create_sketch", {"plane": "Front"}))["id"]
        rect = _mcp_json(R("solidworks_sketch_rectangle", {"width": 40, "height": 30, "centerX": 0, "centerY": 0}))["id"]
        circ = _mcp_json(R("solidworks_sketch_circle", {"centerX": 0, "centerY": 0, "radius": 5}))["id"]
        feat = _mcp_json(R("solidworks_create_extrusion", {"depth": 20}))["id"]
        log(f"ids: sketch={sketch} rect={rect} circle={circ} feature={feat}")
        ok = True

        def check(label, cond, detail=""):
            nonlocal ok
            log(f"{label}{': ' + detail if detail else ''}", "SUCCESS" if cond else "ERROR")
            ok = ok and bool(cond)

        # find_face
        top = _mcp_json(R("solidworks_find_face", {"orientation": "+Z"}))["faces"]
        # the sample point must lie ON the face: inside the block outline and
        # outside the Ø10 hole (the surface's UV midpoint is the hole centre)
        tp = top[0]["point"] if top else {"x": 0, "y": 0, "z": 0}
        check("find_face +Z", len(top) == 1 and abs(top[0]["area"] - (1200 - 25 * math.pi)) < 0.05
              and abs(tp["z"] - 20) < 1e-6 and math.hypot(tp["x"], tp["y"]) > 5.01
              and abs(tp["x"]) < 20 and abs(tp["y"]) < 15, f"{[(f['area'], f['point']) for f in top]}")
        r = _call(server, "solidworks_create_sketch", {"faceX": tp["x"], "faceY": tp["y"], "faceZ": tp["z"]})
        check("create_sketch on that point", _mcp_ok(r), r[:160])
        # exiting it empty: SOLIDWORKS discards the sketch; this used to
        # overwrite the tracked record of the previous sketch
        r = _call(server, "solidworks_exit_sketch", {})
        check("exit empty sketch reports discarded", _mcp_ok(r) and _mcp_json(r).get("discarded"), r[:160])
        side = _mcp_json(R("solidworks_find_face", {"orientation": "-X"}))["faces"]
        check("find_face -X", len(side) == 1 and abs(side[0]["area"] - 600) < 0.05
              and abs(side[0]["point"]["x"] + 20) < 1e-6, f"{[(f['area'], f['point']) for f in side]}")
        cyl = _mcp_json(R("solidworks_find_face", {"surfaceType": "CYLINDER"}))["faces"]
        check("find_face CYLINDER", len(cyl) == 1 and abs(cyl[0]["area"] - 2 * math.pi * 5 * 20) < 0.05,
              f"{[f['area'] for f in cyl]}")
        small = _mcp_json(R("solidworks_find_face", {"orientation": "ANY", "areaRank": "SMALLEST"}))["faces"]
        check("find_face SMALLEST", len(small) == 1 and abs(small[0]["area"] - 600) < 0.05,
              f"{[f['area'] for f in small]}")
        by_feat = _mcp_json(R("solidworks_find_face", {"feature": feat}))["faces"]
        check("find_face feature", len(by_feat) == 7, f"{len(by_feat)} faces (6 box + 1 hole)")
        none = _mcp_json(R("solidworks_find_face", {"orientation": "ANGLED"}))["faces"]
        check("find_face ANGLED (none)", none == [])

        # get_vertices: the 8 box corners (circular hole edges add no corners)
        verts = {(v["x"], v["y"], v["z"]) for v in _mcp_json(R("solidworks_get_vertices", {}))["vertices"]}
        corners = {(x, y, z) for x in (-20, 20) for y in (-15, 15) for z in (0, 20)}
        check("get_vertices", corners <= verts and all(v[2] in (0, 20) for v in verts),
              f"{len(verts)} vertices, all 8 corners present: {corners <= verts}")

        # get_sketch_entities / get_entity
        ents = _mcp_json(R("solidworks_get_sketch_entities", {"sketchId": sketch})).get("entities", [])
        check("get_sketch_entities", [e["id"] for e in ents] == [rect, circ], f"{[(e['id'], e['type']) for e in ents]}")
        e = _mcp_json(R("solidworks_get_entity", {"id": circ}))
        check("get_entity circle", e.get("type") and abs(float(e["shapeInfo"].get("radius", 0)) - 5) < 1e-6,
              f"type={e.get('type')} shapeInfo={e.get('shapeInfo')}")
        f = _mcp_json(R("solidworks_get_entity", {"id": feat}))
        check("get_entity feature", f.get("id") == feat and f.get("type"), f"{f.get('type')} {f.get('name')}")
        s = _mcp_json(R("solidworks_get_entity", {"id": sketch}))
        check("get_entity sketch", s.get("entityCount") == 2, f"entityCount={s.get('entityCount')}")
        missing = R("solidworks_get_entity", {"id": "feat:NoSuchFeature"})
        check("get_entity unknown id", "✓" not in missing, missing[:120])
        return ok
    finally:
        _call(server, "solidworks_close_document", {})


@register_test("mcp_batch", "MCP Batch", "MCP Tools",
               "Build a box in one batch; stopOnError skips the rest, stopOnError=false continues; nesting refused", order=42)
def test_mcp_batch(sw, template):
    server = create_mcp_server()
    R = server._route_tool
    try:
        ok = True

        def check(label, cond, detail=""):
            nonlocal ok
            log(f"{label}{': ' + detail if detail else ''}", "SUCCESS" if cond else "ERROR")
            ok = ok and bool(cond)

        b = _mcp_json(R("solidworks_batch", {"calls": [
            {"tool": "solidworks_new_part"},
            {"tool": "solidworks_create_sketch", "args": {"plane": "Front"}},
            {"tool": "solidworks_sketch_rectangle", "args": {"width": 40, "height": 30, "centerX": 0, "centerY": 0}},
            {"tool": "solidworks_create_extrusion", "args": {"depth": 20}},
        ]}))
        check("build batch 4/4", all(c["ok"] for c in b["calls"]) and b["result"].startswith("✓"), b["result"])
        check("box volume", abs(_volume(server) - 24000) < 0.5)

        bad = [{"tool": "solidworks_get_body_info"},
               {"tool": "solidworks_suppress_feature", "args": {"feature": "NoSuchFeature", "suppress": True}},
               {"tool": "solidworks_get_body_info"}]
        b = _mcp_json(R("solidworks_batch", {"calls": bad}))
        flags = [(c["ok"], c.get("error", "")[:30]) for c in b["calls"]]
        check("stopOnError (default): ok, fail, skipped",
              [c["ok"] for c in b["calls"]] == [True, False, False] and "skipped" in b["calls"][2].get("error", "")
              and b["result"].startswith("⚠"), f"{flags}")
        b = _mcp_json(R("solidworks_batch", {"calls": bad, "stopOnError": False}))
        check("stopOnError=false: ok, fail, ok", [c["ok"] for c in b["calls"]] == [True, False, True],
              f"{[c['ok'] for c in b['calls']]}")
        nested = _call(server, "solidworks_batch", {"calls": [{"tool": "solidworks_batch", "args": {"calls": []}}]})
        check("nested batch refused", not _mcp_ok(nested), nested[:80])
        check("volume unchanged", abs(_volume(server) - 24000) < 0.5)
        return ok
    finally:
        _call(server, "solidworks_close_document", {})


@register_test("mcp_cam_flow", "MCP CAM Flow (setup → features → operations → toolpath → simulate → G-code)", "MCP Tools",
               "60x40x20 base + 30x20 boss to z=30 + 2x Ø10 blind holes; stock +2/+3/+5 mm; 4 operations; "
               "simulation 0 gouges, WIP = part + base side offsets; G-code drill depth Z-23.002", order=43)
def test_mcp_cam_flow(sw, template):
    if sw.GetAddInObject("CAMWorksAddin.MWAddin") is None:
        log("SOLIDWORKS CAM add-in not loaded — test cannot run", "ERROR")
        return False
    server = create_mcp_server()
    R = server._route_tool
    out = tempfile.mkdtemp(prefix="swmcp_cam_test_")   # left in %TEMP%; never deleted by the test
    try:
        ok = True

        def check(label, cond, detail=""):
            nonlocal ok
            log(f"{label}{': ' + detail if detail else ''}", "SUCCESS" if cond else "ERROR")
            ok = ok and bool(cond)

        for name, args in [
                ("solidworks_new_part", {}),
                ("solidworks_create_sketch", {"plane": "Front"}),
                ("solidworks_sketch_rectangle", {"width": 60, "height": 40, "centerX": 0, "centerY": 0}),
                ("solidworks_create_extrusion", {"depth": 20}),
                ("solidworks_create_sketch", {"plane": "Front"}),
                ("solidworks_sketch_rectangle", {"width": 30, "height": 20, "centerX": 0, "centerY": 0}),
                ("solidworks_create_extrusion", {"depth": 30}),
                ("solidworks_hole_wizard", {"type": "HOLE", "standard": "ISO", "size": "Ø10.0", "endCondition": "BLIND",
                                            "depth": 10, "face": {"x": 22, "y": 0, "z": 20},
                                            "positions": [{"x": -22, "y": 0, "z": 20}, {"x": 22, "y": 0, "z": 20}]})]:
            _mcp_json(R(name, args))

        bad = _call(server, "solidworks_cam_setup", {"postProcessor": "NO-SUCH-POST"})
        check("unknown post processor refused", not _mcp_ok(bad), bad[:100])

        s = _mcp_json(R("solidworks_cam_setup", {
            "stockOffsets": {"xPlus": 2, "yPlus": 3, "zPlus": 5, "xMinus": 2, "yMinus": 3, "zMinus": 0},
            "postProcessor": "M3AXIS-TUTORIAL"}))
        check("setup: post M3AXIS-TUTORIAL", s["postProcessor"].upper() == "M3AXIS-TUTORIAL",
              f"{s['postProcessor']} of {s['availablePostProcessors']}, material {s['material']}")

        f = _mcp_json(R("solidworks_cam_extract_features", {}))
        st = f.get("stock") or {}
        check("2 machinable features", f["count"] == 2, f"{f['setups']}")
        check("stock = bbox + offsets (64x46x35)", st.get("min_mm") == [-32, -23, 0] and st.get("max_mm") == [32, 23, 35]
              and abs(st.get("volume_mm3", 0) - 64 * 46 * 35) < 1, f"{st}")

        o = _mcp_json(R("solidworks_cam_generate_operations", {}))
        ops = o["operations"]
        check("4 operations named OP1..OP4", [x["name"] for x in ops] == ["OP1", "OP2", "OP3", "OP4"],
              f"{[(x['name'], x.get('originalName')) for x in ops]}")
        check("tool diameters 10/20/10/10", [x["tool"]["diameter_mm"] for x in ops] == [10, 20, 10, 10],
              f"{[(x['tool']['kind'], x['tool']['diameter_mm']) for x in ops]}")

        t = _mcp_json(R("solidworks_cam_generate_toolpath", {}))
        check("toolpaths for all operations", all(x["generated"] for x in t["operations"]) and t["result"].startswith("✓"),
              t["result"])

        img = os.path.join(out, "sim.jpg")
        m = _mcp_json(R("solidworks_cam_simulate", {"show": True, "imagePath": img}))
        check("simulation: 0 gouges, 0 undercuts", m["gouges"] == 0 and m["undercuts"] == 0, m["result"])
        w = m.get("wip") or {}
        check("WIP box = part box inside the stock offsets", w.get("min_mm") == [-32, -23, 0] and w.get("max_mm") == [32, 23, 30],
              f"{w}")
        side = (64 * 46 - 60 * 40) * 20     # base side offsets: not a machinable feature, left as stock
        check("WIP - part = base side offsets", abs(m.get("wipMinusPart_mm3", 1e9) - side) < 20,
              f"{m.get('wipMinusPart_mm3')} vs {side}")
        check("simulation image saved", os.path.exists(img) and os.path.getsize(img) > 1000, m.get("image", ""))

        nc = os.path.join(out, "cam_test.nc")
        p = _mcp_json(R("solidworks_cam_post", {"outputPath": nc}))
        z = p["ranges_mm"]["Z"]
        check("G-code written", os.path.exists(nc) and p["lines"] > 100, f"{p['lines']} lines, post {p['postProcessor']}")
        check("4 tool changes", len(p["toolChanges"]) == 4, f"{p['toolChanges']}")
        check("deepest Z = hole bottom incl. 118° tip (-23.002)", z and abs(z[0] + 23.002) < 0.01, f"Z {z}")
        check("tool list (.set) written", p.get("toolList") and os.path.exists(p["toolList"]), f"{p.get('toolList')}")
        check("no CAM dialogs had to be answered",
              not any(r.get("dialogs") for r in (s, f, o, t, m, p) if isinstance(r, dict)) and not s.get("notes"))
        return ok
    except Exception as e:
        log(f"mcp_cam_flow FAILED: {e}", "ERROR")
        traceback.print_exc()
        return False
    finally:
        _call(server, "solidworks_close_document", {})


def _mcp_json(r):
    """Parse an MCP tool JSON result. Raises with the raw string on error returns."""
    if r.startswith("❌"):
        raise RuntimeError(f"MCP tool returned error: {r}")
    return json.loads(r)


def _asm_test_dir():
    """Fresh temp directory for saved test parts."""
    d = os.path.join(tempfile.gettempdir(), "swmcp_assembly_tests")
    shutil.rmtree(d, ignore_errors=True)
    os.makedirs(d, exist_ok=True)
    return d


def mcp_build_and_save_box(server, path, size_mm=50):
    """Build a size_mm cube via MCP tools and save it to path. Returns saved path."""
    _mcp_json(server._route_tool("solidworks_new_part", {}))
    _mcp_json(server._route_tool("solidworks_create_sketch", {"plane": "Front"}))
    _mcp_json(server._route_tool("solidworks_sketch_rectangle", {
        "centerX": size_mm / 2, "centerY": size_mm / 2,
        "width": size_mm, "height": size_mm,
    }))
    _mcp_json(server._route_tool("solidworks_create_extrusion", {"depth": size_mm}))
    saved = _mcp_json(server._route_tool("solidworks_save_document", {"path": path}))
    return saved["path"]


def mcp_build_and_save_cylinder(server, path, radius_mm=10, height_mm=30):
    """Build a cylinder via MCP tools and save it to path. Returns saved path."""
    _mcp_json(server._route_tool("solidworks_new_part", {}))
    _mcp_json(server._route_tool("solidworks_create_sketch", {"plane": "Top"}))
    _mcp_json(server._route_tool("solidworks_sketch_circle", {"radius": radius_mm}))
    _mcp_json(server._route_tool("solidworks_create_extrusion", {"depth": height_mm}))
    saved = _mcp_json(server._route_tool("solidworks_save_document", {"path": path}))
    return saved["path"]


@register_test("assembly_doc_lifecycle", "Document Lifecycle (save/activate/close)", "Assembly",
               "Save a part, switch documents, verify per-document state isolation", order=0)
def test_assembly_doc_lifecycle(sw, template):
    test_dir = _asm_test_dir()
    try:
        server = create_mcp_server()
        cube_file = mcp_build_and_save_box(
            server, os.path.join(test_dir, "lifecycle_cube"), 40)
        if not os.path.exists(cube_file):
            log(f"Saved file missing: {cube_file}", "ERROR")
            return False

        # A second part must get its own state scope
        _mcp_json(server._route_tool("solidworks_new_part", {}))
        state2 = _mcp_json(server._route_tool("solidworks_get_state", {}))
        if state2["features"]:
            log(f"New part scope not empty: {state2['features']}", "ERROR")
            return False

        # Switching back must restore the cube's tracked state
        _mcp_json(server._route_tool("solidworks_activate_document", {"name": "lifecycle_cube"}))
        state1 = _mcp_json(server._route_tool("solidworks_get_state", {}))
        if not any(f.get("type") == "extrusion" for f in state1["features"]):
            log(f"Cube scope lost after switching: {state1['features']}", "ERROR")
            return False

        docs = _mcp_json(server._route_tool("solidworks_list_documents", {}))
        if len(docs["documents"]) < 2:
            log(f"Expected >=2 open documents, got {docs['documents']}", "ERROR")
            return False

        _mcp_json(server._route_tool("solidworks_close_document", {}))
        log("Document lifecycle (save/scope-switch/close) verified", "SUCCESS")
        return True
    except Exception as e:
        log(f"assembly_doc_lifecycle FAILED: {e}", "ERROR")
        traceback.print_exc()
        return False
    finally:
        shutil.rmtree(test_dir, ignore_errors=True)


@register_test("assembly_basic", "Basic Assembly (insert + coincident mate + mass)", "Assembly",
               "Build cube and cylinder parts, assemble, mate faces, verify total volume", order=1)
def test_assembly_basic(sw, template):
    test_dir = _asm_test_dir()
    try:
        server = create_mcp_server()
        cube_file = mcp_build_and_save_box(server, os.path.join(test_dir, "asm_cube"), 50)
        cyl_file = mcp_build_and_save_cylinder(server, os.path.join(test_dir, "asm_cyl"), 10, 30)

        _mcp_json(server._route_tool("solidworks_new_assembly", {}))
        ins1 = _mcp_json(server._route_tool("solidworks_insert_component", {"path": cube_file}))
        if not ins1.get("fixed"):
            log(f"First component should be auto-fixed: {ins1}", "ERROR")
            return False
        ins2 = _mcp_json(server._route_tool("solidworks_insert_component", {"path": cyl_file, "x": 150}))

        comps = _mcp_json(server._route_tool("solidworks_list_components", {}))
        if len(comps["components"]) != 2:
            log(f"Expected 2 components: {comps}", "ERROR")
            return False

        # Coincident mate: cube top face <-> cylinder bottom face, by assembly coordinates.
        # Both parts are bbox-centered at their drop points (cube spans ±25, cyl spans y ±15).
        cube_pos = ins1["actualPosition"]
        cyl_pos = ins2["actualPosition"]
        mate = _mcp_json(server._route_tool("solidworks_add_mate", {
            "mateType": "COINCIDENT",
            "entity1": {"entityType": "FACE",
                        "x": cube_pos["x"] + 25, "y": cube_pos["y"] + 50, "z": cube_pos["z"] + 25},
            "entity2": {"entityType": "FACE",
                        "x": cyl_pos["x"], "y": cyl_pos["y"], "z": cyl_pos["z"]},
        }))
        if mate.get("type") != "mate":
            log(f"Mate result unexpected: {mate}", "ERROR")
            return False

        mates = _mcp_json(server._route_tool("solidworks_list_mates", {}))
        if len(mates["mates"]) != 1 or mates["mates"][0].get("mateType") != "coincident":
            log(f"list_mates unexpected: {mates}", "ERROR")
            return False

        mp = _mcp_json(server._route_tool("solidworks_get_assembly_mass_properties", {}))
        expected = 50**3 + math.pi * 10**2 * 30
        if abs(mp["volume_mm3"] - expected) / expected > 0.01:
            log(f"Volume {mp['volume_mm3']} != expected {expected:.0f}", "ERROR")
            return False

        log(f"Assembly built: 2 components, coincident mate, volume {mp['volume_mm3']:.0f} mm^3", "SUCCESS")
        return True
    except Exception as e:
        log(f"assembly_basic FAILED: {e}", "ERROR")
        traceback.print_exc()
        return False
    finally:
        shutil.rmtree(test_dir, ignore_errors=True)


@register_test("assembly_plane_mates", "Plane Mates (parallel + distance)", "Assembly",
               "Mate two cubes via component planes: parallel and 100mm distance", order=2)
def test_assembly_plane_mates(sw, template):
    test_dir = _asm_test_dir()
    try:
        server = create_mcp_server()
        a_file = mcp_build_and_save_box(server, os.path.join(test_dir, "mate_a"), 30)
        b_file = mcp_build_and_save_box(server, os.path.join(test_dir, "mate_b"), 30)

        _mcp_json(server._route_tool("solidworks_new_assembly", {}))
        ins1 = _mcp_json(server._route_tool("solidworks_insert_component", {"path": a_file}))
        ins2 = _mcp_json(server._route_tool("solidworks_insert_component", {"path": b_file, "x": 100}))

        m1 = _mcp_json(server._route_tool("solidworks_add_mate", {
            "mateType": "PARALLEL",
            "entity1": {"plane": "Front", "component": ins1["id"]},
            "entity2": {"plane": "Front", "component": ins2["id"]},
        }))
        m2 = _mcp_json(server._route_tool("solidworks_add_mate", {
            "mateType": "DISTANCE",
            "distance": 100,
            "entity1": {"plane": "Right", "component": ins1["id"]},
            "entity2": {"plane": "Right", "component": ins2["id"]},
        }))
        if m2.get("value") != 100:
            log(f"Distance mate value missing: {m2}", "ERROR")
            return False

        mates = _mcp_json(server._route_tool("solidworks_list_mates", {}))
        types = sorted(m.get("mateType") for m in mates["mates"])
        if types != ["distance", "parallel"]:
            log(f"Unexpected mate types: {types}", "ERROR")
            return False

        log("Parallel + 100mm distance plane mates verified", "SUCCESS")
        return True
    except Exception as e:
        log(f"assembly_plane_mates FAILED: {e}", "ERROR")
        traceback.print_exc()
        return False
    finally:
        shutil.rmtree(test_dir, ignore_errors=True)


@register_test("assembly_interference_suppress", "Interference + Suppress Component", "Assembly",
               "Cylinder dropped inside a cube: interference = cylinder volume; suppress removes it, resolve restores it", order=4)
def test_assembly_interference_suppress(sw, template):
    d = tempfile.mkdtemp(prefix="swmcp_asm_tests_")   # left in %TEMP%; never deleted by the test
    server = create_mcp_server()
    R = server._route_tool
    cyl_vol, cube_vol = math.pi * 10 ** 2 * 30, 50 ** 3
    try:
        cube_file = mcp_build_and_save_box(server, os.path.join(d, "intf_cube"), 50)
        cyl_file = mcp_build_and_save_cylinder(server, os.path.join(d, "intf_cyl"), 10, 30)
        _mcp_json(R("solidworks_new_assembly", {}))
        _mcp_json(R("solidworks_insert_component", {"path": cube_file}))
        cyl = _mcp_json(R("solidworks_insert_component", {"path": cyl_file}))   # bbox-centred inside the cube
        cyl_id = cyl["id"]
        ok = True

        def check(label, cond, detail=""):
            nonlocal ok
            log(f"{label}{': ' + detail if detail else ''}", "SUCCESS" if cond else "ERROR")
            ok = ok and bool(cond)

        def mass():
            return _mcp_json(R("solidworks_get_assembly_mass_properties", {}))["volume_mm3"]

        it = _mcp_json(R("solidworks_check_interference", {}))
        vols = [i["volume_mm3"] for i in it.get("interferences", [])]
        comps = it["interferences"][0].get("components", []) if it.get("interferences") else []
        check("interference = cylinder volume", it.get("count") == 1 and abs(sum(vols) - cyl_vol) < 0.5
              and len(comps) == 2, f"count={it.get('count')} volumes={vols} components={comps}")

        r = _call(server, "solidworks_suppress_component", {"component": cyl_id, "suppress": True})
        check("suppress cylinder", _mcp_ok(r), r[:120])
        it = _mcp_json(R("solidworks_check_interference", {}))
        check("no interference while suppressed", it.get("count") == 0, f"count={it.get('count')}")
        m = mass()
        check("mass without the cylinder", abs(m - cube_vol) < 0.5, f"{m:.2f} (expected {cube_vol})")

        r = _call(server, "solidworks_suppress_component", {"component": cyl_id, "suppress": False})
        check("resolve cylinder", _mcp_ok(r), r[:120])
        m = mass()
        check("mass with the cylinder again", abs(m - cube_vol - cyl_vol) < 0.5, f"{m:.2f} (expected {cube_vol + cyl_vol:.2f})")
        it = _mcp_json(R("solidworks_check_interference", {}))
        check("interference back after resolve", it.get("count") == 1, f"count={it.get('count')}")

        check("unknown component refused",
              not _mcp_ok(_call(server, "solidworks_suppress_component", {"component": "comp:nope-1", "suppress": True})))
        return ok
    except Exception as e:
        log(f"assembly_interference_suppress FAILED: {e}", "ERROR")
        traceback.print_exc()
        return False
    finally:
        _call(server, "solidworks_close_document", {})


@register_test("assembly_state", "Assembly State (get_state + scope isolation)", "Assembly",
               "get_state reflects components/mates; part scopes survive assembly work", order=3)
def test_assembly_state(sw, template):
    test_dir = _asm_test_dir()
    try:
        server = create_mcp_server()
        cube_file = mcp_build_and_save_box(server, os.path.join(test_dir, "state_cube"), 25)

        _mcp_json(server._route_tool("solidworks_new_assembly", {}))
        _mcp_json(server._route_tool("solidworks_insert_component", {"path": cube_file}))

        state = _mcp_json(server._route_tool("solidworks_get_state", {}))
        if state.get("activeDocument", {}).get("type") != "assembly":
            log(f"activeDocument not assembly: {state.get('activeDocument')}", "ERROR")
            return False
        if len(state.get("components", [])) != 1:
            log(f"Expected 1 tracked component: {state.get('components')}", "ERROR")
            return False

        # The part's scope must still hold its feature state
        _mcp_json(server._route_tool("solidworks_activate_document", {"name": "state_cube"}))
        pstate = _mcp_json(server._route_tool("solidworks_get_state", {}))
        if not any(f.get("type") == "extrusion" for f in pstate["features"]):
            log(f"Part scope lost after assembly work: {pstate['features']}", "ERROR")
            return False
        if pstate.get("body", {}).get("size", {}).get("x") != 25.0:
            log(f"Part bounding box wrong: {pstate.get('body')}", "ERROR")
            return False

        log("Assembly state + per-document scope isolation verified", "SUCCESS")
        return True
    except Exception as e:
        log(f"assembly_state FAILED: {e}", "ERROR")
        traceback.print_exc()
        return False
    finally:
        shutil.rmtree(test_dir, ignore_errors=True)


@register_test("parametric_tools", "Parametric Tools (material, CS mass props, set_parameter, edit_mate)", "Assembly",
               "Material density, coordinate-system-relative COM, parametric edit, mate edit", order=5)
def test_parametric_tools(sw, template):
    test_dir = _asm_test_dir()
    try:
        server = create_mcp_server()

        # Material + coordinate-system-relative mass properties on a 100mm cube
        _mcp_json(server._route_tool("solidworks_new_part", {}))
        _mcp_json(server._route_tool("solidworks_create_sketch", {"plane": "Front"}))
        _mcp_json(server._route_tool("solidworks_sketch_rectangle", {"x1": 0, "y1": 0, "x2": 100, "y2": 100}))
        ext = _mcp_json(server._route_tool("solidworks_create_extrusion", {"depth": 100}))
        # Use the names the tools return (localized on non-English SOLIDWORKS)
        ext_name = ext["id"].split(":", 1)[1]
        _mcp_json(server._route_tool("solidworks_set_material", {"material": "6061 Alloy"}))
        mp = _mcp_json(server._route_tool("solidworks_get_mass_properties", {}))
        if abs(mp["mass_g"] - 2700.0) > 1:
            log(f"6061 cube should be 2700g, got {mp['mass_g']}", "ERROR")
            return False

        cs = _mcp_json(server._route_tool("solidworks_coordinate_system",
                                          {"origin": {"x": 100, "y": 100, "z": 100}}))
        mp = _mcp_json(server._route_tool("solidworks_get_mass_properties",
                                          {"coordinateSystem": cs["id"]}))
        com = mp["centerOfMass_mm"]
        if not all(abs(com[k] + 50) < 0.1 for k in ("x", "y", "z")):
            log(f"CS-relative COM should be (-50,-50,-50), got {com}", "ERROR")
            return False

        # set_parameter: extrusion depth 100 -> 40
        _mcp_json(server._route_tool("solidworks_set_parameter",
                                     {"parameter": f"D1@{ext_name}", "value": 40}))
        mp = _mcp_json(server._route_tool("solidworks_get_mass_properties", {}))
        if abs(mp["volume_mm3"] - 400000) > 1:
            log(f"After depth 40, volume should be 400000, got {mp['volume_mm3']}", "ERROR")
            return False

        # edit_mate: distance 80 -> 30 moves the floating component
        a = mcp_build_and_save_box(server, os.path.join(test_dir, "em_a"), 30)
        b = mcp_build_and_save_box(server, os.path.join(test_dir, "em_b"), 30)
        _mcp_json(server._route_tool("solidworks_new_assembly", {}))
        i1 = _mcp_json(server._route_tool("solidworks_insert_component", {"path": a}))
        i2 = _mcp_json(server._route_tool("solidworks_insert_component", {"path": b, "x": 120}))
        m = _mcp_json(server._route_tool("solidworks_add_mate", {
            "mateType": "DISTANCE", "distance": 80,
            "entity1": {"plane": "Right", "component": i1["id"]},
            "entity2": {"plane": "Right", "component": i2["id"]},
        }))
        e = _mcp_json(server._route_tool("solidworks_edit_mate", {"mate": m["id"], "distance": 30}))
        # Right planes pass through each part's origin, so the mate distance equals
        # the X gap between component origins
        xs = {c["name"].split("-")[0]: c["position"]["x"] for c in e["components"]}
        gap = abs(xs["em_b"] - xs["em_a"])
        if abs(gap - 30) > 0.5:
            log(f"edit_mate should leave a 30mm origin gap, got {gap} ({xs})", "ERROR")
            return False

        amp = _mcp_json(server._route_tool("solidworks_get_assembly_mass_properties", {}))
        if "mass_g" not in amp:
            log(f"assembly mass props missing grams: {amp}", "ERROR")
            return False

        log("Material, CS mass props, set_parameter, edit_mate all verified", "SUCCESS")
        return True
    except Exception as e:
        log(f"parametric_tools FAILED: {e}", "ERROR")
        traceback.print_exc()
        return False
    finally:
        shutil.rmtree(test_dir, ignore_errors=True)


@register_test("enriched_state_and_tags", "Enriched State + Feature Tags", "Assembly",
               "get_state body/constraint info; get_faces/get_edges feature tagging", order=4)
def test_enriched_state_and_tags(sw, template):
    try:
        server = create_mcp_server()
        _mcp_json(server._route_tool("solidworks_new_part", {}))
        _mcp_json(server._route_tool("solidworks_create_sketch", {"plane": "Front"}))
        _mcp_json(server._route_tool("solidworks_sketch_rectangle", {
            "centerX": 50, "centerY": 50, "width": 100, "height": 100}))
        _mcp_json(server._route_tool("solidworks_create_extrusion", {"depth": 100}))
        fil = _mcp_json(server._route_tool("solidworks_fillet", {
            "radius": 10, "edges": [{"x": 50, "y": 100, "z": 100}]}))
        fillet_name = fil["id"].split(":", 1)[1]  # "Fillet1" (localized on non-English UI)

        state = _mcp_json(server._route_tool("solidworks_get_state", {}))
        body = state.get("body", {})
        if body.get("size", {}).get("x") != 100.0:
            log(f"Bounding box missing/wrong: {body}", "ERROR")
            return False
        if state["sketches"][0].get("constrainedStatus") not in ("UNDER_DEFINED", "FULLY_DEFINED"):
            log(f"constrainedStatus missing: {state['sketches'][0]}", "ERROR")
            return False

        full = _mcp_json(server._route_tool("solidworks_get_state", {"detail": "full"}))
        if not any("parents" in f for f in full["features"]):
            log(f"Feature tree missing in detail=full: {full['features']}", "ERROR")
            return False

        faces = _mcp_json(server._route_tool("solidworks_get_faces", {}))
        if not any(f.get("feature") == fillet_name for f in faces["faces"]):
            log(f"{fillet_name} face tag missing: {faces['faces']}", "ERROR")
            return False
        if not all(f.get("feature") for f in faces["faces"]):
            log("Some faces missing feature tags", "ERROR")
            return False

        edges = _mcp_json(server._route_tool("solidworks_get_edges", {}))
        if not any(fillet_name in (e.get("features") or []) for e in edges["edges"]):
            log(f"{fillet_name} edge tag missing", "ERROR")
            return False

        log("Enriched get_state + face/edge feature tagging verified", "SUCCESS")
        return True
    except Exception as e:
        log(f"enriched_state_and_tags FAILED: {e}", "ERROR")
        traceback.print_exc()
        return False


# ===========================================================================
# TEST FUNCTIONS — Integration (sequential sub-tests in one document)
# ===========================================================================

@register_test("integration_suite", "Cut-Extrude Reliability Suite", "Integration",
               "18+ sequential sub-tests: sketch lifecycle, extrusion, cuts, units, enumeration",
               order=0)
def test_integration_suite(sw, template):
    """Full integration test from original test.py, adapted to accept sw/template."""
    global PASS, FAIL
    fail_at_start = FAIL  # the suite fails if ANY sub-check below fails

    # --- Part creation + plane selection ---
    subsection("Part creation & plane selection")
    close_all_docs(sw)
    doc = new_part(sw, template)
    ok = doc is not None
    _result("NewDocument returned a document", ok)
    if not ok:
        return False

    for plane in ("Front Plane", "Top Plane", "Right Plane"):
        try:
            feat = find_feature(doc, plane)
            _result(f"Select {plane}", feat is not None)
        except Exception:
            _result(f"Select {plane}", False)

    # --- Sketch lifecycle ---
    subsection("Sketch lifecycle (create \u2192 draw \u2192 exit)")
    try:
        create_sketch_on_plane(doc, "Front Plane")
        _result("InsertSketch on Front Plane", True)
    except Exception as e:
        _result("InsertSketch on Front Plane", False, str(e))
        return False

    try:
        doc.SketchManager.CreateCornerRectangle(0.0, 0.0, 0.0, 0.1, 0.1, 0.0)
        _result("CreateCornerRectangle 100x100 mm", True)
    except Exception as e:
        _result("CreateCornerRectangle 100x100 mm", False, str(e))
        return False

    try:
        exit_sketch(doc)
        _result("Exit sketch", True)
    except Exception as e:
        _result("Exit sketch", False, str(e))
        return False

    try:
        feat = find_feature(doc, "Sketch1")
        _result("Sketch1 discoverable after exit", feat is not None)
    except Exception as e:
        _result("Sketch1 discoverable after exit", False, str(e))
        return False

    # Feature type check
    try:
        feat = find_feature(doc, "Sketch1")
        type_name = feat.GetTypeName2 if feat else None
        _result("GetTypeName2 == 'ProfileFeature'", type_name == "ProfileFeature",
                f"got '{type_name}'" if type_name else "feature not found")
    except Exception as e:
        _result("GetTypeName2()", False, str(e))

    # Sketch counter
    try:
        found = get_latest_sketch_name(doc)
        _result(f"Latest sketch = '{found}'", found == N(doc, "Sketch1"))
    except Exception as e:
        _result("Feature enumeration", False, str(e))

    # Select by name
    try:
        feat = select_sketch(doc, N(doc, "Sketch1"))
        _result("FeatureByName('Sketch1') found", feat is not None)
    except Exception as e:
        _result("FeatureByName('Sketch1') found", False, str(e))

    # ClearSelection2 regression
    try:
        doc.ClearSelection2(True)
        feat = find_feature(doc, "Sketch1")
        result = feat.Select2(False, 0) if feat else False
        _result("Select2 returned True", bool(result))
        doc.ClearSelection2(True)
    except Exception as e:
        _result("Select2 call", False, str(e))

    # --- Extrusion ---
    subsection("Extrusion (add material)")
    try:
        feat = extrude(doc, N(doc, "Sketch1"), 100.0, cut=False)
        _result("FeatureExtrusion2 (add material)", feat is not None)
        doc.ViewZoomtofit2()
    except Exception as e:
        _result("FeatureExtrusion2 (add material)", False, str(e))
        return False

    # Sketch still selectable after extrusion
    try:
        feat = find_feature(doc, "Sketch1")
        _result("FeatureByName('Sketch1') post-extrusion", feat is not None)
    except Exception as e:
        _result("FeatureByName('Sketch1') post-extrusion", False, str(e))

    # --- Cut-extrusion ---
    subsection("Cut-extrusion (remove material)")
    try:
        create_sketch_on_face(doc, 50, 50, 100)
        _result("InsertSketch on top face of solid", True)
    except Exception as e:
        _result("InsertSketch on top face of solid", False, str(e))
        return False

    try:
        doc.SketchManager.CreateCircleByRadius(0.05, 0.05, 0.1, 0.01)
        _result("CreateCircleByRadius 10mm radius on top face", True)
    except Exception as e:
        _result("CreateCircleByRadius 10mm radius on top face", False, str(e))
        return False

    try:
        exit_sketch(doc)
        _result("Exit second sketch", True)
    except Exception as e:
        _result("Exit second sketch", False, str(e))
        return False

    try:
        feat = find_feature(doc, "Sketch2")
        _result("Sketch2 exists after exit", feat is not None)
    except Exception as e:
        _result("Sketch2 exists after exit", False, str(e))

    # Feature type & selection checks for Sketch2
    try:
        feat = find_feature(doc, "Sketch2")
        type_name = feat.GetTypeName2 if feat else None
        _result("Sketch2 GetTypeName2 == 'ProfileFeature'", type_name == "ProfileFeature")
    except Exception:
        pass

    try:
        found = get_latest_sketch_name(doc)
        _result(f"Latest sketch = '{found}'", found == N(doc, "Sketch2"))
    except Exception:
        pass

    try:
        feat = select_sketch(doc, N(doc, "Sketch2"))
        _result("FeatureByName('Sketch2') found", feat is not None)
    except Exception as e:
        _result("FeatureByName('Sketch2') found", False, str(e))

    try:
        feat = extrude(doc, N(doc, "Sketch2"), 50.0, cut=True, reverse=False)
        _result("FeatureCut4 (cut material)", feat is not None)
        doc.ViewZoomtofit2()
    except Exception as e:
        _result("FeatureCut4 (cut material)", False, str(e))

    # --- Cut-extrusion reversed ---
    subsection("Cut-extrusion reversed direction")
    try:
        create_sketch_on_face(doc, 30, 30, 0)
        # On the z=0 (back) face the sketch X axis is model -X, so model
        # x=10..40 is sketch x=-40..-10. (The original used +10..+40, which
        # lies entirely outside the body: both cut directions fail, and the
        # test only "passed" because reverse went into Flip = cut OUTSIDE
        # the profile, removing most of the block.)
        doc.SketchManager.CreateCornerRectangle(-0.04, 0.01, 0.0, -0.01, 0.04, 0.0)
        exit_sketch(doc)
        vol_before = doc.Extension.CreateMassProperty.Volume * 1e9
        # Exactly one direction points into the body; the other must be refused.
        try:
            feat = extrude(doc, N(doc, "Sketch3"), 30.0, cut=True, reverse=True)
        except RuntimeError:
            feat = None
            feat = extrude(doc, N(doc, "Sketch3"), 30.0, cut=True, reverse=False)
        removed = vol_before - doc.Extension.CreateMassProperty.Volume * 1e9
        _result("Cut 30mm into the body from the z=0 face (removes 27000 mm^3)",
                feat is not None and abs(removed - 27000) < 1, f"removed {removed:.1f}")
        doc.ViewZoomtofit2()
    except Exception as e:
        _result("Cut-extrusion reversed 30mm", False, str(e))

    # --- Multiple sequential cuts ---
    subsection("Multiple sequential cut-extrusions")
    for i in range(2):
        sketch_name = f"Sketch{4 + i}"  # label only; the real name may be localized
        cx_mm = 20 + i * 50
        cx_m = cx_mm / 1000.0
        try:
            create_sketch_on_face(doc, cx_mm, 50, 100)
            doc.SketchManager.CreateCircleByRadius(cx_m, 0.05, 0.1, 0.005)
            exit_sketch(doc)
            sketch_name = get_latest_sketch_name(doc)
            feat = extrude(doc, sketch_name, 15.0, cut=True, reverse=False)
            _result(f"Cut #{i+1} using {sketch_name}", feat is not None)
        except Exception as e:
            _result(f"Cut #{i+1} using {sketch_name}", False, str(e))

    # --- Unit conversion validation ---
    subsection("Unit conversion: 50x30mm rectangle")
    try:
        create_sketch_on_plane(doc, "Right Plane")
        doc.SketchManager.CreateCornerRectangle(-0.025, -0.015, 0.0, 0.025, 0.015, 0.0)
        exit_sketch(doc)
        _result("50x30mm rectangle sketch created", True)
    except Exception as e:
        _result("50x30mm rectangle sketch created", False, str(e))

    subsection("Unit conversion: circle radius=10mm")
    try:
        create_sketch_on_plane(doc, "Top Plane")
        doc.SketchManager.CreateCircleByRadius(0.0, 0.0, 0.0, 0.01)
        exit_sketch(doc)
        _result("10mm-radius circle sketch created", True)
    except Exception as e:
        _result("10mm-radius circle sketch created", False, str(e))

    # --- Feature-tree enumeration ---
    subsection("Feature-tree enumeration robustness")
    try:
        features = doc.FeatureManager.GetFeatures(True)
        sketches = [f.Name for f in features if f.GetTypeName2 == "ProfileFeature"]
        _result(f"Found {len(sketches)} sketch(es) in feature tree", len(sketches) > 0,
                ", ".join(sketches))
        for name in sketches:
            feat = doc.FeatureByName(name)
            _result(f"FeatureByName('{name}') round-trip", feat is not None)
    except Exception as e:
        _result("Feature enumeration", False, str(e))

    # --- Final zoom ---
    try:
        doc.ViewZoomtofit2()
        _result("ViewZoomtofit2()", True)
    except Exception as e:
        _result("ViewZoomtofit2()", False, str(e))

    # Previously this always returned True, so failed sub-checks (✗) were
    # printed but the suite still reported PASSED.
    failed = FAIL - fail_at_start
    if failed:
        log(f"{failed} integration sub-check(s) failed", "ERROR")
    return failed == 0


# ===========================================================================
# Test runner
# ===========================================================================

def run_selected_tests(entries):
    """Run a list of TestEntry items. Returns True if all pass."""
    global PASS, FAIL, RESULTS
    PASS, FAIL, RESULTS = 0, 0, []

    section("SOLIDWORKS MCP \u2014 TEST SUITE")

    print("  \u2192 Initialising COM\u2026")
    pythoncom.CoInitialize()

    try:
        sw = connect_to_solidworks()
        ver = sw.RevisionNumber
        print(f"  \u2713 Connected to SolidWorks {ver}")
    except Exception as e:
        print(f"  \u2717 Failed to connect: {e}")
        return False

    template = find_template()
    if not template:
        print("  \u2717 No Part template found. Aborting.")
        return False
    print(f"  \u2713 Template: {template}")

    print("  \u2192 Closing all open documents\u2026")
    close_all_docs(sw)

    overall_results = []
    for entry in entries:
        section(f"{entry.category}: {entry.display_name}")
        try:
            ok = entry.func(sw, template)
            overall_results.append((entry.display_name, ok))
            if ok:
                log(f"{entry.display_name} \u2014 PASSED", "SUCCESS")
            else:
                log(f"{entry.display_name} \u2014 FAILED", "ERROR")
        except Exception as e:
            overall_results.append((entry.display_name, False))
            log(f"{entry.display_name} \u2014 EXCEPTION: {e}", "ERROR")
            traceback.print_exc()

        # Close docs between independent tests to prevent accumulation
        close_all_docs(sw)

    # --- Summary ---
    section("RESULTS")
    total = len(overall_results)
    passed = sum(1 for _, ok in overall_results if ok)
    failed = total - passed

    if passed:
        print(f"\n  PASSED ({passed}):")
        for name, ok in overall_results:
            if ok:
                print(f"    \u2713 {name}")

    if failed:
        print(f"\n  FAILED ({failed}):")
        for name, ok in overall_results:
            if not ok:
                print(f"    \u2717 {name}")

    print(f"\n  Total  : {total}")
    print(f"  Passed : {passed}")
    print(f"  Failed : {failed}")
    if failed == 0:
        print("\n  \u2713 ALL TESTS PASSED")
    else:
        print(f"\n  \u2717 {failed} TEST(S) FAILED")
    print()

    return failed == 0


# ===========================================================================
# Interactive CLI selector (--gui)
# ===========================================================================

def interactive_selector():
    """Print numbered test list, accept user selection, run chosen tests."""

    # Group tests by category
    categories = {}
    for entry in TEST_REGISTRY:
        categories.setdefault(entry.category, []).append(entry)

    # Sort categories by defined order, tests by their order field
    sorted_cats = sorted(categories.keys(),
                         key=lambda c: CATEGORY_ORDER.index(c) if c in CATEGORY_ORDER else 999)

    # Build numbered list
    numbered = []
    print("\n  SolidWorks MCP Test Suite \u2014 Interactive Selector")
    print("  " + "=" * 54)

    for cat in sorted_cats:
        entries = sorted(categories[cat], key=lambda e: e.order)
        print(f"\n  {cat}:")
        for entry in entries:
            numbered.append(entry)
            idx = len(numbered)
            print(f"    [{idx:2d}] {entry.display_name}")

    print(f"\n  Enter selection:")
    print(f"    Numbers/ranges : 1,3-5,13")
    print(f"    Category name  : Sketch Tools")
    print(f"    Run everything : all")
    print(f"    Quit           : q")
    print()

    try:
        raw = input("  > ").strip()
    except (EOFError, KeyboardInterrupt):
        print("\n  Cancelled.")
        return

    if not raw or raw.lower() == "q":
        print("  Cancelled.")
        return

    if raw.lower() == "all":
        selected = list(numbered)
    else:
        # Check if input matches a category name (case-insensitive)
        cat_match = None
        for cat in sorted_cats:
            if raw.lower() == cat.lower():
                cat_match = cat
                break

        if cat_match:
            selected = [e for e in numbered if e.category == cat_match]
        else:
            # Parse as numbers/ranges: "1,3-5,13"
            indices = set()
            for part in raw.split(","):
                part = part.strip()
                if "-" in part:
                    try:
                        lo, hi = part.split("-", 1)
                        for i in range(int(lo), int(hi) + 1):
                            indices.add(i)
                    except ValueError:
                        print(f"  Invalid range: {part}")
                        return
                else:
                    try:
                        indices.add(int(part))
                    except ValueError:
                        print(f"  Invalid input: {part}")
                        return

            selected = []
            for idx in sorted(indices):
                if 1 <= idx <= len(numbered):
                    selected.append(numbered[idx - 1])
                else:
                    print(f"  Index out of range: {idx}")
                    return

    if not selected:
        print("  No tests selected.")
        return

    print(f"\n  Running {len(selected)} test(s)...\n")
    success = run_selected_tests(selected)
    sys.exit(0 if success else 1)


# ===========================================================================
# CLI entry point
# ===========================================================================

def list_tests():
    """Print all registered tests grouped by category."""
    categories = {}
    for entry in TEST_REGISTRY:
        categories.setdefault(entry.category, []).append(entry)

    sorted_cats = sorted(categories.keys(),
                         key=lambda c: CATEGORY_ORDER.index(c) if c in CATEGORY_ORDER else 999)

    for cat in sorted_cats:
        entries = sorted(categories[cat], key=lambda e: e.order)
        print(f"\n  {cat}:")
        for e in entries:
            print(f"    {e.name:30s} {e.description}")


def main():
    parser = argparse.ArgumentParser(description="SolidWorks MCP Test Suite")
    parser.add_argument("--gui", action="store_true",
                        help="Launch interactive CLI test picker")
    parser.add_argument("--category", type=str, default=None,
                        help="Run only tests in this category")
    parser.add_argument("--test", type=str, default=None,
                        help="Run a single test by name")
    parser.add_argument("--list", action="store_true",
                        help="List all available tests and exit")
    args = parser.parse_args()

    if args.list:
        list_tests()
        sys.exit(0)

    if args.gui:
        interactive_selector()
        return

    # Determine which tests to run
    if args.test:
        selected = [e for e in TEST_REGISTRY if e.name == args.test]
        if not selected:
            print(f"  Unknown test: {args.test}")
            print("  Use --list to see available tests.")
            sys.exit(1)
    elif args.category:
        selected = [e for e in TEST_REGISTRY
                    if e.category.lower() == args.category.lower()]
        if not selected:
            print(f"  Unknown category: {args.category}")
            print("  Available: " + ", ".join(CATEGORY_ORDER))
            sys.exit(1)
        selected.sort(key=lambda e: e.order)
    else:
        # Run all tests in category order, then by test order
        selected = sorted(TEST_REGISTRY,
                          key=lambda e: (CATEGORY_ORDER.index(e.category)
                                         if e.category in CATEGORY_ORDER else 999,
                                         e.order))

    success = run_selected_tests(selected)
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()

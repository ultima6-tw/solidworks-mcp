"""
Feature error/warning check shared by every tool that creates features.

A SOLIDWORKS feature call can "succeed" -- return a feature -- while the
feature is in an error or warning state and did nothing (probed on SW2022:
a through-all cut pointing away from the body comes back as error 31; a
linear pattern or mirror whose copies land outside the body comes back as
warning 51 and adds nothing). Which of None / error-feature SOLIDWORKS
returns can even change during a long session. So instead of trusting each
tool's own None check, the server snapshots the feature tree before a
feature tool runs and reads IFeature.GetErrorCode2 on every feature that
appeared (see SolidWorksMCPServer._route_tool).

Descriptions are the official swFeatureError_e text from the SOLIDWORKS API
help (swconst.chm, SW2022).
"""

import logging

import pythoncom
import win32com.client

logger = logging.getLogger(__name__)

FEATURE_ERROR_TEXT = {
    1: "Unknown error",
    10: "Loop for fillet/chamfer does not exist",
    11: "Face for fillet/chamfer does not exist",
    12: "Invalid fillet radius or a face blend fillet recommended",
    13: "Edge for fillet/chamfer does not exist",
    14: "Failed to create fillet due to model geometry",
    15: "Radius too small",
    16: "Selected elements cannot be extended to intersect",
    17: "Specified radius would eliminate one of the elements",
    18: "Radius is too big or the elements are tangent or nearly tangent",
    30: "Feature would create a disjoint body; direction may be wrong",
    31: "Cannot locate end of feature",
    32: "Unable to create this extruded feature due to geometric conditions",
    33: "Extruded cuts cannot have both open and closed contours",
    34: "Extruded cuts require at least one closed or open contour that does not self-intersect",
    35: "Open extruded cuts require a single open contour that does not self-intersect",
    36: "Bosses cannot have both open and closed contours",
    37: "Bosses require one or more closed contours that do not self-intersect",
    38: "One of the edges of this mate is suppressed, invalid, or no longer present",
    39: "One of the faces of this mate is suppressed, invalid, or no longer present",
    40: "Mating surface type is not supported",
    41: "One of the entities of this mate is suppressed, invalid, or no longer present",
    42: "Tangent not satisfied",
    43: "Mate points to dangling geometry",
    44: "Non-linear edges cannot be used for mating",
    45: "Mating is not supported for one of the components or one of the components cannot currently be modified",
    46: "This mate is over-defining the assembly",
    47: "This mate cannot be solved",
    48: "One or more mate entities were suppressed",
    49: "Feature deprecated",
    50: "Feature obsolete",
    51: "Sketch error (also reported for a pattern/mirror whose copies land outside the body)",
    52: "No intersection between reference entity and fillet edge",
    53: "Failed to update the partial edge data",
    54: "Failed to find propagate edges",
    55: "Failed to find a start edge",
    56: "Failed to find end edge",
    57: "Failed to find the fillet/chamfer edge",
    58: "The fillet offset is too large",
    59: "Select a sketch point, reference point, reference plane, or planar face",
    60: "Too many reference entities selected",
    61: "Total offset from start point and end point must be less than 100%",
    62: "End points cross over and result in a zero thickness fillet",
    63: "Reference entity is missing",
    64: "Reference entity is invalid",
    65: "Partial edge fillet is not supported",
    66: "Some edges form closed loops; partial edge fillet does not support closed loops",
    67: "Failed to find a unique point for projection of reference offset",
    68: "There are no references to repair",
    69: "Swept flange: invalid profile or path",
    70: "Swept flange: self-intersecting geometry",
}


def describe(code: int) -> str:
    return f"swFeatureError_e {code}: {FEATURE_ERROR_TEXT.get(code, 'not in the SW2022 list')}"


def feature_error(feat):
    """(code, is_warning) of a feature; (0, False) when it cannot be read.
    GetErrorCode2 takes a BYREF BOOL that receives the warning flag (a plain
    bool raises Type mismatch)."""
    try:
        warn = win32com.client.VARIANT(pythoncom.VT_BYREF | pythoncom.VT_BOOL, False)
        code = int(feat.GetErrorCode2(warn) or 0)
        return code, bool(warn.value) if code else False
    except Exception:
        return 0, False


def top_level_features(doc):
    out, f = [], doc.FirstFeature
    while f is not None:
        out.append(f)
        f = f.GetNextFeature
    return out


def feature_names(doc):
    names = set()
    for f in top_level_features(doc):
        try:
            names.add(f.Name)
        except Exception:
            pass
    return names


def delete_feature_only(doc, feat):
    """Delete one feature; its absorbed sketch stays (DeleteSelection2
    option 0 -- swDelete_Absorbed = 2 would take the sketch too)."""
    doc.ClearSelection2(True)
    feat.Select2(False, 0)
    doc.Extension.DeleteSelection2(0)
    doc.ClearSelection2(True)


def check_new_features(doc, names_before, tracker=None):
    """Read the error state of every top-level feature not in names_before.
    Features in ERROR are deleted (and dropped from the tracker); features
    with a WARNING are kept. Returns (removed_errors, warnings), each a list
    of (name, code)."""
    errors, warnings = [], []
    for f in top_level_features(doc):
        try:
            name = f.Name
        except Exception:
            continue
        if name in names_before:
            continue
        code, is_warning = feature_error(f)
        if not code:
            continue
        if is_warning:
            warnings.append((name, code))
            logger.warning(f"New feature '{name}' has a warning: {describe(code)}")
            continue
        logger.warning(f"New feature '{name}' is in error: {describe(code)}; removing it")
        try:
            delete_feature_only(doc, f)
        except Exception as e:
            logger.warning(f"Could not remove '{name}': {e}")
        if tracker:
            # Drop only the feature / ref-geometry record. Not
            # tracker.remove_feature: that also drops the source sketch,
            # which stays in SOLIDWORKS here (deleted with option 0).
            try:
                scope = tracker._scope()
                scope.features.pop(f"feat:{name}", None)
                scope.ref_geometry.pop(f"ref:{name}", None)
            except Exception:
                pass
        errors.append((name, code))
    return errors, warnings

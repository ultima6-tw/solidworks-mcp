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


def _name(feat):
    try:
        return feat.Name
    except Exception:
        return None


# Every COM object SOLIDWORKS hands to this out-of-process client leaves about
# 8-17 kernel handles in the SOLIDWORKS process that are never given back --
# not on closing the document, not when the client exits (probed on SW2022,
# NOTES.md "SOLIDWORKS 記憶體越用越大"). Walking the whole tree costs one
# object per feature, so the before/after snapshot walked it on every tool
# call: ~900 handles per walk at 59 features, ~5600 per cut, millions over a
# long session. The snapshot below reads only the last few features.
MARKER_DEPTH = 3


def snapshot(doc):
    """The tree before a tool runs, for new_features(). Normally the names of
    the last MARKER_DEPTH top-level features (FeatureByPositionReverse(i) is
    the top-level walk in reverse; new features are appended after them).
    When the rollback bar is not at the end, new features are inserted
    mid-tree, so the full set of names is taken instead."""
    last = doc.FeatureByPositionReverse(0)
    try:
        rolled = last is not None and bool(last.IsRolledBack)
    except Exception:
        rolled = True
    if rolled:
        return feature_names(doc)
    marker = []
    for i in range(MARKER_DEPTH):
        f = last if i == 0 else doc.FeatureByPositionReverse(i)
        if f is None:
            break
        name = _name(f)
        if name is not None:
            marker.append(name)
    return {"marker": marker}


def new_features(doc, before):
    """Top-level features that appeared since `before`, in tree order.
    `before` is a snapshot() or a set of names (full walk). Returns None when
    it cannot be told: none of the marker features is left (deleted, renamed
    or moved into a folder by the tool)."""
    if not isinstance(before, dict):
        names = set(before)
        return [f for f in top_level_features(doc) if _name(f) not in names]
    marker = set(before.get("marker") or [])
    found, i = [], 0
    while True:
        f = doc.FeatureByPositionReverse(i)
        if f is None:
            return found[::-1] if not marker else None
        if _name(f) in marker:
            return found[::-1]
        found.append(f)
        i += 1


def newest_feature(doc, type_names=None):
    """The last top-level feature -- of one of type_names (GetTypeName2) when
    given -- read backwards from the end of the tree with
    FeatureByPositionReverse: normally one or two objects instead of the whole
    tree (GetFeatures(True) also returns the features "in any order" per the
    API help, so its last item was never guaranteed to be the newest). None
    when there is none."""
    i = 0
    while True:
        f = doc.FeatureByPositionReverse(i)
        if f is None:
            return None
        if type_names is None:
            return f
        try:
            if f.GetTypeName2 in type_names:
                return f
        except Exception:
            pass
        i += 1


def newest_sketch_name(doc):
    """Name of the newest sketch (ProfileFeature) in the tree, or None."""
    f = newest_feature(doc, ("ProfileFeature",))
    return _name(f) if f is not None else None


def delete_feature_only(doc, feat):
    """Delete one feature; its absorbed sketch stays (DeleteSelection2
    option 0 -- swDelete_Absorbed = 2 would take the sketch too)."""
    doc.ClearSelection2(True)
    feat.Select2(False, 0)
    doc.Extension.DeleteSelection2(0)
    doc.ClearSelection2(True)


def check_new_features(doc, new_feats, tracker=None):
    """Read the error state of every new top-level feature (new_features()).
    Features in ERROR are deleted (and dropped from the tracker); features
    with a WARNING are kept. Returns (removed_errors, warnings), each a list
    of (name, code)."""
    errors, warnings = [], []
    for f in new_feats or []:
        name = _name(f)
        if name is None:
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

"""
Volume check run by server._route_tool around every tool that should change
the solid: the total volume before and after is reported, and a body that did
not change -- or changed the wrong way -- turns the result's ✓ into ⚠.

A feature can come back with no error code and still do nothing (or not what
was asked), so the error-code check (feature_errors) does not catch
everything. The volume is the one number every solid feature moves.

For a BLIND create_extrusion / create_cut_extrusion the tool also computes the
nominal change itself (profile area x depth; the area comes from SOLIDWORKS,
IModelDocExtension.GetSectionProperties2 on the absorbed sketch), so the
agent does not have to. The actual change may be SMALLER than nominal (a boss
overlapping existing material, a cut partly outside the body) -- that is
noted, not flagged. More than nominal, or the wrong sign, is flagged.

Probed on SW2022: the area of a 20x10 rectangle with an r3 hole reads
171.7257 mm2 (= 200 - 9 pi); a sketch SOLIDWORKS cannot use as a profile (three
nested circles, an open line, overlapping rectangles) returns status 3 -- the
same three nested circles extruded give a feature in error 51.
"""
import logging

import pythoncom
import win32com.client

logger = logging.getLogger(__name__)

# Tools that build or change the solid, with the sign the volume change must
# have: +1 adds material, -1 removes it, 0 either way (but it must change),
# None = report only (a change of 0 can be right: combining touching bodies,
# a scribe wrap, an intersect that keeps everything).
VOLUME_TOOLS = {
    "solidworks_create_extrusion": +1,
    "solidworks_create_cut_extrusion": -1,
    "solidworks_revolve": +1,
    "solidworks_sweep": +1,
    "solidworks_loft": +1,
    "solidworks_boundary_boss": +1,
    "solidworks_cut_revolve": -1,
    "solidworks_cut_sweep": -1,
    "solidworks_cut_loft": -1,
    "solidworks_boundary_cut": -1,
    "solidworks_fillet": 0,
    "solidworks_chamfer": 0,
    "solidworks_shell": -1,
    "solidworks_draft": 0,
    "solidworks_rib": +1,
    "solidworks_wrap": None,          # sign from args["type"], see expected_sign
    "solidworks_intersect": None,
    "solidworks_combine_bodies": None,
    "solidworks_linear_pattern": 0,
    "solidworks_circular_pattern": 0,
    "solidworks_mirror": 0,
    "solidworks_hole_wizard": -1,
}

# |change| below this is "did not change" (mm3)
ZERO_TOL_MM3 = 1e-3
# relative tolerance when comparing with the nominal change
REL_TOL = 1e-3


def expected_sign(tool, args):
    if tool == "solidworks_wrap":
        return {"EMBOSS": +1, "DEBOSS": -1}.get(str((args or {}).get("type", "")).upper())
    return VOLUME_TOOLS.get(tool)


def total_volume_mm3(doc):
    """Sum of the volumes of every solid body (hidden ones too), in mm3.
    Probed on SW2022: equals CreateMassProperty.Volume, overlapping separate
    bodies counted twice. None when it cannot be read."""
    try:
        bodies = doc.GetBodies2(0, False)   # swSolidBody, not visible-only
        total = 0.0
        for b in bodies or []:
            b._FlagAsMethod("GetMassProperties")
            total += float(list(b.GetMassProperties(1000.0))[3])
        return total * 1e9
    except Exception as e:
        logger.debug(f"volume read failed: {e}")
        return None


def sketch_area_mm2(doc, sketch_feature):
    """Area of a sketch's closed profile(s), or None when SOLIDWORKS cannot
    compute it (status != 0: open or invalid profile). GetSectionProperties2
    must be flagged as a method (late binding reads it as a property) and
    clears the selection."""
    try:
        ext = doc.Extension
        ext._FlagAsMethod("GetSectionProperties2")
        doc.ClearSelection2(True)
        sk = sketch_feature.GetSpecificFeature2
        v = ext.GetSectionProperties2(
            win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_DISPATCH, [sk]))
        doc.ClearSelection2(True)
        v = list(v or [])
        if len(v) < 2 or int(v[0]) != 0:
            return None
        return float(v[1]) * 1e6
    except Exception as e:
        logger.debug(f"sketch area read failed: {e}")
        return None


def _absorbed_sketch(feat):
    try:
        sub = feat.GetFirstSubFeature
        while sub is not None:
            if sub.GetTypeName2 in ("ProfileFeature", "3DProfileFeature"):
                return sub
            sub = sub.GetNextSubFeature
    except Exception:
        pass
    return None


def nominal_change_mm3(doc, tool, args, new_feature_names):
    """(nominal |change| in mm3, None) or (None, reason it was not computed)."""
    if tool not in ("solidworks_create_extrusion", "solidworks_create_cut_extrusion"):
        return None, None
    args = args or {}
    if str(args.get("endCondition", "BLIND")).upper() != "BLIND":
        return None, "not computed for THROUGH_ALL (depth set by the body)"
    try:
        depth = float(args["depth"])
    except Exception:
        return None, "no depth"
    # The new feature is the one new top-level feature that is not a sketch
    # (the sketch existed before; a cut's failed first attempt is deleted).
    feats = []
    f = doc.FirstFeature
    while f is not None:
        try:
            if f.Name in new_feature_names and f.GetTypeName2 not in ("ProfileFeature",
                                                                       "3DProfileFeature"):
                feats.append(f)
        except Exception:
            pass
        f = f.GetNextFeature
    if len(feats) != 1:
        return None, "could not tell which new feature to measure"
    sketch = _absorbed_sketch(feats[0])
    if sketch is None:
        return None, "sketch of the new feature not found"
    area = sketch_area_mm2(doc, sketch)
    if area is None:
        return None, "SOLIDWORKS could not compute the sketch area"
    return area * depth, None


def evaluate(tool, args, before, after, nominal=None, nominal_reason=None):
    """Build the volumeChange record. Returns (record, warning text or None,
    info text or None)."""
    delta = after - before
    rec = {"before_mm3": round(before, 3), "after_mm3": round(after, 3),
           "delta_mm3": round(delta, 3)}
    sign = expected_sign(tool, args)
    if nominal is not None:
        rec["expected_delta_mm3"] = round(nominal * (sign or 1), 3)
        rec["expected_basis"] = "profile area x depth"
    elif nominal_reason:
        rec["expected_basis"] = nominal_reason

    if abs(delta) < ZERO_TOL_MM3:
        if sign is not None:
            rec["check"] = "no_change"
            return rec, ("the solid's volume did not change -- the feature may not have done "
                         "anything; check the geometry"), None
        rec["check"] = "no_change_allowed"
        return rec, None, None
    if sign and delta * sign < 0:
        rec["check"] = "wrong_sign"
        want = "add" if sign > 0 else "remove"
        return rec, (f"this tool should {want} material but the volume went "
                     f"{'down' if delta < 0 else 'up'} by {abs(delta):.3f} mm3 -- check the geometry"), None
    if nominal is not None:
        tol = max(ZERO_TOL_MM3, REL_TOL * nominal)
        if abs(abs(delta) - nominal) <= tol:
            rec["check"] = "matches_expected"
            return rec, None, None
        if abs(delta) > nominal + tol:
            rec["check"] = "more_than_expected"
            return rec, (f"the volume changed by {abs(delta):.3f} mm3, more than profile area x depth "
                         f"= {nominal:.3f} mm3 -- check the geometry"), None
        rec["check"] = "less_than_expected"
        what = ("part of the cut is outside the body"
                if tool == "solidworks_create_cut_extrusion"
                else "it overlaps existing material")
        return rec, None, (f"volume changed by {abs(delta):.3f} mm3, less than profile area x depth "
                           f"= {nominal:.3f} mm3 (normal when {what})")
    rec["check"] = "changed"
    return rec, None, None


def annotate(out, rec, warning, info):
    """Add the record to a tool's JSON result; a warning turns ✓ into ⚠."""
    import json
    try:
        data = json.loads(out)
    except Exception:
        data = None
    if not isinstance(data, dict) or not isinstance(data.get("result"), str):
        tail = f" ⚠ Volume check: {warning}." if warning else ""
        return out + tail
    data["volumeChange"] = rec
    r = data["result"]
    if warning:
        data["result"] = ("⚠" + r[1:] if r.startswith("✓") else r) + f" ⚠ Volume check: {warning}."
    elif info:
        data["result"] = r + f" ℹ Volume check: {info}."
    return json.dumps(data)

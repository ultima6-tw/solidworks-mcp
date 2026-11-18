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


PATTERN_TOOLS = ("solidworks_linear_pattern", "solidworks_circular_pattern", "solidworks_mirror")


def new_solid_features(doc, new_feature_names):
    """The new top-level features that are not sketches, in tree order."""
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
    return feats


def copies(tool, args):
    """Number of copies a pattern/mirror adds besides the seed. SOLIDWORKS
    counts the seed in a pattern's instance count."""
    args = args or {}
    try:
        if tool == "solidworks_linear_pattern":
            n = int(args.get("count1", 1))
            n2 = int(args.get("count2", 1) or 1)
            return n * max(n2, 1) - 1
        if tool == "solidworks_circular_pattern":
            return int(args.get("count", 1)) - 1
        if tool == "solidworks_mirror":
            return 1
    except Exception:
        return None
    return None


def _seed_name(ref):
    s = str(ref)
    return s.split(":", 1)[1] if s.startswith("feat:") else s


def expectation(doc, tool, args, new_feature_names, seed_changes=None):
    """What the change should be, or why that is not known.
    Returns (exp, None) with exp = {"delta": signed expected change,
    "upper": largest plausible |change| or None, "basis": text}, or
    (None, reason). seed_changes maps feature name -> {"delta", "nominal"}
    recorded when those features were made in this session."""
    args = args or {}
    if tool in ("solidworks_create_extrusion", "solidworks_create_cut_extrusion"):
        if str(args.get("endCondition", "BLIND")).upper() != "BLIND":
            return None, "not computed for THROUGH_ALL (depth set by the body)"
        try:
            depth = float(args["depth"])
        except Exception:
            return None, "no depth"
        # The new feature is the one new top-level feature that is not a
        # sketch (the sketch existed before; a cut's failed first attempt is
        # deleted).
        feats = new_solid_features(doc, new_feature_names)
        if len(feats) != 1:
            return None, "could not tell which new feature to measure"
        sketch = _absorbed_sketch(feats[0])
        if sketch is None:
            return None, "sketch of the new feature not found"
        area = sketch_area_mm2(doc, sketch)
        if area is None:
            return None, "SOLIDWORKS could not compute the sketch area"
        sign = expected_sign(tool, args)
        return {"delta": sign * area * depth, "upper": area * depth,
                "basis": "profile area x depth"}, None

    if tool in PATTERN_TOOLS:
        k = copies(tool, args)
        if not k or k < 1:
            return None, "could not tell how many copies"
        seeds = [_seed_name(n) for n in (args.get("features") or [])]
        if not seeds:
            return None, "no seed features"
        known = [(seed_changes or {}).get(s) for s in seeds]
        if any(r is None for r in known):
            missing = [s for s, r in zip(seeds, known) if r is None]
            return None, (f"seed change not known for {missing} (made before this session, "
                          f"outside these tools, or renamed)")
        seed_delta = sum(r["delta"] for r in known)
        if abs(seed_delta) < ZERO_TOL_MM3:
            return None, "the seed itself did not change the volume"
        upper = None
        if all(r.get("nominal") is not None for r in known):
            upper = sum(abs(r["nominal"]) for r in known) * k
        return {"delta": seed_delta * k, "upper": upper,
                "basis": f"seed change {seed_delta:.3f} mm3 x {k} cop{'y' if k == 1 else 'ies'}"}, None
    return None, None


def evaluate(tool, args, before, after, exp=None, reason=None):
    """Build the volumeChange record. Returns (record, warning text or None,
    info text or None)."""
    delta = after - before
    rec = {"before_mm3": round(before, 3), "after_mm3": round(after, 3),
           "delta_mm3": round(delta, 3)}
    sign = expected_sign(tool, args)
    if exp is not None:
        rec["expected_delta_mm3"] = round(exp["delta"], 3)
        rec["expected_basis"] = exp["basis"]
        if sign == 0:     # pattern/mirror: the seed's sign
            sign = 1 if exp["delta"] > 0 else -1
    elif reason:
        rec["expected_basis"] = reason

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
    if exp is None:
        rec["check"] = "changed"
        return rec, None, None

    want = abs(exp["delta"])
    upper = exp.get("upper")
    tol = max(ZERO_TOL_MM3, REL_TOL * want)
    got = abs(delta)
    if abs(got - want) <= tol:
        rec["check"] = "matches_expected"
        return rec, None, None
    is_pattern = tool in PATTERN_TOOLS
    if got > want + tol:
        if is_pattern and upper is not None and got <= upper + max(ZERO_TOL_MM3, REL_TOL * upper):
            rec["check"] = "more_than_expected_within_nominal"
            return rec, None, (f"volume changed by {got:.3f} mm3, more than {exp['basis']} = "
                               f"{want:.3f} mm3 but within the seed's profile area x depth per copy "
                               f"({upper:.3f} mm3) -- normal when the seed was partly outside the body "
                               f"or overlapping material and the copies are not")
        rec["check"] = "more_than_expected"
        return rec, (f"the volume changed by {got:.3f} mm3, more than {exp['basis']} "
                     f"= {want:.3f} mm3 -- check the geometry"), None
    rec["check"] = "less_than_expected"
    if is_pattern:
        what = "copies overlap each other or existing material, or fall partly outside the body"
    elif tool == "solidworks_create_cut_extrusion":
        what = "part of the cut is outside the body"
    else:
        what = "it overlaps existing material"
    return rec, None, (f"volume changed by {got:.3f} mm3, less than {exp['basis']} "
                       f"= {want:.3f} mm3 (normal when {what})")


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

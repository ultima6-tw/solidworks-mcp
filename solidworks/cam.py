"""
SOLIDWORKS CAM tools (part milling): stock -> machinable features -> operations
-> toolpaths -> simulation -> G-code.

Verified live on SOLIDWORKS 2022 SP04 + SOLIDWORKS CAM 2022 (Traditional Chinese UI),
built-in post processors only (M3AXIS-TUTORIAL). Probe log: NOTES.md
"SOLIDWORKS CAM" sections; API facts: reference/swcam_api/CAM_API.md.

COM notes (all probed):
  - Entry: ISldWorks.GetAddInObject("CAMWorksAddin.MWAddin").GetCWApp() (default iface ICWApp4).
  - The CAM objects only answer late-bound calls on their DEFAULT interface. To use another
    interface (ICWDoc3, ICWPartDoc, ICWMachine5, ...) a real QueryInterface on a makepy class is
    needed; win32com CastTo does NOT QueryInterface and returns shifted values.
  - ICWMillWorkpiece methods all fail with DISP_E_MEMBERNOTFOUND; the stock is defined through
    ICWDoc3.CreateBoundingBoxWorkPiece2 (offsets in mm).
  - ActiveDocGOP: 2 = regenerate, 1 = retain. NEVER 4 (query): it opens a Yes/No box that
    does not go through the CAM event notifier and blocks the API.
  - Operation names are written into G-code comments; non-ASCII names come out garbled, so
    operations are renamed OP1, OP2, ... (ICWOperation.OperationName is writable).
  - SetController2(False, name, full .ctl path); its return value is unreliable (False on
    success) — read back with GetController2. A folder instead of the .ctl path clears the post
    and PostProcess then silently writes nothing.
  - CreateSTLOfWIPs2(folder, name, ...) concatenates folder + name: the folder needs a trailing
    backslash.
  - Visual simulation: ActiveDocLaunchSimulation / ActiveDocPlaySimulation (returns at once,
    plays ~3 s) / ActiveDocSaveSimulationImage / ActiveDocCloseSimulation. The recording APIs
    (SetActiveDocSimulationRecordAnimation / GetActiveDocSimulationRecordedAnimationFilePath)
    crashed SOLIDWORKS (heap corruption) — not used.
"""

import json
import logging
import os
import struct
import tempfile
import threading
import time
from pathlib import Path

import pythoncom
import win32com.client
from mcp.types import Tool

logger = logging.getLogger(__name__)

WORKSPACE_DIR = Path(__file__).parent.parent / "workspace"
TESTED_ON = "SOLIDWORKS 2022 SP04 + SOLIDWORKS CAM 2022"


# ---------------------------------------------------------------- helpers

def _stl_info(path):
    """(bbox_min, bbox_max, enclosed volume) of an ASCII or binary STL, in the file's units."""
    b = open(path, "rb").read()
    if b[:5] == b"solid" and b"facet" in b[:400]:
        vs = [tuple(map(float, l.split()[1:4])) for l in b.decode("latin-1").splitlines()
              if l.strip().startswith("vertex")]
        tris = [vs[i:i + 3] for i in range(0, len(vs), 3)]
    else:
        n = struct.unpack("<I", b[80:84])[0]
        tris = [[struct.unpack("<3f", b[84 + 50 * i + 12 + 12 * k:84 + 50 * i + 24 + 12 * k]) for k in range(3)]
                for i in range(n)]
    vol = 0.0
    for a, c, d in tris:
        vol += (a[0] * (c[1] * d[2] - c[2] * d[1]) - a[1] * (c[0] * d[2] - c[2] * d[0])
                + a[2] * (c[0] * d[1] - c[1] * d[0])) / 6
    pts = [p for t in tris for p in t]
    lo = [round(min(p[i] for p in pts), 3) for i in range(3)]
    hi = [round(max(p[i] for p in pts), 3) for i in range(3)]
    return lo, hi, round(abs(vol), 2)


class _CamDialogGuard:
    """Background safety net while a CAM call runs.

    SOLIDWORKS CAM can raise modal Yes/No boxes (title 'SOLIDWORKS CAM ...') that bypass the
    event notifier and block the COM call forever. After `grace` seconds such a box is answered
    with No (a single-button box with its only button); the text is kept so the tool can report
    it. The non-modal message window and the simulation panel are left alone."""

    IGNORE = ("訊息視窗", "Message Window", "模擬刀具路徑", "Simulate Toolpath")

    def __init__(self, grace=4.0):
        self.grace = grace
        self.seen = []
        self._stop = threading.Event()
        self._first = {}
        self._done = set()

    def __enter__(self):
        self._t = threading.Thread(target=self._run, daemon=True)
        self._t.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        self._t.join(2)
        return False

    def _run(self):
        import win32gui
        import win32con
        while not self._stop.is_set():
            def cb(h, _):
                try:
                    if win32gui.GetClassName(h) != "#32770" or not win32gui.IsWindowVisible(h):
                        return True
                    title = win32gui.GetWindowText(h)
                    if not title.startswith("SOLIDWORKS CAM") or any(s in title for s in self.IGNORE):
                        return True
                    first = self._first.setdefault(h, time.monotonic())
                    if h in self._done or time.monotonic() - first < self.grace:
                        return True
                    kids = []
                    win32gui.EnumChildWindows(
                        h, lambda c, acc: acc.append((win32gui.GetClassName(c), win32gui.GetWindowText(c), c)) or True, kids)
                    buttons = [k for k in kids if k[0] == "Button" and k[1]]
                    text = " ".join(k[1] for k in kids if k[0] == "Static" and k[1])
                    no = [k for k in buttons if k[1].replace("&", "").lower().startswith(("否", "no"))]
                    target = no[0] if no else (buttons[0] if len(buttons) == 1 else None)
                    self._done.add(h)
                    if target:
                        win32gui.PostMessage(target[2], win32con.BM_CLICK, 0, 0)
                    self.seen.append({"title": title, "text": text,
                                      "answered": target[1] if target else None})
                    logger.warning(f"CAM dialog {title!r}: {text!r} -> {target[1] if target else 'not answered'}")
                except Exception:
                    pass
                return True
            try:
                win32gui.EnumWindows(cb, None)
            except Exception:
                pass
            time.sleep(0.3)


class CamTools:
    """SOLIDWORKS CAM milling workflow for the active part."""

    def __init__(self, connection, tracker=None):
        self.connection = connection
        self.tracker = tracker
        self._mod = None          # makepy module of the CAM type library
        self._sim_open = False    # visual simulation panel left open by cam_simulate(show=True)

    def _json_result(self, result, **extra):
        d = {"result": result}
        d.update(extra)
        return json.dumps(d, ensure_ascii=False)

    # ------------------------------------------------------------ tool list

    def get_tool_definitions(self) -> list[Tool]:
        flow = ("SOLIDWORKS CAM milling flow for the ACTIVE PART: cam_setup -> cam_extract_features -> "
                "cam_generate_operations -> cam_generate_toolpath -> cam_simulate -> cam_post. "
                f"Tested on {TESTED_ON} with the built-in post processors only. ")
        return [
            Tool(
                name="solidworks_cam_setup",
                description=flow + "Step 1: define the stock as the part's bounding box plus optional offsets (mm per side), "
                            "and pick the post processor (G-code dialect). Returns machine, material, post processor, the "
                            "available post processors and the stock box. Call before cam_extract_features.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "stockOffsets": {
                            "type": "object",
                            "description": "Extra stock beyond the part bounding box, mm per side (default 0). Z+ is the top in the CAM setup.",
                            "properties": {k: {"type": "number"} for k in ("xPlus", "yPlus", "zPlus", "xMinus", "yMinus", "zMinus")},
                        },
                        "postProcessor": {"type": "string",
                                          "description": "Post processor name from the returned list, e.g. 'M3AXIS-TUTORIAL' (default: keep current)."},
                    },
                },
            ),
            Tool(
                name="solidworks_cam_extract_features",
                description=flow + "Step 2: Extract Machinable Features (EMF) — finds pockets, bosses, holes etc. that "
                            "can be milled/drilled. Returns the features per setup. If 0 are found the part has nothing "
                            "SOLIDWORKS CAM recognises for 2.5-axis milling.",
                inputSchema={"type": "object", "properties": {}},
            ),
            Tool(
                name="solidworks_cam_generate_operations",
                description=flow + "Step 3: Generate Operation Plan from the technology database — picks operations "
                            "(roughing, contour, center drill, drill ...) and tools automatically. Existing operations are "
                            "regenerated. Operations are renamed OP1, OP2, ... because their names become G-code comments "
                            "and non-ASCII names come out garbled. Returns each operation with its tool.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "asciiNames": {"type": "boolean", "description": "Rename operations OP1.. (default true)."},
                    },
                },
            ),
            Tool(
                name="solidworks_cam_generate_toolpath",
                description=flow + "Step 4: Generate toolpaths for all operations. Returns per operation whether a "
                            "toolpath exists and its extents (CAM setup coordinates, mm).",
                inputSchema={"type": "object", "properties": {}},
            ),
            Tool(
                name="solidworks_cam_simulate",
                description=flow + "Step 5: simulate material removal. Always runs a silent simulation and reports "
                            "gouges and undercuts (both should be 0) and the simulated work-in-process volume vs the part "
                            "volume (the excess is material the plan leaves, e.g. stock offsets on faces no feature machines). "
                            "show=true also plays the simulation in the SOLIDWORKS window (isometric view) and leaves the "
                            "panel open for the user to replay; the next CAM tool closes it. imagePath (.jpg/.bmp/.tif) "
                            "saves a picture of the simulation (implies show).",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "show": {"type": "boolean", "description": "Play the simulation on screen (default false)."},
                        "imagePath": {"type": "string",
                                      "description": "Save a simulation image; bare filename goes to workspace/screenshots."},
                    },
                },
            ),
            Tool(
                name="solidworks_cam_post",
                description=flow + "Step 6: post-process to a G-code file with the post processor chosen in cam_setup. "
                            "Also writes a .set tool list next to it. Returns line count, tool changes and the X/Y/Z ranges "
                            "of the program. NOTE: Z0 of the program is the CAM setup origin (on the probed part: the part "
                            "top, NOT the stock top) — check the Z range against the part before cutting.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "outputPath": {"type": "string",
                                       "description": "G-code file. Bare filename (e.g. 'bracket.nc') goes to workspace/cam; '.nc' added if no extension."},
                    },
                    "required": ["outputPath"],
                },
            ),
        ]

    def execute(self, tool_name: str, args: dict) -> str:
        self.connection.ensure_connection()
        dispatch = {
            "solidworks_cam_setup": self.cam_setup,
            "solidworks_cam_extract_features": self.cam_extract_features,
            "solidworks_cam_generate_operations": self.cam_generate_operations,
            "solidworks_cam_generate_toolpath": self.cam_generate_toolpath,
            "solidworks_cam_simulate": self.cam_simulate,
            "solidworks_cam_post": self.cam_post,
        }
        handler = dispatch.get(tool_name)
        if not handler:
            raise Exception(f"Unknown CAM tool: {tool_name}")
        return handler(args or {})

    # ------------------------------------------------------------ COM plumbing

    def _cw(self):
        addin = self.connection.app.GetAddInObject("CAMWorksAddin.MWAddin")
        if addin is None:
            raise Exception("SOLIDWORKS CAM add-in is not loaded (Tools > Add-Ins > SOLIDWORKS CAM).")
        cw = addin.GetCWApp()
        if self._mod is None:
            from win32com.client import gencache
            tlib, _ = cw._oleobj_.GetTypeInfo().GetContainingTypeLib()
            a = tlib.GetLibAttr()
            self._mod = gencache.EnsureModule(str(a[0]), a[1], a[3], a[4])
            if self._mod is None:
                raise Exception("Could not generate the SOLIDWORKS CAM type library wrapper (makepy).")
        if self._sim_open:
            try:
                self._qi(cw, "ICWApp4").ActiveDocCloseSimulation()
            except Exception as e:
                logger.info(f"close simulation: {e}")
            self._sim_open = False
        return cw

    def _qi(self, obj, iface):
        cls = getattr(self._mod, iface)
        return cls(obj._oleobj_.QueryInterface(cls.CLSID, pythoncom.IID_IDispatch))

    @staticmethod
    def _tname(obj):
        try:
            return obj._oleobj_.GetTypeInfo().GetDocumentation(-1)[0]
        except Exception:
            return "?"

    @staticmethod
    def _items(c):
        return [c.Item(i) for i in range(c.Count)] if c is not None else []

    def _ctx(self):
        """(cw, ICWDoc3, ICWPartDoc, ICWMachine5) for the active part."""
        doc = self.connection.get_active_doc()
        if doc is None or int(doc.GetType) != 1:
            raise Exception("SOLIDWORKS CAM tools need an active PART document.")
        cw = self._cw()
        cwdoc = cw.IGetActiveDoc()
        if cwdoc is None:
            raise Exception("SOLIDWORKS CAM has no document for the active part.")
        pd = self._qi(cwdoc, "ICWPartDoc")
        d3 = self._qi(cwdoc, "ICWDoc3")
        m5 = self._qi(pd.IGetMachine(), "ICWMachine5")
        return cw, d3, pd, m5

    def _operations(self, m5):
        ops = []
        for s in self._items(m5.IGetEnumOpSetups()):
            for o in self._items(self._qi(s, "ICWBaseOpSetup").IGetEnumOperations()):
                ops.append(self._qi(o, "ICWOperation"))
        return ops

    def _op_info(self, op):
        info = {"name": op.OperationName}
        try:
            tool = op.IGetTool()
            p = op.GetCutterRecordParams()
            info["tool"] = {"kind": self._tname(tool).replace("ICW", ""),
                            "diameter_mm": round(p[0], 3), "cornerRadius_mm": round(p[1], 3)}
        except Exception as e:
            info["tool"] = f"? {e}"
        return info

    def _stock_box(self, m5):
        p = os.path.join(tempfile.mkdtemp(prefix="swmcp_cam_"), "stock.stl")
        if not m5.CreateSTLOfStock(p, "", 0) or not os.path.exists(p):
            return None
        lo, hi, vol = _stl_info(p)
        return {"min_mm": lo, "max_mm": hi, "volume_mm3": vol}

    def _part_volume(self):
        from .com_utils import com_prop
        mp = com_prop(self.connection.get_active_doc().Extension, "CreateMassProperty")
        return mp.Volume * 1e9

    @staticmethod
    def _resolve(path, subdir, default_ext):
        p = Path(path)
        if not p.is_absolute():
            p = WORKSPACE_DIR / subdir / p
        if not p.suffix:
            p = p.with_suffix(default_ext)
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    # ------------------------------------------------------------ tools

    def cam_setup(self, args):
        cw, d3, pd, m5 = self._ctx()
        notes = []
        mat = self._qi(m5.IGetWorkpiece(), "ICWWorkpiece").Material
        off = args.get("stockOffsets") or {}
        vals = [float(off.get(k, 0.0)) for k in ("xPlus", "yPlus", "zPlus", "xMinus", "yMinus", "zMinus")]
        if any(v < 0 for v in vals):
            raise Exception("stockOffsets must be >= 0")
        with _CamDialogGuard() as g:
            ok = d3.CreateBoundingBoxWorkPiece2(*vals, False, False, False, mat, False)
        if not ok:
            raise Exception("CreateBoundingBoxWorkPiece2 failed")
        posts = list(m5.GetPostProcessorList(False) or [])
        cur = m5.GetController2()
        post = args.get("postProcessor")
        if post:
            match = [p for p in posts if p.lower() == post.lower()]
            if not match:
                raise Exception(f"Unknown post processor {post!r}; available: {posts}")
            ctl = os.path.join(os.path.dirname(cur[2]), match[0] + ".ctl")
            if not os.path.exists(ctl):
                raise Exception(f"Post processor file not found: {ctl}")
            m5.SetController2(False, match[0], ctl)   # return value unreliable: read back
            cur = m5.GetController2()
            if Path(cur[2]).stem.lower() != match[0].lower():
                raise Exception(f"Post processor did not change (now {cur[2]!r})")
        try:
            machine = self._qi(pd.IGetMachine(), "ICWMillMachine").Name
        except Exception:
            machine = None
        if g.seen:
            notes.append(f"dialogs auto-answered: {g.seen}")
        return self._json_result(
            f"✓ CAM setup: stock = part bounding box + offsets {vals} mm, post {Path(cur[2]).stem}",
            machine=machine, material=mat, postProcessor=Path(cur[2]).stem, postFile=cur[2],
            availablePostProcessors=posts, stockOffsets_mm=dict(zip(
                ("xPlus", "yPlus", "zPlus", "xMinus", "yMinus", "zMinus"), vals)),
            notes=notes, testedOn=TESTED_ON, type="cam_setup")

    def cam_extract_features(self, args):
        cw, d3, pd, m5 = self._ctx()
        with _CamDialogGuard() as g:
            n = d3.ExtractMachinableFeatures()
        setups = []
        for s in self._items(m5.IGetEnumSetups()):
            bs = self._qi(s, "ICWBaseSetup")
            feats = [self._qi(f, "ICWFeature").FeatureName for f in self._items(bs.IGetEnumFeatures())]
            setups.append({"setup": bs.SetupName, "features": feats})
        return self._json_result(
            f"✓ {n} machinable feature(s) extracted" if n else "⚠ No machinable features found",
            count=n, setups=setups, stock=self._stock_box(m5), dialogs=g.seen, type="cam_features")

    def cam_generate_operations(self, args):
        cw, d3, pd, m5 = self._ctx()
        with _CamDialogGuard() as g:
            cw.ActiveDocGOP(2)    # regenerate; never 4 (query) — blocks on a Yes/No box
        ops = self._operations(m5)
        renamed = []
        if args.get("asciiNames", True):
            for k, op in enumerate(ops):
                old = op.OperationName
                op.OperationName = f"OP{k + 1}"
                renamed.append([old, op.OperationName])
        info = [self._op_info(op) for op in ops]
        for i, r in zip(info, renamed):
            i["originalName"] = r[0]
        if not ops:
            return self._json_result("⚠ No operations generated (run cam_extract_features first?)",
                                     operations=[], dialogs=g.seen, type="cam_operations")
        return self._json_result(f"✓ {len(ops)} operation(s) generated", operations=info,
                                 dialogs=g.seen, type="cam_operations")

    def cam_generate_toolpath(self, args):
        cw, d3, pd, m5 = self._ctx()
        with _CamDialogGuard() as g:
            cw.ActiveDocGTP()
        out, missing = [], []
        for op in self._operations(m5):
            i = {"name": op.OperationName, "generated": bool(op.GetIsToolpathGenerated())}
            if i["generated"]:
                e = op.GetToolpathExtents()
                i["extents_mm"] = {"x": [round(e[0], 3), round(e[1], 3)], "y": [round(e[2], 3), round(e[3], 3)],
                                   "z": [round(e[4], 3), round(e[5], 3)]}
            else:
                missing.append(i["name"])
            out.append(i)
        if not out:
            raise Exception("No operations — run cam_generate_operations first.")
        msg = (f"✓ Toolpaths generated for {len(out)} operation(s)" if not missing
               else f"⚠ No toolpath for {missing}")
        return self._json_result(msg, operations=out, dialogs=g.seen, type="cam_toolpath")

    def cam_simulate(self, args):
        cw, d3, pd, m5 = self._ctx()
        with _CamDialogGuard() as g:
            r = d3.RunSimulationSilent(False, False, False)
        gouges, undercuts = int(r[0]), int(r[1])
        folder = tempfile.mkdtemp(prefix="swmcp_cam_")
        wip = None
        # folder + name are concatenated as-is (no extension added): the file is <folder>\wip
        if m5.CreateSTLOfWIPs2(folder + "\\", "wip", "", 0, 0):
            stl = os.listdir(folder)
            if stl:
                lo, hi, vol = _stl_info(os.path.join(folder, stl[0]))
                wip = {"min_mm": lo, "max_mm": hi, "volume_mm3": vol}
        part = round(self._part_volume(), 2)
        extra = {"gouges": gouges, "undercuts": undercuts, "partVolume_mm3": part, "wip": wip}
        if wip:
            extra["wipMinusPart_mm3"] = round(wip["volume_mm3"] - part, 2)
        image = args.get("imagePath")
        if args.get("show") or image:
            doc = self.connection.get_active_doc()
            doc.ShowNamedView2("*Isometric", 7)
            doc.ViewZoomtofit2()
            a4 = self._qi(cw, "ICWApp4")
            a4.ActiveDocLaunchSimulation()
            a4.ActiveDocPlaySimulation()    # returns immediately; playback takes a few seconds
            self._sim_open = True
            extra["shown"] = True
            if image:
                p = self._resolve(image, "screenshots", ".jpg")
                if p.suffix.lower() not in (".jpg", ".bmp", ".tga", ".tif"):
                    raise Exception("imagePath must be .jpg, .bmp, .tga or .tif")
                time.sleep(5)               # let the playback finish (no completion API)
                if not a4.ActiveDocSaveSimulationImage(str(p)) or not p.exists():
                    raise Exception(f"Saving the simulation image failed: {p}")
                extra["image"] = str(p)
        ok = gouges == 0 and undercuts == 0
        msg = ("✓ Simulation: no gouges, no undercuts" if ok
               else f"⚠ Simulation: {gouges} gouge(s), {undercuts} undercut(s)")
        return self._json_result(msg, dialogs=g.seen, type="cam_simulation", **extra)

    def cam_post(self, args):
        cw, d3, pd, m5 = self._ctx()
        out = self._resolve(args["outputPath"], "cam", ".nc")
        if not d3.IsPostingAllowed():
            raise Exception("Posting not allowed: generate toolpaths first and make sure a post processor is set (cam_setup).")
        before = out.stat().st_mtime if out.exists() else None
        with _CamDialogGuard() as g:
            pd.PostProcess(str(out))
        if not out.exists() or (before is not None and out.stat().st_mtime == before):
            raise Exception(f"PostProcess wrote no file at {out}")
        lines = out.read_text(encoding="latin-1").splitlines()
        rng = {"X": [], "Y": [], "Z": []}
        tools = []
        for line in lines:
            words = line.split()
            if "M06" in words:
                tools += [w for w in words if w.startswith("T")]
            for w in words:
                if w[:1] in rng:
                    try:
                        rng[w[0]].append(float(w[1:]))
                    except ValueError:
                        pass
        ranges = {k: [min(v), max(v)] if v else None for k, v in rng.items()}
        set_file = out.with_suffix(".set")
        return self._json_result(
            f"✓ G-code written: {out} ({len(lines)} lines, {len(tools)} tool change(s))",
            path=str(out), lines=len(lines), toolChanges=tools, ranges_mm=ranges,
            postProcessor=Path(m5.GetController2()[2]).stem,
            toolList=str(set_file) if set_file.exists() else None,
            head=lines[:12], dialogs=g.seen, type="cam_post")

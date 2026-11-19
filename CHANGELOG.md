# Changelog

What changed in this fork, newest first. Entries marked **Update recommended**
fix a tool that could report success while it had not done what was asked.

To update a git checkout: run `git pull` in its folder, then restart the MCP
server (restart the AI app, or its MCP connection), and in SSH mode also the
desktop bridge host. The server tells you when GitHub has commits your copy does
not have — see [INSTALL.md › Updates](INSTALL.md#updates).
繁體中文：[CHANGELOG.zh-TW.md](CHANGELOG.zh-TW.md).

## 2026-11-19

- `recognize_features`: when SOLIDWORKS reports FeatureWorks loaded but does
  not actually start it (seen on SOLIDWORKS 2022), the error now says so and
  asks to tick FeatureWorks in Tools › Add-Ins. It used to say a Professional
  or Premium license was needed, which was not the problem. A missing
  FeatureWorks and a license error are now told apart too.
- Sketch tools no longer fail on a small shape next to other geometry when
  the SOLIDWORKS window is small. Snapping reaches a fixed number of screen
  pixels, so in a smaller window (or zoomed further out) it reaches further in
  mm; seen on SOLIDWORKS 2022 in a 1024×640 or 1536×960 window, a 7×14 mm
  rectangle below two small circles made SOLIDWORKS draw nothing, and the tool
  reported an error. The tool now draws it again with snapping off, as it
  already did when snapping resized a shape.

## 2026-11-18

- **Update recommended — tools that change the solid report its volume
  before and after** (`volumeChange`). A feature can come back with no error
  code and still do nothing: seen on SOLIDWORKS 2022, a boss drawn entirely
  inside the body reported ✓ and added nothing. Now no change, or a change the
  wrong way (a boss that removed material), turns ✓ into ⚠. For a BLIND
  extrusion or cut the tool also works out the expected change itself
  (profile area from SOLIDWORKS × depth) and flags a change larger than that;
  a smaller one is only noted, since a cut partly outside the body or a boss
  overlapping existing material is normal. Linear and circular patterns and
  mirrors expect the seed feature's change × the number of copies (when the
  seed was made with these tools in this session).
- `solidworks_batch`: a call that ran but came back ⚠ now makes the batch
  summary ⚠ as well and is named there (`"warning": true` on that call). The
  summary used to read ✓ whenever every call ran.
- `mirror` (mirror plane), `draft` (neutral plane) and `ref_plane` (reference
  plane) accept the ID `ref_plane` returns (`ref:Plane1`); they used to accept
  only the plane's name.
- **Update recommended — `linear_pattern` with `{"axis": "X"}` could run
  toward −X.** SOLIDWORKS patterns along the direction of the edge it is
  given, and the tool used whichever parallel edge it found first; on a plain
  block X and Z ran the wrong way. `{"axis": ...}` now always means +axis, and
  `reverseDir1`/`reverseDir2` −axis, in both pattern directions.
- **Update recommended — SOLIDWORKS no longer piles up handles on every
  feature tool.** Every COM object SOLIDWORKS hands to the server leaves
  handles in the SOLIDWORKS process that are never freed, and the server's
  after-call checks walked the whole feature tree several times per call. In a
  long test session SOLIDWORKS reached almost 3 million handles, and each call
  got slower as the part grew. The checks and the tools now read only the last
  few features: on a 60-feature part a cut costs ~300 handles instead of
  ~6000 (and ~1.3 s instead of ~6.5 s), a new sketch ~450 instead of ~1600.
- **Update check at start-up.** The server asks GitHub once, in the background,
  whether your copy is behind, and the first tool result of the session says so.
  One request to the public GitHub API; nothing about you, your machine or your
  documents is sent. Turn it off with `SOLIDWORKS_MCP_UPDATE_CHECK=0`. Details:
  [INSTALL.md › Updates](INSTALL.md#updates).
- **Update recommended — every feature tool now reads the feature's error
  state** (`2c10219`). SOLIDWORKS can return a feature that is in error or
  carries a warning and did nothing, and the tool used to report ✓. Seen on
  SOLIDWORKS 2022: a linear pattern or mirror whose copies land outside the body
  (warning 51, nothing added). Now a feature in error is removed and the call
  fails with the official SOLIDWORKS description; a feature with a warning is
  kept and the result is marked ⚠ instead of ✓. Whether SOLIDWORKS returns
  "nothing" or such a feature can change during a long session, so this cannot
  be left to each tool.

## 2026-11-17

- **Update recommended — sketch tools check what was drawn** (`df070a2`,
  `9e849c8`). At a zoomed-out view SOLIDWORKS snaps new sketch geometry onto
  nearby geometry and still reports success: a 1.7 mm radius circle next to a
  rectangle came out 3.5 mm, a 7 mm wide rectangle came out 3.4 mm, arcs, slots,
  points and centerlines moved by up to 2.5 mm. Rectangles, circles, lines, arcs,
  polygons, ellipses, slots, splines, points and centerlines are now read back
  and redrawn exactly with snapping off when they differ; `sketch_profile` is
  checked and removed with an error if it came out wrong.
- **Update recommended — cut and extrude features in error** (`8c0abc8`,
  `46439d5`). A Through-All cut pointing away from the body could report ✓ and
  cut nothing; it now tries the other direction. A boss pointing away from every
  body is reported as an error instead of ✓.
- `get_sketch_entities` also returns the sketch's actual geometry (`geometry`),
  read from SOLIDWORKS — after dimensions/relations moved it, and including
  geometry not drawn by these tools (`9478b85`).
- `export_dxf` no longer hangs when the server runs over stdio on Windows
  (`511f4c2`).
- New tools: `export_dxf` (planar face → 1:1 mm DXF), `get_assembly_bounding_box`,
  `screenshot` (SOLIDWORKS window only), `execute_python` (off unless you turn it
  on), and SOLIDWORKS CAM milling tools (stock → toolpaths → simulation → G-code).
- [INSTALL.md](INSTALL.md) (a setup guide written for the AI that installs the
  server) and [TESTING.md](TESTING.md), in English and Traditional Chinese.

## 2026-11-16 — fork of HarrierPigeon/Solidworks-MCP-Server

- Works with localized (non-English) SOLIDWORKS; `test.py` is
  language-independent.
- MCP SDK limited to 1.x (`mcp>=1.26.0,<2`): 2.x removed the API the server uses.
- SSH mode: a desktop bridge host, so the server can be used from SSH and other
  non-desktop sessions.
- Fixes found by testing every tool on SOLIDWORKS 2022: extrusion `reverse`,
  equal-distance chamfer, inscribed polygon, small sketch entities, draft,
  reference axis and point, circular pattern argument order, cosmetic thread,
  boundary boss/cut, rib, wrap, cut loft, hole wizard, `move_to_folder`, face
  sample points, empty-sketch tracking.

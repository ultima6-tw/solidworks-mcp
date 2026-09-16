# Windows MCP SDK and SOLIDWORKS Localization Fix

This document summarizes the Windows and localized-SOLIDWORKS compatibility
fix for `Solidworks-MCP-Server` v0.4.0. It is intended for maintainers, GitHub
issues, and pull requests, and records only the problem, root cause, fix, and
verification.
## 1. Environment

- Windows x64
- Codex CLI 0.154.0
- SOLIDWORKS 2025
- SOLIDWORKS COM revision 33.1.2
- Simplified Chinese SOLIDWORKS UI
- Python 3.12
- pywin32
- Solidworks-MCP-Server v0.4.0
- MCP over stdio
- Compatible MCP SDK: `mcp==1.26.0`
No account identifiers, credentials, tokens, or machine-specific user paths
are required by this fix.
## 2. Problem 1 — MCP Starts but Codex Gets No Tools

### Symptoms

- The MCP configuration was shown as enabled.
- `server.py` was started repeatedly.
- Logs repeatedly contained:
  ```text
  Starting SolidWorks MCP Server (Modular Edition)...
  ```
- `ClientSession.initialize()` ended with `Connection closed`.
- Codex could not discover any `solidworks_*` tools.
`Connection closed` was only the client-visible symptom. The useful exception
occurred inside the MCP server during initialization.
### Root cause

The original dependency declaration allowed any newer MCP release:
```text
mcp>=1.26.0
```
The environment installed MCP 2.2.0, while server v0.4.0 still used the MCP
1.x API:
```python
Server.list_tools()
```
MCP 2.2.0 does not provide that API, so initialization failed with:
```text
AttributeError: 'Server' object has no attribute 'list_tools'
```
The stdio transport then closed and surfaced as `Connection closed` in the
client.
### Fix

The dependency was pinned in both installation manifests:
```text
mcp==1.26.0
```
Changed files:
- `requirements.txt`
- `pyproject.toml`
### Verification

An independent stdio client initialized the server and listed its tools:
```text
MCP HANDSHAKE OK
TOOL COUNT: 95
```
Codex Desktop also discovered the same 95 `solidworks_*` tools.
## 3. Problem 2 — Localized SOLIDWORKS Compatibility

### Templates

The original code assumed English template names:
```text
Part.prtdot
Assembly.asmdot
```
The tested Simplified Chinese installation used:
```text
gb_part.prtdot
gb_assembly.asmdot
```
Consequently, `new_part` and `new_assembly` could fail even though usable
templates were installed.
### Standard planes

The original code also assumed English feature names:
```text
Front Plane
Top Plane
Right Plane
```
The tested installation exposed:
```text
前视基准面
上视基准面
右视基准面
```
Affected or potentially affected operations included:
- `new_part`
- `new_assembly`
- `create_sketch`
- `mirror`
- `draft`
- reference-plane creation
- assembly plane mates
- feature-tree boilerplate filtering
## 4. Fix

### `solidworks/connection.py`

Template discovery now follows this order:
1. Read default part, assembly, and drawing templates through SOLIDWORKS COM
   user preferences.
2. Accept a preference only if the file exists and has the expected template
   extension.
3. Otherwise scan installed `SOLIDWORKS *\templates` directories.
4. Prefer the directory matching the active COM version.
5. Prefer ordinary root templates and conventional English or `gb_*` names.
6. Use MBD templates only as a final fallback.
The chosen template source and path are logged.
### `solidworks/selection_helpers.py`

A shared resolver now handles the semantic keys `Front`, `Top`, and `Right`:
1. Traverse features and collect `RefPlane` objects.
2. Read COM reference-plane geometry when available.
3. Match the dominant normal axis and prefer the origin plane over parallel
   offset planes.
4. Fall back to known English and Simplified Chinese aliases.
5. Use default plane order only when geometry and aliases are unavailable.
Display names are therefore fallbacks rather than the primary standard-plane
identity. Marked selection helpers also use this resolver, covering mirror,
draft, and reference-plane workflows.
### `solidworks/sketching.py`

The English-only local `plane_map` was removed. `create_sketch` now uses the
shared resolver for standard and custom reference planes.
### `solidworks/feature_tree.py`

Feature-tree filtering no longer compares planes with a fixed English
`STOCK_PLANES` set. It resolves and filters the document's actual stock-plane
names.
### `solidworks/assembly.py`

Assembly plane-mate selection resolves the real plane name from the component
model before constructing the assembly-qualified selector. English and Chinese
aliases remain as fallback for lightweight or suppressed components.
### `test_localization_compat.py`

Fake-COM regression tests cover:
- valid COM template preferences
- invalid or virtual preference fallback
- current-version root templates taking priority over MBD templates
- geometry-based resolution with shuffled localized plane names
- Simplified Chinese alias fallback
- marked plane selection
- localized component-plane selection for assembly mates
## 5. Verified Result

### MCP protocol

```text
MCP handshake: PASS
Tools discovered: 95
```
### Localized standard planes

```text
Front -> 前视基准面
Top   -> 上视基准面
Right -> 右视基准面
```
COM geometry identified these by the Z, Y, and X normal axes respectively.
### End-to-end operations

```text
new_part: PASS
create_sketch(Front): PASS
extrude: PASS
save SLDPRT: PASS
STEP export: PASS
cold start: PASS
```
Cold-start verification confirmed that Codex could launch the configured stdio
server, discover 95 tools, connect to a newly started SOLIDWORKS instance, and
create a separate disposable sketch-and-extrude part.
### Smoke-test geometry

```text
Requested size: 50 x 50 x 50 mm
Bounding box:   50 x 50 x 50 mm
Volume:         125000 mm^3
Faces:          6
Edges:          12
```
The native part was saved. The STEP file was non-empty and contained a
manifold BREP representation, and the saved model state was read back
successfully.
The reported 0.125 kg test mass is not a real material mass. No SOLIDWORKS
Material was assigned, so the calculation used the default density of
1000 kg/m^3.
## 6. Remaining Limitations

- The Simplified Chinese part-modeling path was verified on a real SOLIDWORKS
  2025 installation.
- A complete localized assembly mating workflow has not been verified end to
  end on a real assembly.
- Assembly localization is covered by a Fake-COM regression test.
- Renamed stock planes can defeat name fallback when COM geometry is missing.
- Lightweight or suppressed components may expose incomplete geometry and
  require alias fallback.
- Languages other than English and Simplified Chinese were not verified.
- If geometry, aliases, and normal stock-plane ordering are all unavailable,
  resolution may still fail or choose an incorrect plane.
This fix is verified for the tested Windows and Simplified Chinese SOLIDWORKS
2025 environment; it is not a claim of universal localization support.
## 7. Modified Files

- `pyproject.toml` — pins the compatible MCP SDK version.
- `requirements.txt` — pins the same version for direct installs.
- `solidworks/connection.py` — adds COM-first, version-aware template lookup.
- `solidworks/selection_helpers.py` — adds shared geometry-first plane lookup.
- `solidworks/sketching.py` — uses the shared plane resolver.
- `solidworks/feature_tree.py` — filters resolved localized stock planes.
- `solidworks/assembly.py` — resolves component planes before mate selection.
- `test_localization_compat.py` — adds localization regression coverage.
## 8. Contribution / Upstream Notes

For easier upstream review, the work can be split into independent pull
requests:
1. MCP SDK version compatibility
2. Default and localized template discovery
3. Locale-independent standard-plane resolution
4. Localization regression tests
This is only a suggested contribution structure. No remote branch or pull
request is required for the local fix.

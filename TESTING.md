# Testing

[繁體中文](TESTING.zh-TW.md)

There is no mock of the SOLIDWORKS COM layer: every test drives a real, running
SOLIDWORKS. This page explains how to run the tests, what they cover, what they
were run on, and what is known to be unreliable.

## Tested environment

| | |
|---|---|
| SOLIDWORKS | 2022 SP04 (30.4.0), Education Edition, **Traditional Chinese user interface** |
| SOLIDWORKS CAM | 2022 (needed only for the CAM tools) |
| Windows | Windows 11 |
| Python | 3.13 for the test runs; the server was also checked end to end with 3.12 |

Nothing else has been tested. Other SOLIDWORKS versions and languages may work
(the code avoids localized names), but that is not verified. If you run the
tests on another setup, please open an issue with the results.

## Before you run the tests

> **The test runner closes every open SOLIDWORKS document without saving, before
> it starts and between tests.** Save your work, or use a SOLIDWORKS session you
> are not using for anything else.

- Run the tests **on the Windows desktop** where SOLIDWORKS runs (a normal
  terminal, not an SSH session): `test.py` talks to SOLIDWORKS directly and does
  not use the SSH bridge.
- Files the tests save go to new folders under `%TEMP%` (`swmcp_*`); the tests
  never delete them.
- `mcp_screenshot` briefly opens a small red window on top of SOLIDWORKS (it
  checks that other windows are not captured). Don't click while it runs.
- `mcp_cam_flow` needs the SOLIDWORKS CAM add-in; `recognize_features` tests
  need FeatureWorks (SOLIDWORKS Professional or Premium).

## Running

From the project folder, with the project's Python environment (see
*Manual install* in the [README](README.md)):

```powershell
python test.py --list                       # every test with its id and category
python test.py --test mcp_export_dxf        # one test
python test.py --category "Sketch Tools"    # one category
python test.py --gui                        # pick tests interactively
python test.py                              # everything
```

- `--test` takes **one** id. `--test a --test b` runs only `b`; run them one
  after another instead.
- A test prints a ✓ or ❌ line for each check, then `PASSED` / `FAILED`, and the
  run ends with a summary (`Passed : n`, `Failed : n`). The exit code is 0 only
  if everything passed.
- Details go to `solidworks_mcp.log` in the project folder.

## What is covered

90 tests in 6 categories (`python test.py --list` for the full list):

| Category | Tests | What they check |
|---|---|---|
| Basic | 3 | Every `.py` file compiles and the bridge modules import, and the start-up update check behaves (both without SOLIDWORKS); a cube built end to end |
| Sketch Tools | 12 | Each sketch entity, dimensions, constraints, construction geometry |
| Feature Tools | 9 | Cut-extrude, fillet, chamfer, shell, revolve, loft, reference plane, mass properties, feature list |
| MCP Tools | 57 | The tools as an AI calls them (through the server's tool router), with the results checked against expected geometry, volumes, IDs and files. One of them (`mcp_stdio_transport`) starts `server.py` as a real MCP stdio server and sends the calls through the pipe, because a hang that only happens over stdio cannot show up in the others |
| Assembly | 8 | Save / activate / close documents, inserting components, mates, interference and suppression, per-document state, parametric changes, bounding box |
| Integration | 1 | A longer sequence of cut-extrudes on one body |

Every one of the 105 tools is called by at least one test, either in `test.py`
or in `tests/agent_capability_tests.md` — 46 prompts you paste into an AI app to
test the server end to end. All 46 passed through Claude Code (over the SSH
bridge) on the setup above.

## Results and known problems

**The pass criterion is: each test passes when run on its own** (`--test <id>`).
On the setup above, every test does, with the two exceptions below.

The last full run, 2026-11-18, ran all 85 in one go in category order and passed
85/85 (22 min, SOLIDWORKS 2022 SP4 freshly restarted). It used a runner that calls
the same test functions but, unlike `python test.py`, does not close documents
that were open before the run. An earlier `python test.py` run (73 tests at the
time) passed 70; its 3 failures differed from the run before and passed on their
own. They involved picking geometry by coordinates or "the active document"; the
cause was never found and they did not come back in the 85/85 run. `update_check`
was added after that run and passes on its own.

Known unreliable even on their own (the tool descriptions tell the AI the same):

| Tool | Problem | What to do instead |
|---|---|---|
| `set_dimension_value` | Picks the dimension by its position on screen; sometimes finds nothing (`No dimension found at (x, y)`). Passed and failed on different runs. | `set_parameter` with the dimension's name (`D1@Sketch1`; `list_parameters` lists them) — does not depend on the view. |
| `suppress_component` | In the test suite, the next assembly tool sometimes reports `Active document is not an assembly`. Could not be reproduced outside the test suite. | `activate_document` on the assembly, then retry. Not yet confirmed to fix the failure. |

Seen only in full runs (passed on their own): the integration cut sequence
(one cut removed no material) and `mcp_linear_pattern`.

## Testing through an AI app

`tests/agent_capability_tests.md` holds 46 prompts with expected results
(volumes, counts, positions). Paste them one at a time into Claude Desktop or
Claude Code with this server installed and compare the answers. This tests what
`test.py` cannot: whether an AI picks the right tools from their descriptions.

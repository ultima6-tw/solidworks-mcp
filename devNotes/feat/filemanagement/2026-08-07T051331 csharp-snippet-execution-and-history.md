# C# snippet execution and history

Implemented the first agent-generated C# milestone without changing or removing the existing MCP tool surface.

## What changed

- Added `solidworks_compile_csharp` and `solidworks_execute_csharp`.
- Added a per-request .NET 8/Roslyn worker that compiles snippets in memory and, for execution, attaches only to an already-running SolidWorks session.
- Injected strongly typed `swApp`, `model`, `extension`, `selection`, `featureManager`, and `sketchManager` objects while retaining raw SolidWorks interop access.
- Added process/document target validation, compilation diagnostics, timeouts, bounded output, exception capture, rebuild reporting, state snapshots/diffs, and tracker reconciliation.
- Added a versioned local SQLite snippet history with exact source, hashes, outcomes, diagnostics, symbols, pseudonymous document identity, and read-only JSONL/CSV analysis exports.
- Redacted snippet source recursively from ordinary server logs, including C# calls nested in `solidworks_batch`.
- Preserved offline compilation: the server does not connect to or launch SolidWorks for compile-only requests.
- Added worker build/release packaging, installer support for the worker release asset, documentation, and a complete direct-tool audit.
- Hardened the final subprocess boundary after independent review: Python drains
  stdout/stderr with byte caps instead of buffering unbounded output, the
  worker caps its total response and exception text, and ordinary plus raw
  console-output paths cannot exhaust MCP memory.
- Removed build-machine SolidWorks interops from the release. The worker loads
  and compiles against the end user's installed interops, preventing split
  assembly identities across SolidWorks versions.
- Labeled resolved versus candidate API symbols and retained occurrence counts
  and source lines so failed overload guesses do not pollute wrapper-candidate
  analysis.
- Made optional-worker installation best-effort, compile-probed new downloads,
  and preserved a previously working worker when an update asset is absent or
  invalid.

The C# path is explicitly a trusted-local automation feature, not a security sandbox. Snippets use native SolidWorks API units (meters/radians), while existing semantic MCP tools continue to use millimeters/degrees.

## Verification

- Published the self-contained `win-x64` worker successfully with .NET SDK 8.0.423.
- Ran 35 focused Python tests covering history/migrations/concurrency, exports,
  spreadsheet-safe CSV, bounded subprocess pipes, protocol failures, timeouts,
  recursive redaction, the registered offline MCP handler, all 97 unique tool
  registrations, state reconciliation, installed-interop loading, clean release
  layout, and published-worker compilation: all passed.
- Python syntax compilation and PowerShell parser checks passed.
- `python test.py --list` successfully discovered the existing legacy COM test
  registry without mutating a SolidWorks document.
- Compiled the project-brief `SelectByID2` sample through the published worker: success.
- Compiled an invalid API call and verified structured `CS1061` diagnostics at the snippet source line.
- Executed against a live SolidWorks 2025 session using a disposable unsaved part:
  - selected Top Plane;
  - created a sketch and circle through generated C#;
  - rebuilt successfully;
  - observed an added-feature state diff and reconciled `sketch:Sketch1`;
  - recorded all attempts in the local audit DB;
  - closed the disposable part without saving and left SolidWorks running.

## Repository handling

- No GitHub push was performed.
- Existing untracked `.codex/config.toml` and `AGENTS.md` files were left untouched and will not be included in the feature commit.

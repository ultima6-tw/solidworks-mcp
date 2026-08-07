# Trusted-local C# execution

SolidWorks MCP keeps its structured tools as the default CAD interface and
adds C# snippets as an escape hatch for uncommon SolidWorks API operations.
Snippets are compiled with Roslyn before they can run, execute in a fresh
worker process, and attach to the same running SolidWorks process and active
document that the Python server inspected.

This feature is **not a security sandbox**. Only execute code you trust.
Submitted code can use .NET APIs, access the computer, start processes, use the
network, and change the active model. Compilation accepts no caller-supplied
assembly paths, but it references the worker's broad .NET trusted-platform set
plus the installed SolidWorks interops. Output limits and the fresh process are
reliability boundaries, not security boundaries.

## Build the worker

Development requires the .NET 8 SDK and a local SolidWorks installation:

```powershell
powershell -File scripts/build_csharp_worker.ps1
```

The script restores the pinned Roslyn package, publishes a clean self-contained
x64 worker to `worker/publish/`, and creates the release asset
`dist/solidworks-csharp-worker-win-x64.zip`. Neither build output is committed.
Published releases should attach that zip using the exact filename above; the
Windows installer discovers, compile-probes, and installs it automatically. The
worker loads the end user's installed interop assemblies rather than bundling
the build machine's copies.

Environment overrides:

- `SWMCP_CSHARP_WORKER`: full path to another worker executable.
- `SOLIDWORKS_INTEROP_DIR`: directory containing
  `SolidWorks.Interop.sldworks.dll` and `SolidWorks.Interop.swconst.dll`.
- `SWMCP_SNIPPET_DB`: alternate snippet-history database path.

## Compile before execution

Pass C# statements, not a project, class, or `Main` method:

```json
{
  "code": "result = model.Extension.SelectByID2(\"Top Plane\", \"PLANE\", 0, 0, 0, false, 0, null, 0);"
}
```

Call `solidworks_compile_csharp` first. It does not start or attach to
SolidWorks and returns:

- Roslyn errors and warnings with snippet-relative line and column numbers.
- Resolved and compiler-candidate SolidWorks interop members, signatures,
  occurrence counts, and source lines. Candidate entries are labeled so failed
  or ambiguous calls are not mistaken for successful API use.
- An execution ID and compilation duration.

The wrapper imports `System`, common collection/LINQ namespaces,
`SolidWorks.Interop.sldworks`, and `SolidWorks.Interop.swconst`. It injects:

```csharp
ISldWorks swApp;
IModelDoc2 model;
IModelDocExtension extension;
ISelectionMgr selection;
IFeatureManager featureManager;
ISketchManager sketchManager;
object? result;
```

Assign a JSON-serializable value to `result` when the caller needs structured
output:

```csharp
result = new
{
    title = model.GetTitle(),
    featureCount = featureManager.GetFeatureCount(false)
};
```

Raw SolidWorks API values use the API's native units—normally meters and
radians—not the millimeters/degrees normalization provided by semantic MCP
tools.

## Execute and verify

`solidworks_execute_csharp` accepts:

- `code` (required)
- `timeoutMs` (1,000–120,000; default 30,000)
- `rebuild` (default `true`)
- `inspectAfter` (default `true`)

Before invocation, Python captures the target SolidWorks process ID and active
document identity. The worker verifies both after attaching and refuses to run
if the user or another automation client switched sessions/documents in the
interim.

After entered execution, the Python server normally:

1. reacquires and identity-checks the active document;
2. rebuilds and scans the feature tree for rebuild errors;
3. compares feature IDs, body/topology counts, bounding box, and mass data;
4. reconciles created, deleted, and renamed top-level features into its tracker.

`IFeature.GetID()` is used to distinguish a rename from deletion/recreation;
SolidWorks documents that this ID is unique within a document and remains
stable when a feature is renamed.

### Failure semantics

- Compilation errors never invoke user code or trigger a rebuild.
- Runtime exceptions can leave partial model changes; rebuild and inspection
  still run when possible.
- A killed worker cannot cancel a COM call already accepted by SolidWorks.
  Timeout responses therefore use `modelState: "unknown"` and require explicit
  inspection before another mutation.
- Closing SolidWorks, switching the active document, or losing COM access during
  post-processing also produces unknown state.
- There is no automatic save and no transactional rollback. SolidWorks Undo is
  not represented as a reliable transaction boundary.
- Captured standard output and error are each limited to 256K characters;
  returned snippet JSON is limited to 1 MiB. Server-side pipe readers kill the
  worker at 2 MiB stdout or 256 KiB stderr, and the worker caps its complete
  protocol response at 2 MiB.

## Local snippet history

Every accepted compile or execute request is recorded, including compiler
failures, runtime failures, timeouts, and worker/precondition errors. The
default database is:

```text
%LOCALAPPDATA%\SolidWorksMCP\snippet_history.sqlite3
```

It contains exact source, returned results, captured output, diagnostics,
resolved/candidate interop symbols, state-change summaries, and outcomes. Any of those
fields can contain sensitive literal values. The database remains local and is
never uploaded automatically. Document names and paths are not stored as
dedicated metadata; a database-local HMAC creates a stable pseudonymous
document identifier instead. Because the HMAC key lives in the same database,
this is useful for grouping attempts but is not anonymization against someone
who possesses the database and can guess likely titles or paths.
There is no automatic expiry or pruning; the database grows until you archive
or delete it.

Summarize likely wrapper/tool candidates:

```powershell
python scripts/export_csharp_history.py
```

Export for offline analysis:

```powershell
python scripts/export_csharp_history.py --format jsonl --output snippet_exports/snippets.jsonl
python scripts/export_csharp_history.py --format csv --status completed --since 2026-08-01 --output snippet_exports/snippets.csv
python scripts/export_csharp_history.py --format jsonl --without-code --output snippet_exports/without-source.jsonl
```

The documented `snippet_exports/` directory is ignored by Git. Treat **all**
exports as sensitive: `--without-code` removes only the exact source field;
results, output, diagnostics, options, and other fields may still contain
paths, secrets, formulas, or literal values. CSV text cells are prefixed when
needed to prevent spreadsheet formula execution. Deleting the SQLite database
removes the retained local corpus; the server recreates an empty database on
the next request.

## API references

- [Microsoft.CodeAnalysis.CSharp package](https://www.nuget.org/packages/Microsoft.CodeAnalysis.CSharp)
- [SOLIDWORKS `IFeature.GetID`](https://help.solidworks.com/2026/english/api/sldworksapi/solidworks.interop.sldworks~solidworks.interop.sldworks.ifeature~getid.html)
- [SOLIDWORKS `ISldWorks.GetProcessID`](https://help.solidworks.com/2016/English/api/sldworksapi/SolidWorks.Interop.sldworks~SolidWorks.Interop.sldworks.ISldWorks~GetProcessID.html)

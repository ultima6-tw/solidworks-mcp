using System.Diagnostics;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Runtime.Loader;
using System.Text;
using System.Text.Json;
using System.Text.RegularExpressions;
using Microsoft.CodeAnalysis;
using Microsoft.CodeAnalysis.CSharp;
using Microsoft.CodeAnalysis.CSharp.Syntax;
using SolidWorks.Interop.sldworks;

namespace SolidWorks.CSharpWorker;

internal static class Program
{
    private const int ProtocolVersion = 1;
    private const string WorkerVersion = "0.1.0";
    internal const string SnippetPath = "snippet.cs";
    private const int MaxCapturedOutputChars = 256 * 1024;
    private const int MaxResultBytes = 1024 * 1024;
    private const int MaxResponseBytes = 2 * 1024 * 1024;
    private static readonly JsonSerializerOptions JsonOptions = new()
    {
        PropertyNamingPolicy = JsonNamingPolicy.CamelCase,
        WriteIndented = false,
    };

    [STAThread]
    private static int Main()
    {
        WorkerResponse response;
        WorkerRequest? request = null;
        try
        {
            var input = Console.In.ReadToEnd();
            request = JsonSerializer.Deserialize<WorkerRequest>(input, JsonOptions)
                ?? throw new InvalidOperationException("Request body must be a JSON object.");
            InteropAssemblyResolver.Install();
            response = Handle(request);
        }
        catch (Exception exception)
        {
            response = new WorkerResponse
            {
                ProtocolVersion = ProtocolVersion,
                ExecutionId = request?.ExecutionId,
                Exception = ExceptionInfo.From(exception),
            };
        }

        WriteResponse(response);
        return 0;
    }

    private static void WriteResponse(WorkerResponse response)
    {
        try
        {
            using var stream = new SizeLimitedMemoryStream(MaxResponseBytes);
            JsonSerializer.Serialize(stream, response, JsonOptions);
            Console.OpenStandardOutput().Write(stream.GetBuffer(), 0, checked((int)stream.Length));
        }
        catch (Exception exception)
        {
            // Preserve the execution truth so Python still rebuilds/inspects if
            // user code ran, while reducing the protocol response to a safe size.
            var fallback = new WorkerResponse
            {
                ProtocolVersion = ProtocolVersion,
                ExecutionId = response.ExecutionId,
                WorkerVersion = WorkerVersion,
                Compiled = response.Compiled,
                Executed = response.Executed,
                DurationMs = response.DurationMs,
                Exception = new ExceptionInfo
                {
                    Type = "response_too_large",
                    Message = TextLimits.Truncate(exception.Message, 4096),
                },
            };
            var bytes = JsonSerializer.SerializeToUtf8Bytes(fallback, JsonOptions);
            Console.OpenStandardOutput().Write(bytes, 0, bytes.Length);
        }
    }

    private static WorkerResponse Handle(WorkerRequest request)
    {
        var timer = Stopwatch.StartNew();
        var response = new WorkerResponse
        {
            ProtocolVersion = ProtocolVersion,
            ExecutionId = request.ExecutionId,
            WorkerVersion = WorkerVersion,
        };

        try
        {
            ValidateRequest(request);
            var compiled = Compile(request.Code!);
            response.Diagnostics = compiled.Diagnostics;
            response.InteropSymbols = compiled.InteropSymbols;
            response.Compiled = compiled.Success;
            if (!compiled.Success || request.Operation == "compile")
            {
                return response;
            }

            Execute(compiled.AssemblyImage!, request, response);
        }
        catch (Exception exception)
        {
            response.Exception = ExceptionInfo.From(Unwrap(exception));
        }
        finally
        {
            timer.Stop();
            response.DurationMs = timer.ElapsedMilliseconds;
        }

        return response;
    }

    private static void ValidateRequest(WorkerRequest request)
    {
        if (request.ProtocolVersion != ProtocolVersion)
        {
            throw new InvalidOperationException(
                $"Unsupported protocol {request.ProtocolVersion}; expected {ProtocolVersion}.");
        }
        if (request.Operation is not ("compile" or "execute"))
        {
            throw new InvalidOperationException("operation must be 'compile' or 'execute'.");
        }
        if (string.IsNullOrWhiteSpace(request.ExecutionId))
        {
            throw new InvalidOperationException("executionId is required.");
        }
        if (request.Code is null)
        {
            throw new InvalidOperationException("code is required.");
        }
    }

    private static CompilationResult Compile(string code)
    {
        var source = SnippetSource.Wrap(code);
        var syntaxTree = CSharpSyntaxTree.ParseText(
            source,
            new CSharpParseOptions(LanguageVersion.Latest),
            path: "generated-wrapper.cs");
        var references = ReferenceResolver.Resolve();
        var compilation = CSharpCompilation.Create(
            $"SolidWorksSnippet_{Guid.NewGuid():N}",
            new[] { syntaxTree },
            references,
            new CSharpCompilationOptions(
                OutputKind.DynamicallyLinkedLibrary,
                optimizationLevel: OptimizationLevel.Release,
                allowUnsafe: false,
                nullableContextOptions: NullableContextOptions.Enable));

        var model = compilation.GetSemanticModel(syntaxTree, ignoreAccessibility: true);
        var interopSymbols = SymbolCollector.Collect(syntaxTree, model);
        using var stream = new MemoryStream();
        var emit = compilation.Emit(stream);
        var diagnostics = emit.Diagnostics
            .Where(diagnostic => diagnostic.Severity != DiagnosticSeverity.Hidden)
            .OrderBy(diagnostic => diagnostic.Location.SourceSpan.Start)
            .ThenBy(diagnostic => diagnostic.Id, StringComparer.Ordinal)
            .Select(DiagnosticInfo.From)
            .ToList();

        return new CompilationResult
        {
            Success = emit.Success,
            AssemblyImage = emit.Success ? stream.ToArray() : null,
            Diagnostics = diagnostics,
            InteropSymbols = interopSymbols,
        };
    }

    private static void Execute(
        byte[] assemblyImage,
        WorkerRequest request,
        WorkerResponse response)
    {
        ISldWorks? swApp = null;
        IModelDoc2? model = null;
        IModelDocExtension? extension = null;
        ISelectionMgr? selection = null;
        IFeatureManager? featureManager = null;
        ISketchManager? sketchManager = null;
        TextWriter? originalOutput = null;
        TextWriter? originalError = null;
        var capturedOutput = new CappedTextWriter(MaxCapturedOutputChars);
        var capturedError = new CappedTextWriter(MaxCapturedOutputChars);

        try
        {
            swApp = RunningObjectTable.AttachSolidWorks();
            if (request.ExpectedProcessId <= 0 || swApp.GetProcessID() != request.ExpectedProcessId)
            {
                throw new InvalidOperationException(
                    "The running SolidWorks instance does not match the requested session.");
            }
            model = swApp.ActiveDoc as IModelDoc2
                ?? throw new InvalidOperationException("SolidWorks has no active document.");
            ValidateTargetDocument(model, request.ExpectedDocument);
            extension = model.Extension;
            selection = model.SelectionManager as ISelectionMgr
                ?? throw new InvalidOperationException("Could not access the selection manager.");
            featureManager = model.FeatureManager;
            sketchManager = model.SketchManager;

            using var stream = new MemoryStream(assemblyImage, writable: false);
            var assembly = AssemblyLoadContext.Default.LoadFromStream(stream);
            var type = assembly.GetType("SolidWorks.AgentSnippet.SnippetEntryPoint", throwOnError: true)!;
            var method = type.GetMethod("Run", BindingFlags.Public | BindingFlags.Static)
                ?? throw new MissingMethodException(type.FullName, "Run");

            originalOutput = Console.Out;
            originalError = Console.Error;
            Console.SetOut(capturedOutput);
            Console.SetError(capturedError);
            object? value;
            try
            {
                response.Executed = true;
                value = method.Invoke(
                    null,
                    new object[]
                    {
                        swApp, model, extension, selection, featureManager, sketchManager,
                    });
            }
            finally
            {
                Console.SetOut(originalOutput);
                originalOutput = null;
                Console.SetError(originalError);
                originalError = null;
            }

            if (value is not null)
            {
                try
                {
                    using var resultStream = new SizeLimitedMemoryStream(MaxResultBytes);
                    JsonSerializer.Serialize(resultStream, value, value.GetType(), JsonOptions);
                    resultStream.Position = 0;
                    using var document = JsonDocument.Parse(resultStream);
                    response.Result = document.RootElement.Clone();
                }
                catch (Exception exception)
                {
                    response.Exception = new ExceptionInfo
                    {
                        Type = "result_serialization_failed",
                        Message = TextLimits.Truncate(exception.Message, 16 * 1024),
                    };
                }
            }
        }
        finally
        {
            if (originalOutput is not null)
            {
                Console.SetOut(originalOutput);
            }
            if (originalError is not null)
            {
                Console.SetError(originalError);
            }
            var stderr = capturedError.ToString();
            response.CapturedOutput = stderr.Length == 0
                ? capturedOutput.ToString()
                : $"{capturedOutput}\n[stderr]\n{stderr}";
            response.CapturedOutputTruncated =
                capturedOutput.Truncated || capturedError.Truncated;
            ReleaseCom(sketchManager);
            ReleaseCom(featureManager);
            ReleaseCom(selection);
            ReleaseCom(extension);
            ReleaseCom(model);
            ReleaseCom(swApp);
        }
    }

    private static void ValidateTargetDocument(
        IModelDoc2 model,
        ExpectedDocument? expected)
    {
        if (expected is null)
        {
            throw new InvalidOperationException("Expected active-document identity is required.");
        }
        var actualPath = model.GetPathName() ?? string.Empty;
        var actualTitle = model.GetTitle() ?? string.Empty;
        var matches = !string.IsNullOrWhiteSpace(expected.Path)
            ? string.Equals(actualPath, expected.Path, StringComparison.OrdinalIgnoreCase)
            : string.Equals(actualTitle, expected.Title, StringComparison.OrdinalIgnoreCase);
        if (!matches)
        {
            throw new InvalidOperationException(
                "The active SolidWorks document changed before snippet execution.");
        }
        var type = model.GetType();
        if (expected.Type is not null && !string.Equals(
                expected.Type,
                type switch { 1 => "part", 2 => "assembly", 3 => "drawing", _ => "unknown" },
                StringComparison.OrdinalIgnoreCase))
        {
            throw new InvalidOperationException(
                "The active SolidWorks document type does not match the requested target.");
        }
    }

    private static Exception Unwrap(Exception exception)
    {
        while (exception is TargetInvocationException { InnerException: not null })
        {
            exception = exception.InnerException;
        }
        return exception;
    }

    private static void ReleaseCom(object? value)
    {
        if (value is null || !Marshal.IsComObject(value))
        {
            return;
        }
        try
        {
            Marshal.FinalReleaseComObject(value);
        }
        catch
        {
            // Process exit is the final isolation boundary. Cleanup is best-effort.
        }
    }
}

internal static class SnippetSource
{
    private const string Header = """
#nullable enable
using System;
using System.Collections.Generic;
using System.Linq;
using SolidWorks.Interop.sldworks;
using SolidWorks.Interop.swconst;

namespace SolidWorks.AgentSnippet
{
    public static class SnippetEntryPoint
    {
        public static object? Run(
            ISldWorks swApp,
            IModelDoc2 model,
            IModelDocExtension extension,
            ISelectionMgr selection,
            IFeatureManager featureManager,
            ISketchManager sketchManager)
        {
            object? result = null;
#line 1 "snippet.cs"
""";

    private const string Footer = """

#line default
            return result;
        }
    }
}
""";

    internal static string Wrap(string code) => Header + System.Environment.NewLine + code + Footer;
}

internal static class InteropAssemblyResolver
{
    private static readonly HashSet<string> InteropNames = new(StringComparer.OrdinalIgnoreCase)
    {
        "SolidWorks.Interop.sldworks",
        "SolidWorks.Interop.swconst",
    };
    private static string? _directory;

    internal static string Directory => _directory
        ?? throw new InvalidOperationException("SolidWorks interop resolver was not initialized.");

    internal static void Install()
    {
        if (_directory is not null)
        {
            return;
        }
        _directory = LocateInteropDirectory();
        AssemblyLoadContext.Default.Resolving += Resolve;
    }

    private static Assembly? Resolve(AssemblyLoadContext context, AssemblyName name)
    {
        if (name.Name is null || !InteropNames.Contains(name.Name))
        {
            return null;
        }
        var path = Path.Combine(Directory, $"{name.Name}.dll");
        return context.LoadFromAssemblyPath(Path.GetFullPath(path));
    }

    private static string LocateInteropDirectory()
    {
        var configured = System.Environment.GetEnvironmentVariable("SOLIDWORKS_INTEROP_DIR");
        if (!string.IsNullOrWhiteSpace(configured) && HasInterop(configured))
        {
            return Path.GetFullPath(configured);
        }

        var programFiles = System.Environment.GetFolderPath(
            System.Environment.SpecialFolder.ProgramFiles);
        var productRoot = Path.Combine(programFiles, "SOLIDWORKS Corp");
        var canonical = Path.Combine(productRoot, "SOLIDWORKS", "api", "redist");
        if (HasInterop(canonical))
        {
            return canonical;
        }
        if (System.IO.Directory.Exists(productRoot))
        {
            var discovered = System.IO.Directory.EnumerateDirectories(productRoot, "SOLIDWORKS*")
                .Select(directory => Path.Combine(directory, "api", "redist"))
                .Where(HasInterop)
                .OrderByDescending(path => path, StringComparer.OrdinalIgnoreCase)
                .FirstOrDefault();
            if (discovered is not null)
            {
                return discovered;
            }
        }
        throw new DirectoryNotFoundException(
            "SolidWorks interop assemblies were not found. Set SOLIDWORKS_INTEROP_DIR.");
    }

    private static bool HasInterop(string directory) =>
        File.Exists(Path.Combine(directory, "SolidWorks.Interop.sldworks.dll")) &&
        File.Exists(Path.Combine(directory, "SolidWorks.Interop.swconst.dll"));
}

internal static class ReferenceResolver
{
    internal static IReadOnlyList<MetadataReference> Resolve()
    {
        var paths = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        var solidWorksInteropNames = new HashSet<string>(StringComparer.OrdinalIgnoreCase)
        {
            "SolidWorks.Interop.sldworks.dll",
            "SolidWorks.Interop.swconst.dll",
        };
        var trusted = AppContext.GetData("TRUSTED_PLATFORM_ASSEMBLIES") as string;
        if (string.IsNullOrWhiteSpace(trusted))
        {
            throw new InvalidOperationException(".NET trusted platform assemblies are unavailable.");
        }
        foreach (var path in trusted.Split(Path.PathSeparator))
        {
            // Bind SolidWorks interop types from the installed API redist only.
            // A self-contained deployment can otherwise surface a second copy via
            // TPA and hand Roslyn duplicate assembly identities.
            if (!solidWorksInteropNames.Contains(Path.GetFileName(path)))
            {
                paths.Add(path);
            }
        }

        var interopDirectory = InteropAssemblyResolver.Directory;
        paths.Add(Path.Combine(interopDirectory, "SolidWorks.Interop.sldworks.dll"));
        paths.Add(Path.Combine(interopDirectory, "SolidWorks.Interop.swconst.dll"));

        return paths.Select(path => MetadataReference.CreateFromFile(path)).ToList();
    }
}

internal static class SymbolCollector
{
    internal static List<InteropSymbolInfo> Collect(SyntaxTree tree, SemanticModel model)
    {
        var symbols = new Dictionary<string, InteropSymbolInfo>(StringComparer.Ordinal);
        foreach (var node in tree.GetRoot().DescendantNodes())
        {
            if (!IsPrimaryUsage(node))
            {
                continue;
            }
            var span = node.GetLocation().GetMappedLineSpan();
            if (!string.Equals(span.Path, Program.SnippetPath, StringComparison.OrdinalIgnoreCase))
            {
                continue;
            }

            var symbolInfo = model.GetSymbolInfo(node);
            IEnumerable<(ISymbol Symbol, string Resolution, string? Reason)> candidates;
            if (symbolInfo.Symbol is not null)
            {
                candidates = new List<(ISymbol Symbol, string Resolution, string? Reason)>
                {
                    (symbolInfo.Symbol, "resolved", null),
                };
            }
            else
            {
                candidates = symbolInfo.CandidateSymbols.Select(symbol => (
                    Symbol: symbol,
                    Resolution: "candidate",
                    Reason: (string?)symbolInfo.CandidateReason.ToString()));
            }

            foreach (var candidate in candidates)
            {
                var symbol = candidate.Symbol;
                var assemblyName = symbol.ContainingAssembly?.Name;
                if (assemblyName is null || !assemblyName.StartsWith(
                        "SolidWorks.Interop.", StringComparison.OrdinalIgnoreCase))
                {
                    continue;
                }

                var typeName = symbol is INamedTypeSymbol namedType
                    ? namedType.ToDisplayString(SymbolDisplayFormat.MinimallyQualifiedFormat)
                    : symbol.ContainingType?.ToDisplayString(
                        SymbolDisplayFormat.MinimallyQualifiedFormat);
                var signature = symbol.ToDisplayString(
                    SymbolDisplayFormat.CSharpErrorMessageFormat);
                var key = $"{assemblyName}|{signature}|{candidate.Resolution}";
                var line = span.StartLinePosition.Line + 1;
                if (symbols.TryGetValue(key, out var existing))
                {
                    existing.OccurrenceCount += 1;
                    if (!existing.SourceLines.Contains(line))
                    {
                        existing.SourceLines.Add(line);
                    }
                    continue;
                }
                symbols[key] = new InteropSymbolInfo
                {
                    Assembly = assemblyName,
                    Type = typeName,
                    Member = symbol is INamedTypeSymbol ? null : symbol.Name,
                    Kind = symbol.Kind.ToString(),
                    Signature = signature,
                    Resolution = candidate.Resolution,
                    CandidateReason = candidate.Reason,
                    OccurrenceCount = 1,
                    SourceLines = new List<int> { line },
                };
            }
        }
        return symbols.Values
            .OrderBy(value => value.Signature, StringComparer.Ordinal)
            .ThenBy(value => value.Resolution, StringComparer.Ordinal)
            .ToList();
    }

    private static bool IsPrimaryUsage(SyntaxNode node) => node switch
    {
        InvocationExpressionSyntax => true,
        ObjectCreationExpressionSyntax => true,
        MemberAccessExpressionSyntax member =>
            member.Parent is not InvocationExpressionSyntax invocation ||
            !ReferenceEquals(invocation.Expression, member),
        IdentifierNameSyntax identifier =>
            identifier.Parent is not MemberAccessExpressionSyntax &&
            identifier.Parent is not QualifiedNameSyntax &&
            identifier.Parent is not InvocationExpressionSyntax,
        _ => false,
    };
}

internal static class RunningObjectTable
{
    [DllImport("ole32.dll", CharSet = CharSet.Unicode)]
    private static extern int CLSIDFromProgID(string progId, out Guid classId);

    [DllImport("oleaut32.dll", PreserveSig = false)]
    private static extern void GetActiveObject(
        ref Guid classId,
        IntPtr reserved,
        [MarshalAs(UnmanagedType.IUnknown)] out object instance);

    internal static ISldWorks AttachSolidWorks()
    {
        var result = CLSIDFromProgID("SldWorks.Application", out var classId);
        if (result < 0)
        {
            Marshal.ThrowExceptionForHR(result);
        }
        try
        {
            GetActiveObject(ref classId, IntPtr.Zero, out var instance);
            return instance as ISldWorks
                ?? throw new InvalidCastException("Running object is not ISldWorks.");
        }
        catch (COMException exception)
        {
            throw new InvalidOperationException(
                "No running SolidWorks instance was found. Start SolidWorks first.", exception);
        }
    }
}

internal sealed class WorkerRequest
{
    public int ProtocolVersion { get; set; }
    public string? Operation { get; set; }
    public string? ExecutionId { get; set; }
    public string? Code { get; set; }
    public int TimeoutMs { get; set; }
    public int ExpectedProcessId { get; set; }
    public ExpectedDocument? ExpectedDocument { get; set; }
}

internal sealed class ExpectedDocument
{
    public string? Path { get; set; }
    public string? Title { get; set; }
    public string? Type { get; set; }
}

internal sealed class WorkerResponse
{
    public int ProtocolVersion { get; set; }
    public string? ExecutionId { get; set; }
    public bool Compiled { get; set; }
    public bool Executed { get; set; }
    public long DurationMs { get; set; }
    public List<DiagnosticInfo> Diagnostics { get; set; } = new();
    public List<InteropSymbolInfo> InteropSymbols { get; set; } = new();
    public ExceptionInfo? Exception { get; set; }
    public JsonElement? Result { get; set; }
    public string CapturedOutput { get; set; } = string.Empty;
    public bool CapturedOutputTruncated { get; set; }
    public string? WorkerVersion { get; set; }
}

internal sealed class DiagnosticInfo
{
    public string Severity { get; set; } = string.Empty;
    public string Code { get; set; } = string.Empty;
    public string Message { get; set; } = string.Empty;
    public int? Line { get; set; }
    public int? Column { get; set; }
    public string Source { get; set; } = string.Empty;

    internal static DiagnosticInfo From(Diagnostic diagnostic)
    {
        var mapped = diagnostic.Location.GetMappedLineSpan();
        var isSnippet = string.Equals(
            mapped.Path, Program.SnippetPath, StringComparison.OrdinalIgnoreCase);
        return new DiagnosticInfo
        {
            Severity = diagnostic.Severity.ToString().ToLowerInvariant(),
            Code = diagnostic.Id,
            Message = TextLimits.Truncate(diagnostic.GetMessage(), 16 * 1024),
            Line = diagnostic.Location.IsInSource ? mapped.StartLinePosition.Line + 1 : null,
            Column = diagnostic.Location.IsInSource ? mapped.StartLinePosition.Character + 1 : null,
            Source = isSnippet ? "snippet" : "wrapper",
        };
    }
}

internal sealed class InteropSymbolInfo
{
    public string Assembly { get; set; } = string.Empty;
    public string? Type { get; set; }
    public string? Member { get; set; }
    public string Kind { get; set; } = string.Empty;
    public string Signature { get; set; } = string.Empty;
    public string Resolution { get; set; } = "resolved";
    public string? CandidateReason { get; set; }
    public int OccurrenceCount { get; set; }
    public List<int> SourceLines { get; set; } = new();
}

internal sealed class ExceptionInfo
{
    public string Type { get; set; } = string.Empty;
    public string Message { get; set; } = string.Empty;
    public string? Stack { get; set; }

    internal static ExceptionInfo From(Exception exception) => new()
    {
        Type = TextLimits.Truncate(
            exception.GetType().FullName ?? exception.GetType().Name, 1024),
        Message = TextLimits.Truncate(exception.Message, 16 * 1024),
        Stack = SanitizeStack(exception.StackTrace),
    };

    private static string? SanitizeStack(string? stack) => stack is null
        ? null
        : TextLimits.Truncate(
            Regex.Replace(stack, @" in [^\r\n]+:line (\d+)", " in <worker>:line $1"),
            32 * 1024);
}

internal static class TextLimits
{
    internal static string Truncate(string? value, int maximumCharacters)
    {
        value ??= string.Empty;
        return value.Length <= maximumCharacters
            ? value
            : value[..maximumCharacters] + "...[truncated]";
    }
}

internal sealed class CompilationResult
{
    public bool Success { get; set; }
    public byte[]? AssemblyImage { get; set; }
    public List<DiagnosticInfo> Diagnostics { get; set; } = new();
    public List<InteropSymbolInfo> InteropSymbols { get; set; } = new();
}

internal sealed class CappedTextWriter(int maximumCharacters) : TextWriter
{
    private readonly StringBuilder _builder = new(Math.Min(maximumCharacters, 4096));
    public override Encoding Encoding => Encoding.UTF8;
    public bool Truncated { get; private set; }

    public override void Write(char value)
    {
        if (_builder.Length < maximumCharacters)
        {
            _builder.Append(value);
        }
        else
        {
            Truncated = true;
        }
    }

    public override void Write(string? value)
    {
        if (string.IsNullOrEmpty(value))
        {
            return;
        }
        var remaining = maximumCharacters - _builder.Length;
        if (remaining <= 0)
        {
            Truncated = true;
            return;
        }
        if (value.Length > remaining)
        {
            _builder.Append(value, 0, remaining);
            Truncated = true;
        }
        else
        {
            _builder.Append(value);
        }
    }

    public override string ToString() => _builder.ToString();
}

internal sealed class SizeLimitedMemoryStream(long maximumBytes) : MemoryStream
{
    private void Check(long additional)
    {
        if (Length + additional > maximumBytes)
        {
            throw new InvalidOperationException(
                $"Snippet result exceeds the {maximumBytes}-byte JSON limit.");
        }
    }

    public override void Write(byte[] buffer, int offset, int count)
    {
        Check(count);
        base.Write(buffer, offset, count);
    }

    public override void Write(ReadOnlySpan<byte> buffer)
    {
        Check(buffer.Length);
        base.Write(buffer);
    }

    public override void WriteByte(byte value)
    {
        Check(1);
        base.WriteByte(value);
    }
}

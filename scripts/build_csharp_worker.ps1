param(
    [ValidateSet('Debug', 'Release')]
    [string]$Configuration = 'Release',
    [ValidateSet('win-x64')]
    [string]$Runtime = 'win-x64',
    [bool]$SelfContained = $true
)

$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot -Parent
$workerRoot = Join-Path $root 'worker'
$project = Join-Path $workerRoot 'SolidWorks.CSharpWorker\SolidWorks.CSharpWorker.csproj'
$publish = Join-Path $workerRoot 'publish'
$stage = Join-Path $workerRoot ('.publish-stage-' + [Guid]::NewGuid().ToString('N'))
$packageDir = Join-Path $root 'dist'
$package = Join-Path $packageDir 'solidworks-csharp-worker-win-x64.zip'

function Test-InteropDirectory([string]$Path) {
    return $Path -and
        (Test-Path -LiteralPath (Join-Path $Path 'SolidWorks.Interop.sldworks.dll')) -and
        (Test-Path -LiteralPath (Join-Path $Path 'SolidWorks.Interop.swconst.dll'))
}

if ($env:SOLIDWORKS_INTEROP_DIR) {
    $interop = $env:SOLIDWORKS_INTEROP_DIR
    if (-not (Test-InteropDirectory $interop)) {
        throw "SOLIDWORKS_INTEROP_DIR does not contain both required interop assemblies: $interop"
    }
} else {
    $productRoot = Join-Path $env:ProgramFiles 'SOLIDWORKS Corp'
    $interop = Join-Path $productRoot 'SOLIDWORKS\api\redist'
    if (-not (Test-InteropDirectory $interop) -and (Test-Path -LiteralPath $productRoot)) {
        $interop = Get-ChildItem -LiteralPath $productRoot -Directory |
            Where-Object { $_.Name -like 'SOLIDWORKS*' } |
            ForEach-Object { Join-Path $_.FullName 'api\redist' } |
            Where-Object { Test-InteropDirectory $_ } |
            Sort-Object -Descending |
            Select-Object -First 1
    }
    if (-not (Test-InteropDirectory $interop)) {
        throw "SolidWorks interop assemblies were not found under: $productRoot"
    }
}

if (-not $SelfContained) {
    throw 'The release worker asset must be self-contained.'
}
if (-not (Get-Command dotnet -ErrorAction SilentlyContinue)) {
    throw 'The .NET 8 SDK is required to build the C# worker.'
}
$sdks = & dotnet --list-sdks
if (-not ($sdks | Where-Object { $_ -match '^8\.' })) {
    throw 'The .NET 8 SDK is required; a runtime alone is not sufficient.'
}

$workerRootFull = [IO.Path]::GetFullPath($workerRoot).TrimEnd('\') + '\'
foreach ($candidate in @($publish, $stage)) {
    $candidateFull = [IO.Path]::GetFullPath($candidate)
    if (-not $candidateFull.StartsWith($workerRootFull, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to manage a publish path outside the worker directory: $candidateFull"
    }
}

try {
    & dotnet publish $project `
        --configuration $Configuration `
        --runtime $Runtime `
        --self-contained $SelfContained `
        --output $stage `
        -p:SolidWorksInteropDir="$interop"
    if ($LASTEXITCODE -ne 0) { throw 'C# worker publish failed.' }

    $stagedWorker = Join-Path $stage 'SolidWorks.CSharpWorker.exe'
    if (-not (Test-Path -LiteralPath $stagedWorker)) {
        throw "Publish completed without expected worker: $stagedWorker"
    }
    if (Test-Path -LiteralPath $publish) {
        Remove-Item -LiteralPath $publish -Recurse -Force
    }
    Move-Item -LiteralPath $stage -Destination $publish

    $worker = Join-Path $publish 'SolidWorks.CSharpWorker.exe'
    Write-Host "C# worker published: $worker"

    New-Item -ItemType Directory -Force $packageDir | Out-Null
    Compress-Archive -Path (Join-Path $publish '*') -DestinationPath $package -Force
    Write-Host "Release asset created: $package"
} finally {
    if (Test-Path -LiteralPath $stage) {
        Remove-Item -LiteralPath $stage -Recurse -Force
    }
}

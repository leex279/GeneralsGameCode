param(
    [Parameter(Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [string]$InventoryPath,

    [Parameter(Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [string]$Destination,

    [switch]$Apply
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

# TheSuperHackers @feature Leex 19/08/2026 Archive only hash-pinned untracked replay artifacts after a complete preflight. (#TBD)
function Stop-Archive([string]$Message) {
    throw $Message
}

function Test-HasProperty([object]$Object, [string]$Name) {
    return $null -ne $Object -and $null -ne $Object.PSObject.Properties[$Name]
}

try {
    if (-not (Test-Path -LiteralPath $InventoryPath -PathType Leaf)) {
        Stop-Archive "Invalid inventory schema: inventory file is missing: $InventoryPath"
    }

    $resolvedInventoryPath = (Resolve-Path -LiteralPath $InventoryPath).Path
    try {
        $inventory = Get-Content -LiteralPath $resolvedInventoryPath -Raw -Encoding UTF8 | ConvertFrom-Json
    } catch {
        Stop-Archive "Invalid inventory schema: inventory is not valid JSON. $($_.Exception.Message)"
    }

    if (-not (Test-HasProperty $inventory "schemaVersion") -or
        $inventory.schemaVersion -ne 1 -or
        -not (Test-HasProperty $inventory "repositoryRoot") -or
        $inventory.repositoryRoot -isnot [string] -or
        [string]::IsNullOrWhiteSpace($inventory.repositoryRoot) -or
        -not (Test-HasProperty $inventory "files") -or
        $inventory.files -isnot [System.Array]) {
        Stop-Archive "Invalid inventory schema: expected schemaVersion 1, repositoryRoot, and a files array."
    }

    $repositoryRoot = [IO.Path]::GetFullPath([string]$inventory.repositoryRoot).TrimEnd('\', '/')
    if (-not (Test-Path -LiteralPath $repositoryRoot -PathType Container)) {
        Stop-Archive "Invalid inventory schema: repositoryRoot does not exist."
    }

    $gitRootOutput = @(& git -C $repositoryRoot rev-parse --show-toplevel 2>$null)
    if ($LASTEXITCODE -ne 0 -or $gitRootOutput.Count -ne 1) {
        Stop-Archive "Invalid inventory schema: repositoryRoot is not a Git work tree root."
    }
    $gitRoot = [IO.Path]::GetFullPath([string]$gitRootOutput[0]).TrimEnd('\', '/')
    if (-not [string]::Equals($repositoryRoot, $gitRoot, [StringComparison]::OrdinalIgnoreCase)) {
        Stop-Archive "Invalid inventory schema: repositoryRoot does not match the Git work tree root."
    }

    $destinationRoot = [IO.Path]::GetFullPath($Destination).TrimEnd('\', '/')
    if ([string]::IsNullOrWhiteSpace($destinationRoot)) {
        Stop-Archive "Invalid destination path."
    }
    $manifestPath = Join-Path $destinationRoot "archive-manifest.json"
    if (Test-Path -LiteralPath $manifestPath) {
        Stop-Archive "Destination manifest already exists: $manifestPath"
    }

    $sourcePaths = New-Object 'System.Collections.Generic.HashSet[string]' ([StringComparer]::OrdinalIgnoreCase)
    $destinationPaths = New-Object 'System.Collections.Generic.HashSet[string]' ([StringComparer]::OrdinalIgnoreCase)
    $planned = New-Object 'System.Collections.Generic.List[object]'
    $repositoryPrefix = $repositoryRoot + [IO.Path]::DirectorySeparatorChar
    $destinationPrefix = $destinationRoot + [IO.Path]::DirectorySeparatorChar

    foreach ($entry in $inventory.files) {
        $requiredProperties = @("path", "bytes", "sha256", "tracked", "proposedArchiveCategory")
        foreach ($property in $requiredProperties) {
            if (-not (Test-HasProperty $entry $property)) {
                Stop-Archive "Invalid inventory schema: file entry is missing '$property'."
            }
        }

        if ($entry.path -isnot [string] -or [string]::IsNullOrWhiteSpace($entry.path)) {
            Stop-Archive "Invalid inventory schema: file path must be a non-empty string."
        }
        $relativePath = ([string]$entry.path).Replace('\', '/')
        if ([IO.Path]::IsPathRooted($relativePath) -or
            $relativePath -match '^[A-Za-z]:' -or
            $relativePath -match '(^|/)\.\.(/|$)' -or
            $relativePath -match '(^|/)\.(/|$)') {
            Stop-Archive "Inventory path must be a normalized relative path without traversal: $relativePath"
        }

        if ($entry.proposedArchiveCategory -isnot [string] -or
            [string]::IsNullOrWhiteSpace($entry.proposedArchiveCategory) -or
            [string]$entry.proposedArchiveCategory -notmatch '^[A-Za-z0-9][A-Za-z0-9._-]*$') {
            Stop-Archive "Invalid archive category for '$relativePath'."
        }
        $category = [string]$entry.proposedArchiveCategory

        if ($entry.tracked -isnot [bool]) {
            Stop-Archive "Invalid inventory schema: tracked must be Boolean for '$relativePath'."
        }
        if ([bool]$entry.tracked) {
            Stop-Archive "Inventory entry is recorded as tracked and cannot be archived: $relativePath"
        }

        if ($entry.bytes -isnot [ValueType]) {
            Stop-Archive "Invalid inventory schema: bytes must be a non-negative integer for '$relativePath'."
        }
        try {
            $expectedBytes = [long]$entry.bytes
            if ($expectedBytes -lt 0 -or [decimal]$entry.bytes -ne [decimal]$expectedBytes) {
                Stop-Archive "Invalid inventory schema: bytes must be a non-negative integer for '$relativePath'."
            }
        } catch {
            Stop-Archive "Invalid inventory schema: bytes must be a non-negative integer for '$relativePath'."
        }

        if ($entry.sha256 -isnot [string] -or [string]$entry.sha256 -notmatch '^[0-9A-Fa-f]{64}$') {
            Stop-Archive "Invalid inventory schema: SHA-256 must contain 64 hexadecimal characters for '$relativePath'."
        }
        $expectedHash = ([string]$entry.sha256).ToUpperInvariant()

        if (-not $sourcePaths.Add($relativePath)) {
            Stop-Archive "Duplicate inventory source path: $relativePath"
        }

        $nativeRelativePath = $relativePath.Replace('/', [IO.Path]::DirectorySeparatorChar)
        $sourcePath = [IO.Path]::GetFullPath((Join-Path $repositoryRoot $nativeRelativePath))
        if (-not $sourcePath.StartsWith($repositoryPrefix, [StringComparison]::OrdinalIgnoreCase)) {
            Stop-Archive "Inventory path must resolve under the repository root: $relativePath"
        }
        if (-not (Test-Path -LiteralPath $sourcePath -PathType Leaf)) {
            Stop-Archive "Inventory source is missing: $relativePath"
        }

        $cursorPath = $sourcePath
        while ($cursorPath.StartsWith($repositoryPrefix, [StringComparison]::OrdinalIgnoreCase)) {
            $cursor = Get-Item -LiteralPath $cursorPath -Force
            if (($cursor.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
                Stop-Archive "Inventory source may not traverse a reparse point: $relativePath"
            }
            $cursorPath = Split-Path -Parent $cursorPath
        }

        $trackedPath = @(& git -C $repositoryRoot ls-files -- $relativePath)
        if ($LASTEXITCODE -ne 0) {
            Stop-Archive "Unable to query Git tracked state for '$relativePath'."
        }
        if ($trackedPath.Count -gt 0) {
            Stop-Archive "Inventory source is currently tracked by Git: $relativePath"
        }

        $actualBytes = (Get-Item -LiteralPath $sourcePath).Length
        if ($actualBytes -ne $expectedBytes) {
            Stop-Archive "Inventory byte length drift for '$relativePath': expected $expectedBytes, found $actualBytes."
        }
        $actualHash = (Get-FileHash -LiteralPath $sourcePath -Algorithm SHA256).Hash.ToUpperInvariant()
        if ($actualHash -ne $expectedHash) {
            Stop-Archive "Inventory SHA-256 drift for '$relativePath': expected $expectedHash, found $actualHash."
        }

        $archivedRelativePath = "$category/$relativePath"
        $destinationPath = [IO.Path]::GetFullPath((Join-Path $destinationRoot $archivedRelativePath.Replace('/', [IO.Path]::DirectorySeparatorChar)))
        if (-not $destinationPath.StartsWith($destinationPrefix, [StringComparison]::OrdinalIgnoreCase)) {
            Stop-Archive "Archive destination must resolve under the destination root: $archivedRelativePath"
        }
        if (-not $destinationPaths.Add($destinationPath)) {
            Stop-Archive "Duplicate archive destination: $archivedRelativePath"
        }
        if (Test-Path -LiteralPath $destinationPath) {
            Stop-Archive "Destination collision: $destinationPath"
        }

        $planned.Add([PSCustomObject]@{
            OriginalPath = $relativePath
            ArchivedPath = $archivedRelativePath
            SourcePath = $sourcePath
            DestinationPath = $destinationPath
            Bytes = $expectedBytes
            Sha256 = $expectedHash
            Category = $category
        })
    }

    $planned = @($planned | Sort-Object -Property OriginalPath)
    Write-Output "Archive preflight passed."
    Write-Output "Mode: $(if ($Apply) { 'apply' } else { 'dry-run' })"
    Write-Output "Destination: $destinationRoot"
    Write-Output "Planned files: $($planned.Count)"
    foreach ($group in @($planned | Group-Object -Property Category | Sort-Object -Property Name)) {
        Write-Output "Category $($group.Name): $($group.Count)"
    }
    foreach ($item in $planned) {
        Write-Output "PLAN $($item.SourcePath) -> $($item.DestinationPath)"
    }

    if (-not $Apply) {
        exit 0
    }

    foreach ($item in $planned) {
        $parent = Split-Path -Parent $item.DestinationPath
        if (-not (Test-Path -LiteralPath $parent -PathType Container)) {
            $null = New-Item -ItemType Directory -Path $parent -Force
        }
        Move-Item -LiteralPath $item.SourcePath -Destination $item.DestinationPath -ErrorAction Stop
    }

    if (-not (Test-Path -LiteralPath $destinationRoot -PathType Container)) {
        $null = New-Item -ItemType Directory -Path $destinationRoot -Force
    }
    $manifest = [ordered]@{
        schemaVersion = 1
        files = @($planned | ForEach-Object {
            [ordered]@{
                originalPath = $_.OriginalPath
                archivedPath = $_.ArchivedPath
                bytes = $_.Bytes
                sha256 = $_.Sha256
            }
        })
    }
    $manifestJson = $manifest | ConvertTo-Json -Depth 5
    [IO.File]::WriteAllText($manifestPath, $manifestJson + [Environment]::NewLine, (New-Object Text.UTF8Encoding($false)))
    Write-Output "Archived files: $($planned.Count)"
    Write-Output "Manifest: $manifestPath"
} catch {
    [Console]::Error.WriteLine("ERROR: " + $_.Exception.Message)
    exit 1
}

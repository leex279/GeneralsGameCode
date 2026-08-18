[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$OutputPath
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$repositoryRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..\..'))
$resolvedOutputPath = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($OutputPath)
$outputDirectory = Split-Path -Parent $resolvedOutputPath

if (-not [System.IO.Directory]::Exists($outputDirectory)) {
    [System.IO.Directory]::CreateDirectory($outputDirectory) | Out-Null
}

function Get-ProposedArchiveCategory {
    param(
        [System.IO.FileInfo]$File
    )

    switch ($File.Extension.ToLowerInvariant()) {
        '.mp4' { return 'video' }
        '.webm' { return 'video' }
        '.wav' { return 'audio' }
        '.mp3' { return 'audio' }
        '.png' { return 'image' }
        '.jpg' { return 'image' }
        '.log' { return 'diagnostic-log' }
        '.html' { return 'generated-html' }
        '.txt' { return 'replay-diagnostic' }
        '.py' { return 'replay-experiment' }
        default { throw "Unexpected inventory extension: $($File.Extension)" }
    }
}

function Test-IsInventoryCandidate {
    param(
        [System.IO.FileInfo]$File
    )

    $extension = $File.Extension.ToLowerInvariant()
    if ($extension -in @('.mp4', '.webm', '.wav', '.mp3', '.png', '.jpg', '.log', '.html')) {
        return $true
    }

    if ($extension -eq '.txt') {
        return $File.Name -match '(?i)(replay|autocast|telemetry|diag|camera)'
    }

    return $extension -eq '.py'
}

$files = @(
    Get-ChildItem -LiteralPath $repositoryRoot -File -Force |
        Where-Object { Test-IsInventoryCandidate $_ } |
        ForEach-Object {
            $relativePath = $_.Name
            $trackedPath = @(& git -C $repositoryRoot ls-files -- $relativePath)
            $isTracked = $trackedPath.Count -gt 0
            $hash = Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256

            [PSCustomObject][ordered]@{
                path                    = $relativePath
                bytes                   = $_.Length
                sha256                  = $hash.Hash
                tracked                 = $isTracked
                proposedArchiveCategory = Get-ProposedArchiveCategory $_
            }
        } |
        Sort-Object -Property path
)

$inventory = [PSCustomObject][ordered]@{
    schemaVersion   = 1
    generatedAtUtc  = [DateTime]::UtcNow.ToString('o')
    repositoryRoot  = $repositoryRoot
    files           = $files
}

$json = $inventory | ConvertTo-Json -Depth 4
$utf8WithoutBom = New-Object System.Text.UTF8Encoding($false)
[System.IO.File]::WriteAllText($resolvedOutputPath, $json, $utf8WithoutBom)

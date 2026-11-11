[CmdletBinding()]
param(
    [ValidatePattern('^\d+\.\d+\.\d+(-[0-9A-Za-z.-]+)?$')]
    [string]$Version = '0.7.0-preview.10',
    [string]$RepoRoot = (Join-Path $PSScriptRoot '..'),
    [string]$OutputDirectory,
    [switch]$WriteChecksums
)
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.IO.Compression.FileSystem
$repo = [IO.Path]::GetFullPath($RepoRoot).TrimEnd('\', '/')
if (-not $OutputDirectory) { $OutputDirectory = Join-Path $repo 'dist' }
$output = [IO.Path]::GetFullPath($OutputDirectory)
New-Item -ItemType Directory -Force -Path $output | Out-Null
$rootFiles = @('.gitignore', '.gitattributes', 'README.md', 'LICENSE', 'THIRD_PARTY_NOTICES.md',
    'requirements-runtime.txt', 'dependencies.lock.json', 'supported-clients.json')
$sourceRoots = @('.github', 'src', 'tests', 'scripts', 'installer', 'docs')
# Limit generated-folder traversal and file types as well as top-level paths.
# Reparse points are skipped so no linked directory can escape the repository.
$extensions = @('.py', '.js', '.mjs', '.cjs', '.cs', '.c', '.h', '.s', '.def', '.ps1',
    '.iss', '.md', '.txt', '.yml', '.yaml', '.json', '.svg', '.png', '.jpg', '.jpeg', '.ico')
$files = New-Object 'System.Collections.Generic.List[object]'
foreach ($name in $rootFiles) {
    $path = Join-Path $repo $name
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { throw ('Missing source root file: ' + $name) }
    if ((Get-Item -LiteralPath $path -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) { throw ('Linked source root file: ' + $name) }
    $files.Add([PSCustomObject]@{ Path=$path; Relative=$name })
}
foreach ($sourceRoot in $sourceRoots) {
    $base = Join-Path $repo $sourceRoot
    if (-not (Test-Path -LiteralPath $base -PathType Container)) { throw ('Missing source directory: ' + $sourceRoot) }
    if ((Get-Item -LiteralPath $base -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) { throw ('Linked source directory: ' + $sourceRoot) }
    $pending = New-Object 'System.Collections.Generic.Stack[string]'
    $pending.Push($base)
    while ($pending.Count -gt 0) {
        $directory = $pending.Pop()
        foreach ($item in Get-ChildItem -LiteralPath $directory -Force) {
            if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) { continue }
            $relative = $item.FullName.Substring($repo.Length + 1).Replace('\', '/')
            if ($relative -match '(?i)(^|/)(?:\.git|\.build|dist|__pycache__|\.cache|cache|work|private|logs|runtime|artifacts|build)(/|$)') { continue }
            if ($relative -match '(?i)^tests/native_autoload/fixture-[^/]*(/|$)') { continue }
            if ($item.PSIsContainer) { $pending.Push($item.FullName); continue }
            if ($extensions -notcontains $item.Extension.ToLowerInvariant()) { continue }
            if ($item.Name -match '(?i)^(?:\.env(?:\..*)?|(?:state|settings|config)(?:\.[^/]+)?)$') { continue }
            $files.Add([PSCustomObject]@{ Path=$item.FullName; Relative=$relative })
        }
    }
}
$prefix = 'NcmWasapiExtension-' + $Version
$archivePath = Join-Path $output ($prefix + '-source.zip')
$temporary = Join-Path $output ('.source-' + [Guid]::NewGuid().ToString('N') + '.zip')
$stream = [IO.File]::Open($temporary, [IO.FileMode]::CreateNew, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None)
$archive = New-Object IO.Compression.ZipArchive($stream, [IO.Compression.ZipArchiveMode]::Create, $false)
try {
    foreach ($file in ($files | Sort-Object Relative)) {
        $entry = $archive.CreateEntry('NcmWasapiProject/' + $file.Relative, [IO.Compression.CompressionLevel]::Optimal)
        # Source timestamps do not identify a release. Keep archive metadata stable.
        $entry.LastWriteTime = [DateTimeOffset]'2000-01-01T00:00:00+00:00'
        $input = [IO.File]::OpenRead($file.Path)
        $destination = $entry.Open()
        try { $input.CopyTo($destination) } finally { $destination.Dispose(); $input.Dispose() }
    }
} finally { $archive.Dispose() }
if (Test-Path -LiteralPath $archivePath) { Remove-Item -LiteralPath $archivePath }
Move-Item -LiteralPath $temporary -Destination $archivePath
if ($WriteChecksums) {
    $releaseFiles = @(
        (Join-Path $output ($prefix + '-windows-x64-setup.exe')),
        (Join-Path $output ($prefix + '-windows-x64-portable.zip')),
        $archivePath
    )
    $lines = foreach ($file in $releaseFiles) {
        if (-not (Test-Path -LiteralPath $file -PathType Leaf)) { throw ('Missing release asset: ' + [IO.Path]::GetFileName($file)) }
        (Get-FileHash -LiteralPath $file -Algorithm SHA256).Hash.ToLowerInvariant() + '  ' + [IO.Path]::GetFileName($file)
    }
    $utf8 = New-Object Text.UTF8Encoding($false)
    [IO.File]::WriteAllText((Join-Path $output 'SHA256SUMS.txt'), (($lines -join "`n") + "`n"), $utf8)
}
Write-Host ('Source archive: ' + $files.Count + ' files')
Write-Output $archivePath

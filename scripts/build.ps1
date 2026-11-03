[CmdletBinding()]
param(
    [ValidatePattern('^\d+\.\d+\.\d+(-[0-9A-Za-z.-]+)?$')]
    [string]$Version = '0.7.0-preview.5',
    [string]$InnoCompiler,
    [string]$ZigCompiler,
    [switch]$PayloadOnly
)
$ErrorActionPreference = 'Stop'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
Add-Type -AssemblyName System.IO.Compression.FileSystem
$repo = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$buildRoot = Join-Path $repo '.build'
$cache = Join-Path $buildRoot 'downloads'
$dist = Join-Path $repo 'dist'
New-Item -ItemType Directory -Force -Path $cache, $dist | Out-Null
$dependencyLock = Get-Content -LiteralPath (Join-Path $repo 'dependencies.lock.json') -Raw | ConvertFrom-Json

function Get-PinnedDownload($item) {
    $local = $item.PSObject.Properties.Name -contains 'path'
    if ($local) {
        if ([string]::IsNullOrWhiteSpace($item.path) -or [IO.Path]::IsPathRooted($item.path)) {
            throw ('Pinned local path must be repository-relative: ' + $item.filename)
        }
        $target = [IO.Path]::GetFullPath((Join-Path $repo $item.path))
        $prefix = $repo.TrimEnd('\', '/') + [IO.Path]::DirectorySeparatorChar
        if (-not $target.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase) -or
            -not (Test-Path -LiteralPath $target -PathType Leaf)) {
            throw ('Pinned local file is missing or outside the repository: ' + $item.filename)
        }
    } else {
        $target = Join-Path $cache $item.filename
        if (-not (Test-Path -LiteralPath $target)) {
            Write-Host ('Downloading ' + $item.filename)
            Invoke-WebRequest -Uri $item.url -OutFile $target -UseBasicParsing
        }
    }
    $actual = (Get-FileHash -LiteralPath $target -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($actual -ne $item.sha256) {
        if ($local) { throw ('SHA256 mismatch in repository file: ' + $item.path) }
        throw ('SHA256 mismatch: ' + $item.filename + '. Remove the invalid cached file before retrying.')
    }
    return $target
}
function Copy-SourceFile([string]$relative, [string]$targetDirectory) {
    Copy-Item -LiteralPath (Join-Path $repo $relative) -Destination $targetDirectory
}

# A new staging directory prevents old binaries or local settings entering a release.
$stage = Join-Path $buildRoot ('stage-' + [Guid]::NewGuid().ToString('N'))
$payload = Join-Path $stage 'NcmWasapiExtension'
$runtime = Join-Path $payload 'runtime'
$sitePackages = Join-Path $runtime 'Lib\site-packages'
$licenses = Join-Path $payload 'licenses'
New-Item -ItemType Directory -Force -Path $payload, $runtime, $sitePackages, $licenses | Out-Null
[IO.Compression.ZipFile]::ExtractToDirectory((Get-PinnedDownload $dependencyLock.python), $runtime)
foreach ($wheel in $dependencyLock.wheels) {
    [IO.Compression.ZipFile]::ExtractToDirectory((Get-PinnedDownload $wheel), $sitePackages)
}
$utf8 = New-Object Text.UTF8Encoding($false)
[IO.File]::WriteAllText((Join-Path $runtime 'python312._pth'), "python312.zip`n.`nLib\site-packages`nimport site`n", $utf8)
# Embedded Python requires no system Python, pip, or administrator access.
$pythonFiles = 'eq_settings.py','controller.py','installation.py','compatibility.py','cdp_bridge.py','device_capabilities.py','job_process.py','attach_process.py','autoload_install.py'
foreach ($name in $pythonFiles) { Copy-SourceFile ('src\python\' + $name) $payload }
$audioFiles = 'audio-bootstrap.js','convert-audio.js','parametric-eq.js','native-source.js','alignment-retry.js','generic-adapter-hooks.js'
$audioParts = foreach ($name in $audioFiles) {
    [IO.File]::ReadAllText((Join-Path $repo ('src\audio\' + $name)))
}
[IO.File]::WriteAllText((Join-Path $payload 'adapter.js'), ($audioParts -join "`n"), $utf8)
$nativeSource = [IO.File]::ReadAllText((Join-Path $repo 'src\audio\native-source.js'))
$guardEnd = $nativeSource.IndexOf('function createNativeSourceTracker')
if ($guardEnd -lt 0) { throw 'Native compatibility guard is missing' }
$guard = $nativeSource.Substring(0,$guardEnd) + "`nrpc.exports={verify(){const c=resolveNativeSourceCompatibility(globalThis.__ncmCompatibility);return {ok:true,workerRva:c.workerRva,versions:c.versions};}};`n"
[IO.File]::WriteAllText((Join-Path $payload 'compatibility-guard.js'), $guard, $utf8)
foreach ($name in @('output-option-inject.js','install_output_options.js','equalizer-panel.js','equalizer-transport.js')) { Copy-SourceFile ('src\frontend\' + $name) $payload }
foreach ($name in @('supported-clients.json','dependencies.lock.json','LICENSE','README.md','THIRD_PARTY_NOTICES.md')) { Copy-SourceFile $name $payload }
Copy-Item -LiteralPath (Join-Path $repo 'docs') -Destination (Join-Path $payload 'docs') -Recurse
Copy-Item -LiteralPath (Join-Path $runtime 'LICENSE.txt') -Destination (Join-Path $licenses 'Python-LICENSE.txt')
$fridaLicense = Get-ChildItem -LiteralPath $sitePackages -Recurse -File | Where-Object { $_.Name -eq 'COPYING' -and $_.FullName -match 'frida.*dist-info' } | Select-Object -First 1
if (-not $fridaLicense) { throw 'Frida wheel COPYING missing' }
Copy-Item -LiteralPath $fridaLicense.FullName -Destination (Join-Path $licenses 'Frida-COPYING')
Copy-Item -LiteralPath (Get-PinnedDownload $dependencyLock.fridaBaseLicense) -Destination (Join-Path $licenses 'Frida-COPYING.LIB')
$websocketLicense = Get-ChildItem -LiteralPath $sitePackages -Recurse -File | Where-Object { $_.Name -eq 'LICENSE' -and $_.FullName -match 'websocket_client.*dist-info' } | Select-Object -First 1
if (-not $websocketLicense) { throw 'websocket-client license missing' }
Copy-Item -LiteralPath $websocketLicense.FullName -Destination (Join-Path $licenses 'websocket-client-LICENSE')

$csc = Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'
if (-not (Test-Path -LiteralPath $csc)) { throw '.NET Framework 4.x C# compiler is required on the Windows x64 build host.' }
$parts = $Version.Split('-')[0].Split('.')
$winVersion = ($parts -join '.') + '.0'
if ($Version -match '-preview\.(\d+)$') { $winVersion = ($parts -join '.') + '.' + $Matches[1] }
$assemblyInfo = Join-Path $stage 'AssemblyInfo.cs'
$attributes = @"
using System.Reflection;
[assembly: AssemblyTitle("NetEase WASAPI Extension")]
[assembly: AssemblyProduct("NcmWasapiExtension")]
[assembly: AssemblyVersion("$winVersion")]
[assembly: AssemblyFileVersion("$winVersion")]
[assembly: AssemblyInformationalVersion("$Version")]
"@
[IO.File]::WriteAllText($assemblyInfo, $attributes, $utf8)
$launcher = Join-Path $payload 'NcmWasapiExtension.exe'
& $csc /nologo /target:winexe /platform:x64 /optimize+ "/out:$launcher" /r:System.Windows.Forms.dll /r:System.Drawing.dll /r:System.Web.Extensions.dll (Join-Path $repo 'src\launcher\Launcher.cs') $assemblyInfo
if ($LASTEXITCODE -ne 0) { throw 'Launcher compilation failed' }
$buildInfo = @{ version=$Version; architecture='x64'; runtime='CPython 3.12.10'; channel='preview'; signed=$false }
[IO.File]::WriteAllText((Join-Path $payload 'build-info.json'), (($buildInfo | ConvertTo-Json) + "`n"), $utf8)
[IO.File]::WriteAllText((Join-Path $payload '.ncm-wasapi-install'), 'NcmWasapiExtension:97D3C8CF-7EB4-4710-9BBA-A8266F44CDAC:v1', $utf8)

if (-not $ZigCompiler) {
    $zigRoot = Join-Path $buildRoot 'Zig'
    $ZigCompiler = Join-Path $zigRoot 'ziglang\zig.exe'
    if (-not (Test-Path -LiteralPath $ZigCompiler)) {
        $zigArchive = Get-PinnedDownload $dependencyLock.zig
        New-Item -ItemType Directory -Force -Path $zigRoot | Out-Null
        [IO.Compression.ZipFile]::ExtractToDirectory($zigArchive,$zigRoot)
    }
}
$native = Join-Path $payload 'native'
New-Item -ItemType Directory -Force -Path $native | Out-Null
& $ZigCompiler cc -target x86_64-windows-gnu -O2 -g0 -s -shared (Join-Path $repo 'src\autoload\loader.c') (Join-Path $repo 'src\autoload\forwarders.S') (Join-Path $repo 'src\autoload\msimg32.def') -lws2_32 -o (Join-Path $native 'msimg32.dll')
if ($LASTEXITCODE -ne 0) { throw 'Native autoload module compilation failed' }
$mingwNotice = Join-Path (Split-Path -Parent $ZigCompiler) 'lib\libc\mingw\COPYING'
if (Test-Path -LiteralPath $mingwNotice) { Copy-Item -LiteralPath $mingwNotice -Destination (Join-Path $licenses 'mingw-w64-COPYING') }

# Verify the clean runtime and manifest without starting or stopping a player.
$python = Join-Path $runtime 'python.exe'
& $python -B -c "import frida, websocket; print('Runtime imports OK:', frida.__version__, websocket.__version__)"
if ($LASTEXITCODE -ne 0) { throw 'Bundled runtime import check failed' }
& $python -B -c "import json, pathlib, sys; p=pathlib.Path(sys.argv[1]); m=json.loads((p/'supported-clients.json').read_text(encoding='utf-8-sig')); assert m['clients'] and all(len(x)==64 for c in m['clients'] for x in c['sha256'].values()); [compile(f.read_bytes(), str(f), 'exec') for f in p.glob('*.py')]; print('Payload syntax and manifest OK')" $payload
if ($LASTEXITCODE -ne 0) { throw 'Payload check failed' }

if ($PayloadOnly) {
    [IO.File]::WriteAllText((Join-Path $buildRoot 'last-payload.txt'), $payload, $utf8)
    Write-Host ('Test payload: ' + $payload)
    return
}

if (-not $InnoCompiler) {
    $InnoCompiler = Join-Path $buildRoot 'InnoSetup\ISCC.exe'
    if (-not (Test-Path -LiteralPath $InnoCompiler)) {
        $compilerInstaller = Get-PinnedDownload $dependencyLock.innoSetup
        $signature = Get-AuthenticodeSignature -LiteralPath $compilerInstaller
        if ($signature.Status -ne 'Valid' -or $signature.SignerCertificate.Subject -notmatch 'Pyrsys B\.V\.') { throw 'Inno Setup publisher verification failed' }
        $compilerDirectory = Split-Path -Parent $InnoCompiler
        $process = Start-Process -FilePath $compilerInstaller -ArgumentList @('/VERYSILENT','/SUPPRESSMSGBOXES','/NORESTART','/CURRENTUSER','/NOICONS',('/DIR="' + $compilerDirectory + '"')) -WindowStyle Hidden -Wait -PassThru
        if ($process.ExitCode -ne 0 -or -not (Test-Path -LiteralPath $InnoCompiler)) { throw 'Inno Setup compiler installation failed' }
    }
}
& $InnoCompiler /Q "/DAppVersion=$Version" "/DWinVersion=$winVersion" "/DPayloadDir=$payload" "/DReleaseDir=$dist" (Join-Path $repo 'installer\setup.iss')
if ($LASTEXITCODE -ne 0) { throw 'Installer compilation failed' }
$prefix = 'NcmWasapiExtension-' + $Version + '-windows-x64'
$portable = Join-Path $dist ($prefix + '-portable.zip')
if (Test-Path -LiteralPath $portable) { Remove-Item -LiteralPath $portable }
[IO.Compression.ZipFile]::CreateFromDirectory($stage, $portable, [IO.Compression.CompressionLevel]::Optimal, $false)
# Only the application folder is in the portable zip; AssemblyInfo is build-only.
$archive = [IO.Compression.ZipFile]::Open($portable, [IO.Compression.ZipArchiveMode]::Update)
try { $entry = $archive.GetEntry('AssemblyInfo.cs'); if ($entry) { $entry.Delete() } } finally { $archive.Dispose() }
& (Join-Path $PSScriptRoot 'package-source.ps1') -Version $Version -RepoRoot $repo -OutputDirectory $dist -WriteChecksums | Out-Null
[IO.File]::WriteAllText((Join-Path $buildRoot 'last-payload.txt'), $payload, $utf8)
Write-Host ('Release artifacts: ' + $dist)

[CmdletBinding()]
param(
    [switch]$SkipDownload
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

$version = "1.11.0"
$releaseTag = "runtime-llamacpp-v0.1.9"
$cpuUrl = "https://github.com/modelscope/FunASR/releases/download/$releaseTag/funasr-llamacpp-windows-x64-avx2.zip"
$cpuSha256 = "F2A1389658E6FB5F5F93C7BAD98B5CE100EB4811E0E3C39603E39466773B1B4C"
$cudaUrl = "https://github.com/modelscope/FunASR/releases/download/$releaseTag/funasr-llamacpp-windows-x64-cuda.zip"
$cudaSha256 = "3809FB22EC977B29D86C1CA1D132A16A90019949D7AD2A0262292A5732BF596A"
$modelUrl = "https://huggingface.co/FunAudioLLM/SenseVoiceSmall-GGUF/resolve/main/sensevoice-small-q8.gguf"
$modelSha256 = "4AE45C94422DE949B387E2E0FB10D7E14E4C42C69DB30C3444ECC7D4B844B7C5"
$vadUrl = "https://huggingface.co/FunAudioLLM/fsmn-vad-GGUF/resolve/main/fsmn-vad.gguf"
$vadSha256 = "1270F2559C495F4E7B6E739541151027D360761A3FDA43FC147034F5719F5479"

$cache = Join-Path $root ".cache\release-deps\sensevoice-v0.1.9"
$cpuZip = Join-Path $cache "funasr-llamacpp-windows-x64-avx2.zip"
$cudaZip = Join-Path $cache "funasr-llamacpp-windows-x64-cuda.zip"
$modelFile = Join-Path $cache "sensevoice-small-q8.gguf"
$vadFile = Join-Path $cache "fsmn-vad.gguf"
$cpuExpanded = Join-Path $cache "avx2"
$cudaExpanded = Join-Path $cache "cuda"
$funasrTarget = Join-Path $root "tools\funasr"
$release = Join-Path $root "release"

New-Item -ItemType Directory -Force -Path $cache, $funasrTarget, $release | Out-Null

function Assert-Hash {
    param([string]$Path, [string]$Expected)
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "Missing file: $Path"
    }
    $actual = (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToUpperInvariant()
    if ($actual -ne $Expected.ToUpperInvariant()) {
        throw "SHA-256 mismatch: $Path`nExpected: $Expected`nActual: $actual"
    }
}

function Get-VerifiedDownload {
    param([string]$Uri, [string]$Path, [string]$Expected, [string]$Label)
    if (Test-Path -LiteralPath $Path) {
        $actual = (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToUpperInvariant()
        if ($actual -eq $Expected.ToUpperInvariant()) { return }
        Remove-Item -LiteralPath $Path -Force
    }
    $partial = "$Path.download"
    if (Test-Path -LiteralPath $partial) { Remove-Item -LiteralPath $partial -Force }
    Write-Host "Downloading $Label..."
    Invoke-WebRequest -Uri $Uri -OutFile $partial
    Assert-Hash -Path $partial -Expected $Expected
    Move-Item -LiteralPath $partial -Destination $Path -Force
}

if (-not $SkipDownload) {
    Get-VerifiedDownload -Uri $cpuUrl -Path $cpuZip -Expected $cpuSha256 -Label "FunASR SenseVoice CPU AVX2 runtime"
    Get-VerifiedDownload -Uri $cudaUrl -Path $cudaZip -Expected $cudaSha256 -Label "FunASR SenseVoice CUDA runtime"
    Get-VerifiedDownload -Uri $modelUrl -Path $modelFile -Expected $modelSha256 -Label "SenseVoiceSmall Q8 model"
    Get-VerifiedDownload -Uri $vadUrl -Path $vadFile -Expected $vadSha256 -Label "FSMN-VAD model"
}

if (-not $SkipDownload) {
    Assert-Hash -Path $cpuZip -Expected $cpuSha256
    Assert-Hash -Path $cudaZip -Expected $cudaSha256
    Assert-Hash -Path $modelFile -Expected $modelSha256
    Assert-Hash -Path $vadFile -Expected $vadSha256

    foreach ($entry in @(@($cpuZip, $cpuExpanded), @($cudaZip, $cudaExpanded))) {
        if (-not (Test-Path -LiteralPath (Join-Path $entry[1] "llama-funasr-sensevoice.exe"))) {
            New-Item -ItemType Directory -Force -Path $entry[1] | Out-Null
            Expand-Archive -LiteralPath $entry[0] -DestinationPath $entry[1] -Force
        }
    }

    New-Item -ItemType Directory -Force -Path (Join-Path $funasrTarget "cpu"), (Join-Path $funasrTarget "cuda"), (Join-Path $funasrTarget "models") | Out-Null
    Copy-Item -LiteralPath (Join-Path $cpuExpanded "llama-funasr-sensevoice.exe") -Destination (Join-Path $funasrTarget "cpu\llama-funasr-sensevoice.exe") -Force
    Copy-Item -LiteralPath (Join-Path $cudaExpanded "llama-funasr-sensevoice.exe") -Destination (Join-Path $funasrTarget "cuda\llama-funasr-sensevoice.exe") -Force
    Copy-Item -LiteralPath $modelFile -Destination (Join-Path $funasrTarget "models\sensevoice-small-q8.gguf") -Force
    Copy-Item -LiteralPath $vadFile -Destination (Join-Path $funasrTarget "models\fsmn-vad.gguf") -Force
}

$legacyWhisper = [IO.Path]::GetFullPath((Join-Path $root "tools\whisper"))
if ($legacyWhisper.StartsWith([IO.Path]::GetFullPath($root), [StringComparison]::OrdinalIgnoreCase) -and (Test-Path -LiteralPath $legacyWhisper)) {
    Remove-Item -LiteralPath $legacyWhisper -Recurse -Force
}

$required = @(
    "tools\yt-dlp.exe",
    "tools\ffmpeg.exe",
    "tools\funasr\cpu\llama-funasr-sensevoice.exe",
    "tools\funasr\cuda\llama-funasr-sensevoice.exe",
    "tools\funasr\models\sensevoice-small-q8.gguf",
    "tools\funasr\models\fsmn-vad.gguf",
    "tools\funasr\licenses\FUNASR-LICENSE.txt",
    "tools\funasr\licenses\APACHE-2.0.txt",
    "assets\zhuanjiekou.ico",
    "README.md",
    "THIRD_PARTY_NOTICES.txt"
)
foreach ($relative in $required) {
    if (-not (Test-Path -LiteralPath (Join-Path $root $relative))) {
        throw "Missing release dependency: $relative"
    }
}

$pythonCandidates = @(
    (Join-Path $root ".venv\Scripts\python.exe"),
    $env:ZHUANJIEKOU_PYTHON
)
$python = $pythonCandidates | Where-Object { $_ -and (Test-Path -LiteralPath $_) } | Select-Object -First 1
if (-not $python) { throw "Python build runtime not found." }
$pythonRoot = Split-Path -Parent $python
$pyinstaller = Join-Path $pythonRoot "Scripts\pyinstaller.exe"
if (-not (Test-Path -LiteralPath $pyinstaller)) {
    $pyinstaller = Join-Path $pythonRoot "pyinstaller.exe"
}
$makensis = "C:\Program Files (x86)\NSIS\makensis.exe"
foreach ($tool in @($pyinstaller, $makensis)) {
    if (-not (Test-Path -LiteralPath $tool)) { throw "Build tool not found: $tool" }
}

Write-Host "Checking application source..."
& $python -m py_compile (Join-Path $root "app.py") (Join-Path $root "transcription_engine.py") (Join-Path $root "media_resolver.py") (Join-Path $root "browser_capture.py")
if ($LASTEXITCODE -ne 0) { throw "Python source check failed." }

Write-Host "Building standalone application directory..."
& $pyinstaller --noconfirm --clean (Join-Path $root "ZhuanJieKou.spec")
if ($LASTEXITCODE -ne 0) { throw "PyInstaller build failed." }

$dist = Join-Path $root "dist\ZhuanJieKou"
foreach ($name in @("README.md", "THIRD_PARTY_NOTICES.txt")) {
    Copy-Item -LiteralPath (Join-Path $root $name) -Destination (Join-Path $dist $name) -Force
    Copy-Item -LiteralPath (Join-Path $root $name) -Destination (Join-Path $release $name) -Force
}

Write-Host "Building single-file installer..."
& $makensis "/INPUTCHARSET" "UTF8" "/DROOT=$root" (Join-Path $root "installer\ZhuanJieKou.nsi")
if ($LASTEXITCODE -ne 0) { throw "NSIS build failed." }

$installer = Get-ChildItem -LiteralPath $release -File |
    Where-Object { $_.Name -like "*Setup-$version-x64.exe" } |
    Sort-Object LastWriteTime -Descending |
    Select-Object -First 1
if (-not $installer) { throw "Installer output was not created." }

$installerHash = (Get-FileHash -LiteralPath $installer.FullName -Algorithm SHA256).Hash.ToUpperInvariant()
Set-Content -LiteralPath (Join-Path $release "SHA256SUMS.txt") -Value "$installerHash *$($installer.Name)" -Encoding ascii

$sizeMb = [Math]::Round($installer.Length / 1MB, 1)
Write-Host ""
Write-Host "Release complete: $($installer.FullName)"
Write-Host "Size: $sizeMb MB"
Write-Host "SHA-256: $installerHash"

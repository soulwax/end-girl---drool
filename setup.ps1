<#
  setup.ps1 - install auvide prerequisites on Windows via scoop, and cache the
  Real-ESRGAN models locally.

  auvide does NOT bundle binaries: it resolves ffmpeg / ffprobe /
  realesrgan-ncnn-vulkan from PATH. scoop is the canonical installer on Windows.

  Usage:  powershell -ExecutionPolicy Bypass -File .\setup.ps1
#>
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

function Have($n) { [bool](Get-Command $n -ErrorAction SilentlyContinue) }

if (-not (Have scoop)) {
    Write-Host "[scoop] not found. Install it first (https://scoop.sh):" -ForegroundColor Yellow
    Write-Host '  Set-ExecutionPolicy -Scope CurrentUser RemoteSigned'
    Write-Host '  irm get.scoop.sh | iex'
    exit 1
}

Write-Host "[deps] scoop install ffmpeg realesrgan-ncnn-vulkan ..."
scoop install ffmpeg realesrgan-ncnn-vulkan

# The scoop realesrgan package ships without model weights -> cache them locally.
$cache = Join-Path $env:LOCALAPPDATA "auvide\models"
New-Item -ItemType Directory -Force $cache | Out-Null
if (Get-ChildItem $cache -Filter *.param -ErrorAction SilentlyContinue) {
    Write-Host "[models] already cached at $cache"
}
else {
    Write-Host "[models] downloading Real-ESRGAN models ..."
    $tmp = Join-Path $env:TEMP "auvide-setup"
    New-Item -ItemType Directory -Force $tmp | Out-Null
    $zip = Join-Path $tmp "realesrgan.zip"
    Invoke-WebRequest "https://github.com/xinntao/Real-ESRGAN/releases/download/v0.2.5.0/realesrgan-ncnn-vulkan-20220424-windows.zip" `
        -OutFile $zip -MaximumRedirection 5 -TimeoutSec 300
    $ex = Join-Path $tmp "re"
    Expand-Archive $zip $ex -Force
    Get-ChildItem $ex -Recurse -Include *.param, *.bin |
        ForEach-Object { Copy-Item $_.FullName (Join-Path $cache $_.Name) -Force }
    Write-Host "[models] cached at $cache"
}

Write-Host ""
Write-Host "Done. Prerequisites:" -ForegroundColor Green
foreach ($t in "ffmpeg", "ffprobe", "realesrgan-ncnn-vulkan") {
    $c = Get-Command $t -ErrorAction SilentlyContinue
    Write-Host ("  {0,-24} {1}" -f $t, $(if ($c) { $c.Source } else { "MISSING" }))
}
Write-Host ""
Write-Host "Run it:  uv run --python 3.12 --with pillow gui.py"

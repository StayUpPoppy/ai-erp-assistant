#Requires -Version 5.1
<#
.SYNOPSIS
  下载并静默安装 UB Mannheim 版 Tesseract OCR（64 位），默认安装到用户目录（无需管理员）。

.PARAMETER InstallDir
  安装目录，默认：$env:LOCALAPPDATA\Programs\Tesseract-OCR
#>
param(
  [string] $InstallDir = ""
)

$ErrorActionPreference = "Stop"

$version = "5.4.0.20240606"
$file = "tesseract-ocr-w64-setup-$version.exe"
# 依次尝试：GitHub 官方、Mannheim 镜像、国内镜像（若失效可改脚本或手动下载后安装）
$urls = @(
  "https://github.com/UB-Mannheim/tesseract/releases/download/v$version/$file",
  "https://digi.bib.uni-mannheim.de/tesseract/$file",
  "https://mirrors.nju.edu.cn/github-release/UB-Mannheim/tesseract/v$version/$file",
  "https://mirror.ghproxy.com/https://github.com/UB-Mannheim/tesseract/releases/download/v$version/$file"
)

if (-not $InstallDir) {
  $installDir = Join-Path $env:LOCALAPPDATA "Programs\Tesseract-OCR"
} else {
  $installDir = $InstallDir
}

$tmp = Join-Path $env:TEMP $file
Write-Host "Downloading $file ..."
$ok = $false
foreach ($u in $urls) {
  try {
    Invoke-WebRequest -Uri $u -OutFile $tmp -UseBasicParsing -TimeoutSec 120
    $ok = $true
    break
  } catch {
    Write-Warning "Download failed from $u : $_"
  }
}
if (-not $ok) {
  throw "All download URLs failed. Install manually from https://github.com/UB-Mannheim/tesseract/wiki"
}

New-Item -ItemType Directory -Force -Path $installDir | Out-Null
Write-Host "Installing silently to $installDir ..."
$p = Start-Process -FilePath $tmp -ArgumentList @(
  "/VERYSILENT",
  "/SUPPRESSMSGBOXES",
  "/NORESTART",
  "/DIR=$installDir"
) -Wait -PassThru

if ($p.ExitCode -ne 0) {
  throw "Installer exit code $($p.ExitCode)"
}

$exe = Join-Path $installDir "tesseract.exe"
if (-not (Test-Path $exe)) {
  throw "tesseract.exe not found at $exe"
}

Write-Host "OK: $exe"
Write-Host "Optional: set in repo .env -> TESSERACT_CMD=$exe"
& $exe --version

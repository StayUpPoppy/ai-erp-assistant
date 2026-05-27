<#
.SYNOPSIS
  在你本机对 Datynk 常见主数据分页路径批量发 GET，打印 HTTP 状态与是否像 Datynk 信封（code=200 且 data.records 为数组）。
  用于一次性校准 ERP_*_SEARCH_PATH，无需在聊天里逐条贴 F12。

.PARAMETER SecretsFile
  凭据 JSON 路径。默认读取与本脚本同目录的 datynk-probe.credentials.local.json（该文件已加入 .gitignore，勿提交）。
  也可用环境变量 DATYNK_PROBE_USER / DATYNK_PROBE_PASSWORD / DATYNK_PROBE_ORG / DATYNK_PROBE_BASE_URL（勿把密码写进命令行历史）。

.PARAMETER BaseUrl
  根地址，默认 https://erp.datynk.com

.PARAMETER Org
  传给各 page 的 org（与后台一致，如 英科1厂）

.PARAMETER User / Password
  若均提供，则先 POST /api/auth/login 再带 Cookie 请求。未在命令行提供时，会尝试 SecretsFile 与环境变量。

.PARAMETER Keyword
  附加在查询串中的模糊参数，默认 keyword=a（若某接口不用 keyword，仅作探测仍可能 200 但 records 为空）

.PARAMETER ExtraQuery
  追加到每条 URL 的查询串片段（不含前导 &），例如 vendorName=x（可选）

安全说明
  请勿在 Cursor 聊天、截图或 Git 中发送真实密码。只在本机创建 datynk-probe.credentials.local.json（从 .example 复制）。
#>
param(
    [string]$SecretsFile = "",
    [string]$BaseUrl = "https://erp.datynk.com",
    [string]$Org = "",
    [string]$User = "",
    [string]$Password = "",
    [string]$Keyword = "a",
    [string]$ExtraQuery = ""
)

$ErrorActionPreference = "Stop"
$bp = $PSBoundParameters
$defSecrets = Join-Path $PSScriptRoot "datynk-probe.credentials.local.json"
$pathToUse = if ($SecretsFile.Trim().Length -gt 0) { $SecretsFile.Trim() } else { $defSecrets }

if (Test-Path -LiteralPath $pathToUse) {
    $j = Get-Content -LiteralPath $pathToUse -Raw -Encoding UTF8 | ConvertFrom-Json
    if (-not $bp.ContainsKey("BaseUrl") -and $j.baseUrl) { $BaseUrl = [string]$j.baseUrl }
    if (-not $bp.ContainsKey("Org") -and $j.org) { $Org = [string]$j.org }
    if (-not $bp.ContainsKey("User") -and $j.username) { $User = [string]$j.username }
    if (-not $bp.ContainsKey("Password") -and $j.password) { $Password = [string]$j.password }
}
if (-not $bp.ContainsKey("User") -and $env:DATYNK_PROBE_USER) { $User = [string]$env:DATYNK_PROBE_USER }
if (-not $bp.ContainsKey("Password") -and $env:DATYNK_PROBE_PASSWORD) { $Password = [string]$env:DATYNK_PROBE_PASSWORD }
if (-not $bp.ContainsKey("Org") -and $env:DATYNK_PROBE_ORG) { $Org = [string]$env:DATYNK_PROBE_ORG }
if (-not $bp.ContainsKey("BaseUrl") -and $env:DATYNK_PROBE_BASE_URL) { $BaseUrl = [string]$env:DATYNK_PROBE_BASE_URL }

$root = $BaseUrl.TrimEnd("/")
$session = New-Object Microsoft.PowerShell.Commands.WebRequestSession

if ($User -and $Password) {
    $loginUrl = "$root/api/auth/login"
    Write-Host "POST $loginUrl (user=$User)"
    $body = (@{ username = $User; password = $Password } | ConvertTo-Json -Compress)
    $null = Invoke-RestMethod -Uri $loginUrl -Method Post -Body $body -ContentType "application/json; charset=utf-8" -WebSession $session -TimeoutSec 30
    Write-Host "Login OK`n"
} else {
    Write-Host "（未配置登录：未找到 User+Password。请复制 backend/scripts/datynk-probe.credentials.local.json.example 为 datynk-probe.credentials.local.json 并填写，或设置 DATYNK_PROBE_USER / DATYNK_PROBE_PASSWORD。）`n"
}

$paths = @(
    @{ Path = "/api/supplier/page"; Note = "供应商（Datynk 常见）" },
    @{ Path = "/api/vendor/page"; Note = "供应商（旧猜名，对照用）" },
    @{ Path = "/api/warehouse/page"; Note = "仓库" },
    @{ Path = "/api/bin/page"; Note = "仓位（菜单「仓位管理」若与仓库不同 path 可对照）" },
    @{ Path = "/api/storage/page"; Note = "仓储/库存类（备选）" },
    @{ Path = "/api/material/page"; Note = "物料" },
    @{ Path = "/api/tax/page"; Note = "税码" },
    @{ Path = "/api/customer/page"; Note = "客户分页" }
)

function Summarize-Body([string]$json) {
    try {
        $o = $json | ConvertFrom-Json
        if ($null -eq $o) { return "non-json" }
        $code = $o.code
        $rec = $o.data.records
        $n = 0
        if ($rec -is [Array]) { $n = $rec.Count }
        return "code=$code records_count=$n"
    } catch {
        return "parse_error"
    }
}

foreach ($row in $paths) {
    $p = $row.Path
    $kwParam = if ($p -match "/supplier/") { "supplierName" } else { "keyword" }
    $parts = @("pageNum=1", "pageSize=5", "$kwParam=$([System.Uri]::EscapeDataString($Keyword))")
    if ($Org) {
        $parts += "org=$([System.Uri]::EscapeDataString($Org))"
    }
    if ($ExtraQuery.Trim().Length -gt 0) {
        $parts += $ExtraQuery.Trim().TrimStart("&")
    }
    $url = "$root$p?" + ($parts -join "&")
    Write-Host "---- $($row.Note) ----"
    Write-Host "GET $url"
    try {
        $resp = Invoke-WebRequest -Uri $url -WebSession $session -UseBasicParsing -TimeoutSec 25 -Method Get
        $sum = Summarize-Body $resp.Content
        Write-Host "HTTP $($resp.StatusCode)  $sum"
    } catch {
        $st = $null
        if ($_.Exception.Response) { $st = [int]$_.Exception.Response.StatusCode.value__ }
        Write-Host "HTTP $st  $($_.Exception.Message)"
    }
    Write-Host ""
}

Write-Host "说明："
Write-Host "  - HTTP 200 且 records_count>0：路径可用，可把 ERP_*_SEARCH_PATH 配成该 Path。"
Write-Host "  - HTTP 404：路径不对或无权；仓库/物料请换 F12 里真实 path 写进 .env。"
Write-Host "  - 若某接口模糊字段不是 keyword，用 F12 看查询参数名，再设 ERP_*_SEARCH_QUERY_KEY。"
Write-Host "  - 本脚本只读分页，不写数据。详见 backend/docs/runbook.md Datynk 小节。"
Write-Host "  - 下次把本页输出（可打码域名）贴给协作者即可；勿在聊天发送密码。"

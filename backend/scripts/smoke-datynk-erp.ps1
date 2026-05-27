<#
.SYNOPSIS
    快速探测 Datynk 风格 ERP：销售订单分页 GET、可选 Cookie 登录、可选客户保存 POST。

.PARAMETER BaseUrl
    根地址，默认 https://erp.datynk.com

.PARAMETER Org
    传给 page 的 org 查询参数（工厂/组织名）

.PARAMETER User / Password
    若均提供，则先 POST /api/auth/login 再带 Cookie 请求分页接口。

.PARAMETER ProbeCustomerSave
    指定后在销售订单分页 GET 之后（若未 -SkipSaleOrderPage）再 POST 客户保存，共用 Cookie 会话。

.PARAMETER SkipSaleOrderPage
    仅测客户保存等场景：跳过 GET 分页（常与 -ProbeCustomerSave 同用）。

.PARAMETER CustomerSavePath
    客户保存路径，默认 /api/customer/save

.PARAMETER CustomerInnerJson
    customer 内层 JSON 字符串。不传且已提供 -Org 时，内层为 org + 随机 customerName（对方若还有必填项会失败，请改本参数）。
#>
param(
    [string]$BaseUrl = "https://erp.datynk.com",
    [string]$Org = "",
    [string]$User = "",
    [string]$Password = "",
    [string]$PagePath = "/api/sale-order/page",
    [int]$PageNum = 1,
    [int]$PageSize = 5,
    [switch]$ProbeCustomerSave,
    [switch]$SkipSaleOrderPage,
    [string]$CustomerSavePath = "/api/customer/save",
    [string]$CustomerInnerJson = ""
)

$ErrorActionPreference = "Stop"
$root = $BaseUrl.TrimEnd("/")
if (-not $PagePath.StartsWith("/")) { $PagePath = "/" + $PagePath }

$session = New-Object Microsoft.PowerShell.Commands.WebRequestSession

if ($User -and $Password) {
    $loginUrl = "$root/api/auth/login"
    Write-Host "POST $loginUrl"
    $body = (@{ username = $User; password = $Password } | ConvertTo-Json -Compress)
    $null = Invoke-RestMethod -Uri $loginUrl -Method Post -Body $body -ContentType "application/json; charset=utf-8" -WebSession $session -TimeoutSec 30
    Write-Host "Login OK (session cookie stored)"
}

if (-not $SkipSaleOrderPage) {
    $parts = @("pageNum=$PageNum", "pageSize=$PageSize")
    if ($Org) {
        $enc = [System.Uri]::EscapeDataString($Org)
        $parts += "org=$enc"
    }
    $qs = $parts -join "&"
    $pageUrl = "$root$PagePath" + "?" + $qs
    Write-Host "GET $pageUrl"
    try {
        $r = Invoke-RestMethod -Uri $pageUrl -Method Get -WebSession $session -TimeoutSec 30
        $r | ConvertTo-Json -Depth 6 -Compress
    } catch {
        Write-Host "Request failed: $($_.Exception.Message)"
        if ($_.Exception.Response) {
            Write-Host "Status:" $_.Exception.Response.StatusCode.value__
        }
        exit 1
    }
} else {
    Write-Host "Skipping sale-order page (-SkipSaleOrderPage)"
}

if (-not $ProbeCustomerSave) {
    exit 0
}

if (-not $CustomerSavePath.StartsWith("/")) { $CustomerSavePath = "/" + $CustomerSavePath }
$saveUrl = "$root$CustomerSavePath"
$inner = $null
if ($CustomerInnerJson.Trim().Length -gt 0) {
    $inner = $CustomerInnerJson | ConvertFrom-Json
} else {
    if (-not $Org) {
        Write-Error "ProbeCustomerSave: specify -Org for default inner payload, or pass -CustomerInnerJson with full customer object JSON."
        exit 1
    }
    $suffix = [guid]::NewGuid().ToString("N").Substring(0, 8)
    $inner = @{ org = $Org; customerName = "smoke-$suffix" }
}
$saveBody = @{ customer = $inner } | ConvertTo-Json -Depth 8 -Compress
Write-Host "POST $saveUrl"
try {
    $cr = Invoke-RestMethod -Uri $saveUrl -Method Post -Body $saveBody -ContentType "application/json; charset=utf-8" -WebSession $session -TimeoutSec 30
    $cr | ConvertTo-Json -Depth 6 -Compress
} catch {
    Write-Host "Customer save failed: $($_.Exception.Message)"
    if ($_.Exception.Response) {
        Write-Host "Status:" $_.Exception.Response.StatusCode.value__
    }
    exit 1
}

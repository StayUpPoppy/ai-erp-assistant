param(
  [string]$ApiBaseUrl = "http://127.0.0.1:8042",
  [string]$FrontendUrl = "http://127.0.0.1:3084"
)

$ErrorActionPreference = "Stop"

function Write-StatusLine {
  param(
    [string]$Name,
    [string]$Value,
    [string]$Level = "info"
  )
  $color = switch ($Level) {
    "ok" { "Green" }
    "warn" { "Yellow" }
    "error" { "Red" }
    default { "Gray" }
  }
  Write-Host ("{0,-26} {1}" -f $Name, $Value) -ForegroundColor $color
}

Write-Host "AI ERP Assistant local health check" -ForegroundColor Cyan
Write-Host ("API      : {0}" -f $ApiBaseUrl)
Write-Host ("Frontend : {0}" -f $FrontendUrl)
Write-Host ""

try {
  $health = Invoke-RestMethod -Uri "$ApiBaseUrl/health" -Method Get -TimeoutSec 10
} catch {
  Write-StatusLine "api_health" "FAILED: $($_.Exception.Message)" "error"
  exit 1
}

Write-StatusLine "api_health" $health.status "ok"
Write-StatusLine "llm_router" ($(if ($health.llm_router_enabled) { "enabled" } else { "disabled" })) ($(if ($health.llm_router_enabled) { "ok" } else { "warn" }))
Write-StatusLine "llm_api_key" ($(if ($health.llm_api_key_configured) { "configured" } else { "missing" })) ($(if ($health.llm_api_key_configured) { "ok" } else { "warn" }))
Write-StatusLine "llm_model" $health.llm_model "info"
Write-StatusLine "erp_mode" $health.erp_client_mode ($(if ($health.erp_client_mode -eq "real") { "ok" } else { "warn" }))
Write-StatusLine "erp_body_style" $health.erp_create_body_style "info"
Write-StatusLine "queue" ("{0} / {1} / available={2}" -f $health.queue_backend, $health.queue_name, $health.queue_available) ($(if ($health.queue_available) { "ok" } else { "warn" }))
Write-StatusLine "ocr_engine" $health.ocr_engine "info"

try {
  $frontend = Invoke-WebRequest -Uri $FrontendUrl -Method Get -TimeoutSec 10 -UseBasicParsing
  Write-StatusLine "frontend_http" ("HTTP {0}" -f [int]$frontend.StatusCode) "ok"
} catch {
  Write-StatusLine "frontend_http" "FAILED: $($_.Exception.Message)" "warn"
}

Write-Host ""
if ($health.erp_client_mode -ne "real") {
  Write-Host "Note: ERP is running in mock mode. Creating a draft will not write to the real ERP." -ForegroundColor Yellow
}
if (-not $health.llm_api_key_configured) {
  Write-Host "Note: LLM key is missing. Assistant will fall back where possible." -ForegroundColor Yellow
}

param(
  [ValidateSet("stable", "balanced", "performance", "ultra-stable")]
  [string]$Profile = "balanced",
  [string]$ComposeDir = "E:\AI_Projects\ComfyUI-Project\pytorch2.8.0-cu128"
)

$ErrorActionPreference = "Stop"

function Assert-DockerReady {
  docker version *> $null
  if ($LASTEXITCODE -ne 0) {
    throw "Docker Desktop / Docker engine is not reachable. Start Docker Desktop first, then retry."
  }
}

if (-not (Test-Path -LiteralPath $ComposeDir)) {
  throw "Compose directory not found: $ComposeDir"
}

$baseCompose = Join-Path $ComposeDir "docker-compose.yml"
$profileCompose = Join-Path $ComposeDir "docker-compose.$Profile.yml"
$statePath = Join-Path $ComposeDir "comfyui_runtime_profile.json"

if (-not (Test-Path -LiteralPath $baseCompose)) {
  throw "Base compose file not found: $baseCompose"
}
if (-not (Test-Path -LiteralPath $profileCompose)) {
  throw "Profile compose file not found: $profileCompose"
}

Assert-DockerReady

Push-Location $ComposeDir
try {
  Write-Output "Switching ComfyUI Docker profile to '$Profile'..."
  docker compose -f docker-compose.yml -f "docker-compose.$Profile.yml" up -d --force-recreate
  if ($LASTEXITCODE -ne 0) {
    throw "docker compose up failed with exit code $LASTEXITCODE."
  }

  $payload = [ordered]@{
    profile = $Profile
    compose_dir = $ComposeDir
    applied_at = (Get-Date).ToString("yyyy-MM-dd HH:mm:ss")
    base_compose = $baseCompose
    profile_compose = $profileCompose
  }
  $payload | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $statePath -Encoding UTF8

  Write-Output ""
  Write-Output "Current containers:"
  docker compose -f docker-compose.yml -f "docker-compose.$Profile.yml" ps
  if ($LASTEXITCODE -ne 0) {
    throw "docker compose ps failed with exit code $LASTEXITCODE."
  }
}
finally {
  Pop-Location
}

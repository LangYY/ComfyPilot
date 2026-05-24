param(
  [string]$ComposeDir = "E:\AI_Projects\ComfyUI-Project\pytorch2.8.0-cu128",
  [string]$ContainerName = "comfyui_cu128_v0812",
  [int]$LogTail = 500
)

$ErrorActionPreference = "Continue"

Write-Output "== ComfyUI Docker diagnosis =="
Write-Output "Time: $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')"
Write-Output "ComposeDir: $ComposeDir"
Write-Output "ContainerName: $ContainerName"
Write-Output ""

Write-Output "== nvidia-smi =="
nvidia-smi 2>&1
Write-Output ""

Write-Output "== docker availability =="
docker version 2>&1
if ($LASTEXITCODE -ne 0) {
  Write-Output "Docker engine is not reachable. Start Docker Desktop first to inspect container logs."
  exit 0
}
Write-Output ""

Write-Output "== docker ps =="
docker ps -a --filter "name=$ContainerName" --format "table {{.ID}}\t{{.Names}}\t{{.Status}}\t{{.Image}}\t{{.Ports}}" 2>&1
Write-Output ""

Write-Output "== docker inspect restart / exit info =="
docker inspect $ContainerName --format "RestartCount={{.RestartCount}} ExitCode={{.State.ExitCode}} OOMKilled={{.State.OOMKilled}} Error={{.State.Error}} StartedAt={{.State.StartedAt}} FinishedAt={{.State.FinishedAt}}" 2>&1
Write-Output ""

Write-Output "== recent suspicious logs =="
$logs = docker logs --tail $LogTail $ContainerName 2>&1
$patterns = "CUDA out of memory|out of memory|OutOfMemory|OOM|OOMKilled|Killed|RuntimeError|Traceback|Allocation|CUBLAS|cudnn"
$matches = $logs | Select-String -Pattern $patterns -CaseSensitive:$false
if ($matches) {
  $matches | ForEach-Object { $_.Line }
} else {
  Write-Output "No obvious OOM/error pattern found in the last $LogTail log lines."
}
Write-Output ""

Write-Output "== recent log tail =="
$logs | Select-Object -Last 120

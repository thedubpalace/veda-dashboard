#Requires -Version 7
<#
.SYNOPSIS
    Build and push a Docker image, reading NEXT_PUBLIC_* build args from an env file.

.PARAMETER Image
    Docker Hub image name, e.g. thedubpalace/matchday

.PARAMETER EnvFile
    Path to the env file (default: .env.local in the current directory)

.EXAMPLE
    .\docker-deploy.ps1 -Image thedubpalace/matchday
    .\docker-deploy.ps1 -Image thedubpalace/myapp -EnvFile .env.production
#>

param(
    [Parameter(Mandatory)][string]$Image,
    [string]$EnvFile = (Join-Path (Get-Location) '.env.local')
)

$ErrorActionPreference = 'Stop'

# ── parse env file ────────────────────────────────────────────────────────────
if (-not (Test-Path $EnvFile)) {
    Write-Error "Env file not found: $EnvFile"
    exit 1
}

$env_vars = @{}
Get-Content $EnvFile | ForEach-Object {
    if ($_ -match '^\s*([^#=\s][^=]*)\s*=\s*(.*)$') {
        $env_vars[$Matches[1].Trim()] = $Matches[2].Trim()
    }
}

# ── build args (NEXT_PUBLIC_* only — server secrets are NOT baked in) ─────────
$build_args = $env_vars.Keys |
    Where-Object { $_ -like 'NEXT_PUBLIC_*' } |
    ForEach-Object { "--build-arg", "$_=$($env_vars[$_])" }

# ── tags: latest + git sha ────────────────────────────────────────────────────
$git_sha = (git rev-parse --short HEAD 2>$null) ?? 'unknown'
$tag_latest = "${Image}:latest"
$tag_sha    = "${Image}:${git_sha}"

Write-Host "Building $tag_latest (sha: $git_sha)" -ForegroundColor Cyan

docker build @build_args -t $tag_latest -t $tag_sha .

if ($LASTEXITCODE -ne 0) { Write-Error "docker build failed"; exit 1 }

# ── push ──────────────────────────────────────────────────────────────────────
Write-Host "Pushing..." -ForegroundColor Cyan
docker push $tag_latest
docker push $tag_sha

if ($LASTEXITCODE -ne 0) { Write-Error "docker push failed"; exit 1 }

Write-Host "Done — $tag_latest pushed. Watchtower will pull within 5 minutes." -ForegroundColor Green

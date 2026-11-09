# Envia o site (pasta docs) e o programa para o GitHub. O GitHub Pages republica sozinho em 1-2 minutos.
# Uso: powershell -ExecutionPolicy Bypass -File publicar_site.ps1
$ErrorActionPreference = "Stop"
$env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [Environment]::GetEnvironmentVariable("Path", "User")
Set-Location $PSScriptRoot
git add -A
$mudou = git status --porcelain
if (-not $mudou) { Write-Host "Nada novo para publicar."; exit 0 }
git commit -m ("Atualizacao do painel " + (Get-Date -Format "yyyy-MM-dd HH:mm")) | Out-Null
git pull --rebase --quiet
git push --quiet
Write-Host "Site publicado no GitHub."

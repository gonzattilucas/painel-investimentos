# Cria o repositorio publico "painel-investimentos" no GitHub, envia o projeto e liga o GitHub Pages (pasta /docs).
# Rode uma vez, depois de "gh auth login":  powershell -ExecutionPolicy Bypass -File criar_site_github.ps1
$ErrorActionPreference = "Stop"
$env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [Environment]::GetEnvironmentVariable("Path", "User")
Set-Location $PSScriptRoot
$repo = "painel-investimentos"

gh auth status | Out-Null
$login = gh api user --jq .login
$nome = gh api user --jq .name
if (-not $nome) { $nome = $login }
git config user.name "$nome"

if (-not (Test-Path .git)) { git init -b main -q }
git add -A
if (git status --porcelain) { git commit -q -m "Painel de investimentos: rastreador, site e recomendacoes" }

gh repo create $repo --public --source . --remote origin --push --description "Painel de acoes e FIIs: relatorio semanal e consenso das corretoras"
gh api -X POST "repos/$login/$repo/pages" -f "source[branch]=main" -f "source[path]=/docs" | Out-Null

Write-Host ""
Write-Host "Site: https://$login.github.io/$repo/  (fica no ar em 1 a 2 minutos)"

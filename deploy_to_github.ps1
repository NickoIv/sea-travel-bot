# deploy_to_github.ps1
# Запуск: .\deploy_to_github.ps1 -Message "Update bot"
# (remote origin и токен уже сохранены в .git/config)

param(
    [string]$Message = "Deploy $(Get-Date -Format 'yyyy-MM-dd HH:mm')",
    [string]$Branch  = "main"
)

$env:Path += ";C:\Program Files\Git\bin"

if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    Write-Host "Git не установлен." -ForegroundColor Red
    exit 1
}

git add -A
git commit -m $Message
if (-not $?) { Write-Host "Нечего коммитить или ошибка коммита" -ForegroundColor Yellow }

git pull --rebase origin $Branch
git push origin $Branch

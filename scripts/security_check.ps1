# ============================================================
# 部署前安全检查脚本（Windows PowerShell 版）
# 用法：.\scripts\security_check.ps1
# 依赖：git（可选）、gitleaks（可选）、pip-audit（可选）
# 未安装的工具会跳过并给出安装提示
# ============================================================

$ErrorActionPreference = "Continue"
$projectRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $projectRoot

Write-Host "========== 安全体检开始 ==========" -ForegroundColor Cyan
Write-Host "项目路径: $projectRoot"
Write-Host ""

# ---- 1. Git 状态检查 ----
Write-Host "[1] Git 仓库状态" -ForegroundColor Yellow
if (Test-Path ".git") {
    $tracked = git ls-files 2>$null
    $secretFiles = $tracked | Where-Object { $_ -match "secrets\.toml$|\.env$|\.env\." }
    if ($secretFiles) {
        Write-Host "  [!] 警告：以下敏感文件被 Git 跟踪：" -ForegroundColor Red
        $secretFiles | ForEach-Object { Write-Host "      $_" }
    } else {
        Write-Host "  [OK] 无敏感文件被 Git 跟踪" -ForegroundColor Green
    }
    $historyLeaks = git log --all --oneline -- ".streamlit/secrets.toml" ".env" 2>$null
    if ($historyLeaks) {
        Write-Host "  [!] 警告：Git 历史中出现过 secrets.toml 或 .env：" -ForegroundColor Red
        Write-Host $historyLeaks
    } else {
        Write-Host "  [OK] Git 历史无 secrets.toml / .env 记录" -ForegroundColor Green
    }
} else {
    Write-Host "  [INFO] 未初始化 Git 仓库（首次推送前请先 git init）" -ForegroundColor DarkGray
}
Write-Host ""

# ---- 2. .gitignore 检查 ----
Write-Host "[2] .gitignore 规则" -ForegroundColor Yellow
$gitignore = Get-Content ".gitignore" -ErrorAction SilentlyContinue
$required = @(".streamlit/secrets.toml", "venv/", ".venv/", "__pycache__/", "*.log", ".env")
foreach ($rule in $required) {
    if ($gitignore -match [regex]::Escape($rule.TrimEnd('/'))) {
        Write-Host "  [OK] $rule" -ForegroundColor Green
    } else {
        Write-Host "  [!] 缺失: $rule" -ForegroundColor Red
    }
}
Write-Host ""

# ---- 3. 源码硬编码密钥扫描 ----
Write-Host "[3] 源码硬编码密钥扫描" -ForegroundColor Yellow
$patterns = @(
    "sk-[A-Za-z0-9]{20,}",
    "sb_publishable_[A-Za-z0-9]{10,}",
    "sb_secret_[A-Za-z0-9]{10,}",
    "eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"
)
$pyFiles = Get-ChildItem -Path "." -Filter "*.py" -Recurse |
    Where-Object { $_.FullName -notmatch "\.venv|venv|__pycache__" }
$found = $false
foreach ($file in $pyFiles) {
    $content = Get-Content $file.FullName -Raw -ErrorAction SilentlyContinue
    foreach ($pat in $patterns) {
        $matches = [regex]::Matches($content, $pat)
        if ($matches.Count -gt 0) {
            Write-Host "  [!] $($file.Name): 发现 $($matches.Count) 处疑似密钥" -ForegroundColor Red
            $found = $true
        }
    }
}
if (-not $found) {
    Write-Host "  [OK] .py 源码未发现硬编码密钥" -ForegroundColor Green
}
Write-Host ""

# ---- 4. gitleaks（如果安装了） ----
Write-Host "[4] gitleaks 全仓库扫描" -ForegroundColor Yellow
$gitleaks = Get-Command gitleaks -ErrorAction SilentlyContinue
if ($gitleaks) {
    & gitleaks detect --source . --verbose --redact
    if ($LASTEXITCODE -eq 0) {
        Write-Host "  [OK] gitleaks 未发现泄露" -ForegroundColor Green
    } else {
        Write-Host "  [!] gitleaks 发现疑似泄露，请检查上方输出" -ForegroundColor Red
    }
} else {
    Write-Host "  [SKIP] gitleaks 未安装。安装方式：" -ForegroundColor DarkGray
    Write-Host "    https://github.com/gitleaks/gitleaks/releases" -ForegroundColor DarkGray
}
Write-Host ""

# ---- 5. pip-audit（如果安装了） ----
Write-Host "[5] 依赖漏洞审计 (pip-audit)" -ForegroundColor Yellow
$pipAudit = Get-Command pip-audit -ErrorAction SilentlyContinue
if ($pipAudit) {
    & pip-audit -r requirements.txt
    if ($LASTEXITCODE -eq 0) {
        Write-Host "  [OK] 未发现已知漏洞依赖" -ForegroundColor Green
    }
} else {
    Write-Host "  [SKIP] pip-audit 未安装。安装方式：pip install pip-audit" -ForegroundColor DarkGray
}
Write-Host ""

Write-Host "========== 安全体检完成 ==========" -ForegroundColor Cyan
Write-Host "请人工复核以上结果，确认无 [!] 警告后再推送 GitHub。" -ForegroundColor Yellow

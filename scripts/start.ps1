<#
    EikoCode 启动脚本

    用法：
        .\scripts\start.ps1

    它只做三件事：切到项目根目录、检查环境是否就绪、启动 EikoCode。
    凭据已经在 setup.ps1 里配过了，这里不再重复设置任何环境变量。
#>

$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $ProjectRoot

$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"

# 环境没装好就给出明确指引，不要让用户对着一堆红色堆栈发呆
if (-not (Test-Path $Python)) {
    Write-Host ""
    Write-Host "没找到虚拟环境：$Python" -ForegroundColor Red
    Write-Host "先在项目根目录跑这两行："
    Write-Host "  python -m venv .venv" -ForegroundColor White
    Write-Host '  .\.venv\Scripts\python.exe -m pip install -e ".[dev]"' -ForegroundColor White
    Write-Host ""
    exit 1
}

# 兜底：环境变量是进程启动时快照的。配置完之后，已经开着的窗口不会自动看到新值，
# Explorer 没刷新的话连新开的窗口也可能读不到。所以这里直接从注册表补进当前进程，
# 保证「任何窗口跑这个脚本都能用」，不用依赖 Windows 那套刷新机制。
$EikoVars = @(
    "EIKOCODE_ANTHROPIC_API_KEY",
    "EIKOCODE_OPENAI_API_KEY",
    "EIKOCODE_OPENAI_BASE_URL",
    "EIKOCODE_MODEL"
)
foreach ($name in $EikoVars) {
    if (-not [Environment]::GetEnvironmentVariable($name)) {
        $persisted = [Environment]::GetEnvironmentVariable($name, "User")
        if ($persisted) {
            Set-Item "env:$name" $persisted
        }
    }
}

if (-not $env:EIKOCODE_OPENAI_API_KEY -and -not $env:EIKOCODE_ANTHROPIC_API_KEY) {
    Write-Host ""
    Write-Host "没有找到 API Key。" -ForegroundColor Yellow
    Write-Host "先配置一次（只需要一次，之后所有窗口都能用）："
    Write-Host "  .\scripts\setup.ps1" -ForegroundColor White
    Write-Host ""
    exit 1
}

& $Python -m eikocode
exit $LASTEXITCODE

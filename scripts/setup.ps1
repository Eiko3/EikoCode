<#
    EikoCode 一次性配置脚本

    作用：把 API 凭据写进 Windows 用户级环境变量。写完之后，
          这台机器上任何新开的 PowerShell / Git Bash 都会自动带上，
          不用每次开终端都重新 export 一遍。

    用法：
        .\scripts\setup.ps1                 # 交互式配置（推荐第一次用）
        .\scripts\setup.ps1 -Show           # 查看当前已存的配置
        .\scripts\setup.ps1 -Clear          # 清除已存的配置
        .\scripts\setup.ps1 -Key "sk-xxx" -BaseUrl "..." -Model "..."   # 无交互

    安全提示：
        凭据存在 Windows 用户环境变量里（注册表 HKCU\Environment），
        只有当前 Windows 用户能读到。它不在项目目录里，不会被 git 提交。
#>

param(
    [string]$Key,
    [string]$BaseUrl = "https://api.deepseek.com/v1",
    [string]$Model = "deepseek-chat",
    [switch]$Show,
    [switch]$Clear
)

$ErrorActionPreference = "Stop"

$VarNames = @(
    "EIKOCODE_OPENAI_API_KEY",
    "EIKOCODE_OPENAI_BASE_URL",
    "EIKOCODE_MODEL"
)

function Notify-EnvironmentChange {
    <#
    改完用户级环境变量后要调这个。

    光写注册表是不够的：Explorer 的环境是它自己启动时快照的，不通知它刷新的话，
    之后从开始菜单/桌面新开的终端会继承那份旧快照，照样读不到刚配的 Key。

    两条路都试，全都失败也不报错——start.ps1 有兜底，会直接从注册表读进当前进程，
    所以刷新失败不会让 EikoCode 用不了，只会让「在窗口里直接敲 python -m eikocode」失效。
    #>
    $done = $false

    # 路一：P/Invoke 广播。某些机器的执行策略会禁掉 Add-Type，所以要容错。
    try {
        if (-not ("Win32.EnvNotify" -as [type])) {
            Add-Type -Namespace Win32 -Name EnvNotify -MemberDefinition @'
[DllImport("user32.dll", SetLastError = true, CharSet = CharSet.Auto)]
public static extern IntPtr SendMessageTimeout(
    IntPtr hWnd, uint Msg, UIntPtr wParam, string lParam,
    uint fuFlags, uint uTimeout, out UIntPtr lpdwResult);
'@ -ErrorAction Stop
        }
        $result = [UIntPtr]::Zero
        [void][Win32.EnvNotify]::SendMessageTimeout(
            [IntPtr]0xffff,   # HWND_BROADCAST
            0x001A,           # WM_SETTINGCHANGE
            [UIntPtr]::Zero,
            "Environment",
            0x0002,           # SMTO_ABORTIFHUNG
            5000,
            [ref]$result)
        $done = $true
    }
    catch {
        $done = $false
    }

    # 路二：setx 写任何东西都会自带一次广播。用一个无害的标记变量来借它的广播能力。
    if (-not $done) {
        try {
            & setx EIKOCODE_ENV_STAMP (Get-Date -Format "yyyyMMddHHmmss") 2>&1 | Out-Null
        }
        catch {
            # 两条路都不通也不影响使用：start.ps1 会从注册表兜底读取。
        }
    }
}

function Read-SecretKey {
    <#
    读一行 API Key，输入内容不回显。

    不能直接用 Read-Host：那会把整条 Key 明文打在屏幕上，
    旁边有人、或者你顺手截个图，Key 就泄露了。
    #>
    param([string]$Prompt)

    $secure = Read-Host $Prompt -AsSecureString
    $ptr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
    try {
        return [Runtime.InteropServices.Marshal]::PtrToStringAuto($ptr)
    }
    finally {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($ptr)
    }
}

function Show-Config {
    Write-Host "当前用户级配置：" -ForegroundColor Cyan
    Write-Host ""
    foreach ($name in $VarNames) {
        $value = [Environment]::GetEnvironmentVariable($name, "User")
        if (-not $value) {
            $display = "（未设置）"
        }
        elseif ($name -like "*KEY*") {
            # 密钥只露前 6 位，够你认出是哪个 key，又不至于整条泄在屏幕上
            $prefix = $value.Substring(0, [Math]::Min(6, $value.Length))
            $display = "$prefix...（共 $($value.Length) 位）"
        }
        else {
            $display = $value
        }
        Write-Host ("  {0,-28} {1}" -f $name, $display)
    }
    Write-Host ""
    Write-Host "（这些值来自用户环境变量，对任何新开的终端窗口都生效）" -ForegroundColor DarkGray
}

function Clear-Config {
    foreach ($name in $VarNames) {
        [Environment]::SetEnvironmentVariable($name, $null, "User")
        Remove-Item "env:$name" -ErrorAction SilentlyContinue
    }
    Notify-EnvironmentChange
    Write-Host ""
    Write-Host "已清除 EikoCode 的用户级配置。" -ForegroundColor Yellow
    Write-Host "重新运行 .\scripts\setup.ps1 可以再配一次。"
}

if ($Clear) {
    Clear-Config
    exit 0
}

if ($Show) {
    Show-Config
    exit 0
}

$alreadyConfigured = [bool][Environment]::GetEnvironmentVariable("EIKOCODE_OPENAI_API_KEY", "User")
if ($alreadyConfigured) {
    $title = "EikoCode 更改配置"
    $subtitle = "直接回车表示保留原值"
}
else {
    $title = "EikoCode 首次配置"
    $subtitle = "只需配这一次，之后新开终端自动生效"
}

Write-Host ""
Write-Host "  /\_/\    $title" -ForegroundColor White
Write-Host " ( ^.^ )   $subtitle" -ForegroundColor White
Write-Host "  > ^ <" -ForegroundColor White
Write-Host ""

# 1. API Key —— 没用 -Key 参数传就交互式问。
# 无论首次配置还是修改，一律走 Read-SecretKey，保证输入内容不回显到屏幕上。
if (-not $Key) {
    $existing = [Environment]::GetEnvironmentVariable("EIKOCODE_OPENAI_API_KEY", "User")
    if ($existing) {
        Write-Host "检测到已配置过 Key（$($existing.Substring(0, [Math]::Min(6, $existing.Length)))...）。" -ForegroundColor DarkGray
        $answer = Read-SecretKey "直接回车保留，或输入新 Key 覆盖（输入内容不显示）"
        if ([string]::IsNullOrWhiteSpace($answer)) {
            $Key = $existing
        }
        else {
            $Key = $answer
        }
    }
    else {
        Write-Host "请粘贴你的 API Key（输入内容不会显示在屏幕上，粘完直接回车）：" -ForegroundColor Cyan
        $Key = Read-SecretKey "Key"
    }
}

if ([string]::IsNullOrWhiteSpace($Key)) {
    Write-Host ""
    Write-Host "Key 是空的，配置已取消。" -ForegroundColor Red
    Write-Host "提示：如果 Read-Host 在这里报错，说明当前不是交互式终端，改用："
    Write-Host '  .\scripts\setup.ps1 -Key "你的key"' -ForegroundColor DarkGray
    exit 1
}

$Key = $Key.Trim()

# 2. 服务端地址
$defaultUrl = [Environment]::GetEnvironmentVariable("EIKOCODE_OPENAI_BASE_URL", "User")
if (-not $defaultUrl) { $defaultUrl = $BaseUrl }
$inputUrl = Read-Host "服务端地址（直接回车用 $defaultUrl）"
if ([string]::IsNullOrWhiteSpace($inputUrl)) { $inputUrl = $defaultUrl }

# 3. 模型名
$defaultModel = [Environment]::GetEnvironmentVariable("EIKOCODE_MODEL", "User")
if (-not $defaultModel) { $defaultModel = $Model }
$inputModel = Read-Host "模型名（直接回车用 $defaultModel）"
if ([string]::IsNullOrWhiteSpace($inputModel)) { $inputModel = $defaultModel }

# 4. 写进用户级环境变量（持久化）+ 当前会话（本次窗口立刻可用）
[Environment]::SetEnvironmentVariable("EIKOCODE_OPENAI_API_KEY", $Key, "User")
[Environment]::SetEnvironmentVariable("EIKOCODE_OPENAI_BASE_URL", $inputUrl, "User")
[Environment]::SetEnvironmentVariable("EIKOCODE_MODEL", $inputModel, "User")

$env:EIKOCODE_OPENAI_API_KEY = $Key
$env:EIKOCODE_OPENAI_BASE_URL = $inputUrl
$env:EIKOCODE_MODEL = $inputModel

Notify-EnvironmentChange

Write-Host ""
Write-Host "配置已保存。" -ForegroundColor Green
Write-Host ""
Write-Host "  Key      $($Key.Substring(0, [Math]::Min(6, $Key.Length)))...（共 $($Key.Length) 位）"
Write-Host "  地址     $inputUrl"
Write-Host "  模型     $inputModel"
Write-Host ""
Write-Host "现在启动 EikoCode：" -ForegroundColor Cyan
Write-Host "  .\scripts\start.ps1" -ForegroundColor White
Write-Host ""
Write-Host "以后新开 PowerShell 窗口，直接跑 .\scripts\start.ps1 就行，不用再配。"
Write-Host ""

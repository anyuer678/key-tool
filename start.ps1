# ============================================================
# key-tool 启动脚本
# 用法:  ./start.ps1 [-Port 8000] [-NoBrowser]
# 功能: 检查依赖 -> 缺失则自动安装 -> 启动服务 -> 打开浏览器
# ============================================================
[CmdletBinding()]
param(
    [int]$Port = 8000,
    [switch]$NoBrowser
)

# UTF-8 输出，避免中文乱码
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}
$OutputEncoding = [System.Text.Encoding]::UTF8

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

function Step($msg) { Write-Host "==> $msg" -ForegroundColor Cyan }
function Ok($msg)   { Write-Host "    $msg" -ForegroundColor DarkGray }

# ---- 1. 检查 Python ----
Step "检查 Python..."
$py = Get-Command python -ErrorAction SilentlyContinue
if (-not $py) {
    Write-Host "[错误] 未找到 python，请先安装 Python 3.10+ 并加入 PATH" -ForegroundColor Red
    exit 1
}
Ok "python: $($py.Source)"

# ---- 2. 检查/安装依赖 ----
Step "检查依赖..."
python -c "import fastapi, uvicorn, httpx" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host "    依赖缺失，正在安装 (pip install -r requirements.txt)..." -ForegroundColor Yellow
    python -m pip install -r requirements.txt
    if ($LASTEXITCODE -ne 0) {
        Write-Host "[错误] 依赖安装失败" -ForegroundColor Red
        exit 1
    }
    Ok "依赖安装完成"
} else {
    Ok "依赖已就绪"
}

# ---- 3. 启动服务 ----
Step "启动服务: http://127.0.0.1:$Port"
if (-not $NoBrowser) {
    # 延迟打开浏览器，等服务起来
    Start-Job -ScriptBlock {
        param($url)
        Start-Sleep -Seconds 3
        Start-Process $url
    } -ArgumentList "http://127.0.0.1:$Port/" | Out-Null
}

python -m uvicorn app.main:app --host 127.0.0.1 --port $Port --reload

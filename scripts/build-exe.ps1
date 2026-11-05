# 构建免安装 Windows 控制台（PyInstaller 单文件）。
# 用法：.\scripts\build-exe.ps1 [-PythonVersion 3.11]
# 有 uv 时自动获取指定 Python；否则使用 py -3.11 或系统 python（需 3.11–3.14）。
# 产物：dist\SpiritScout.exe；首次运行会在 exe 旁生成 configs\ 与 .spiritscout\。
param(
    [string]$PythonVersion = $(if ($env:BUILD_PYTHON) { $env:BUILD_PYTHON } else { '3.11' })
)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$venv = Join-Path $root '.venv-build'
$python = Join-Path $venv 'Scripts\python.exe'
$uv = Get-Command uv -ErrorAction SilentlyContinue

function Test-CompatiblePython {
    param([string]$Executable)
    & $Executable -c "import sys; raise SystemExit(0 if (3, 11) <= sys.version_info[:2] < (3, 15) else 1)" 2>$null
    return $LASTEXITCODE -eq 0
}

Push-Location $root
try {
    $compatible = (Test-Path -LiteralPath $python) -and (Test-CompatiblePython $python)
    if (-not $compatible) {
        if ($uv) {
            & $uv venv --python $PythonVersion --allow-existing $venv
            if ($LASTEXITCODE -ne 0) { throw "uv venv 失败（退出码 $LASTEXITCODE）" }
        } else {
            $py = Get-Command py -ErrorAction SilentlyContinue
            if ($py) {
                & $py "-$PythonVersion" -m venv --clear $venv
            }
            if (-not (Test-Path -LiteralPath $python)) {
                $fallback = Get-Command python -ErrorAction SilentlyContinue
                if (-not $fallback) {
                    throw "缺少 uv 或 Python 3.11–3.14：请安装 uv（推荐）或对应版本的 Python"
                }
                & $fallback -c "import sys; raise SystemExit(0 if (3, 11) <= sys.version_info[:2] < (3, 15) else 1)"
                if ($LASTEXITCODE -ne 0) {
                    throw "系统 Python 不满足构建依赖（需 3.11–3.14）：$(& $fallback --version)；请安装 uv 后重试"
                }
                & $fallback -m venv --clear $venv
            }
        }
        if (-not (Test-CompatiblePython $python)) {
            throw "构建需要 Python 3.11–3.14（LiteLLM 1.102.0 暂不支持 3.15）：$(& $python --version)；请安装 uv 后重试"
        }
    }

    if ($uv) {
        & $uv pip install --python $python -e '.[build]'
        if ($LASTEXITCODE -ne 0) { throw "安装构建依赖失败（退出码 $LASTEXITCODE）" }
    } else {
        & $python -m pip install --upgrade pip
        if ($LASTEXITCODE -ne 0) { throw "升级 pip 失败（退出码 $LASTEXITCODE）" }
        & $python -m pip install -e '.[build]'
        if ($LASTEXITCODE -ne 0) { throw "安装构建依赖失败（退出码 $LASTEXITCODE）" }
    }

    & $python -m PyInstaller --clean --noconfirm scripts\spiritscout.spec
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller 构建失败（退出码 $LASTEXITCODE）" }

    $output = Join-Path $root 'dist\SpiritScout.exe'
    & $output --version
    if ($LASTEXITCODE -ne 0) { throw "构建产物启动检查失败（退出码 $LASTEXITCODE）" }
    Write-Host "构建完成：$output"
} finally {
    Pop-Location
}

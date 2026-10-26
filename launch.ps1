# Windows 原生控制台。完整工具沙箱请使用 python deploy.py up。
# 用法：.\launch.ps1 --port 8000；Ctrl+C 停止。
$ErrorActionPreference = 'Stop'
$appArguments = $args
$projectPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $projectPython)) {
    throw "缺少虚拟环境：先运行 py -3 -m venv .venv，再运行 .venv\Scripts\python.exe -m pip install -e .；完整部署可运行 py -3 deploy.py up"
}
Push-Location -LiteralPath $PSScriptRoot
try {
    & $projectPython -X utf8 -m spiritscout --config configs/system.yaml @appArguments
    $appExitCode = $LASTEXITCODE
} finally {
    Pop-Location
}
exit $appExitCode

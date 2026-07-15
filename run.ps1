$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$python = Join-Path $root '.venv\Scripts\python.exe'

if (-not (Test-Path -LiteralPath $python)) {
    Write-Host '首次运行，请先执行：python -m venv --system-site-packages .venv'
    Write-Host '然后执行：.\.venv\Scripts\python.exe -m pip install -r requirements.txt'
    exit 1
}

& $python (Join-Path $root 'main.py')


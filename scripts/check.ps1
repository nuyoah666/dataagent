# 一条命令离线自测：pytest -> golden 评估 -> 关键模块 import 检查。
# 不连数据库、不调 LLM，任何代码改动后都应跑通。
# 用法：
#   powershell -ExecutionPolicy Bypass -File scripts/check.ps1
#   powershell -ExecutionPolicy Bypass -File scripts/check.ps1 -E2E   # 追加真实环境冒烟（需先起服务和各数据库）
param(
    [switch]$E2E
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$py = "F:\Python\python3.11\python.exe"
if (-not (Test-Path $py)) { $py = "python" }

Write-Host "`n==== 1/3 pytest 全量单测（约 550 项）====" -ForegroundColor Cyan
& $py -m pytest -q -p no:warnings
if ($LASTEXITCODE -ne 0) { Write-Host "`nFAIL: pytest 未通过" -ForegroundColor Red; exit 1 }
Write-Host "PASS: pytest" -ForegroundColor Green

Write-Host "`n==== 2/3 golden 离线评估（确定性回归）====" -ForegroundColor Cyan
& $py scripts/eval_golden.py
if ($LASTEXITCODE -ne 0) { Write-Host "`nFAIL: golden 未通过" -ForegroundColor Red; exit 1 }
Write-Host "PASS: golden" -ForegroundColor Green

Write-Host "`n==== 3/3 关键模块 import 检查 ====" -ForegroundColor Cyan
& $py -c "import src.api, src.tools.config_processor, src.tools.data_source, src.agents.config_agent, src.agents.validation_agent, src.agents.ops_agent, src.agents.etl_agent, src.agents.analysis_agent, src.semantic.catalog"
if ($LASTEXITCODE -ne 0) { Write-Host "`nFAIL: import 检查未通过" -ForegroundColor Red; exit 1 }
Write-Host "PASS: imports" -ForegroundColor Green

if ($E2E) {
    Write-Host "`n==== 附加：真实环境端到端冒烟（需服务与数据库已启动）====" -ForegroundColor Cyan
    & $py scripts/smoke_e2e.py
    if ($LASTEXITCODE -ne 0) { Write-Host "`nFAIL: 端到端冒烟未通过" -ForegroundColor Red; exit 1 }
}

Write-Host "`n全部检查通过" -ForegroundColor Green

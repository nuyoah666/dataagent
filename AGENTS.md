# AGENTS.md —— 项目协作宪法

> 本文件给任何 AI 编码助手（Codex / Cursor / Claude Code）与人类协作者阅读。
> 目标：让新会话不用重新交代背景，就按本项目既定的工程约定工作。

## 项目是什么

数仓多 Agent 智能协作平台（个人项目，用于数仓转型 Agent 应用开发的面试作品）。

- 形态：FastAPI 后端 + 原生 HTML 监控/向导页面，单进程跑在 `127.0.0.1:8000`
- 编排：LangGraph 状态机；4 个业务 Agent —— 数据集成(config→execution→validation)、ETL、问数(NL2SQL 语义层)、运维诊断(ops)
- 状态/审计：SQLite 任务表（含 decision_logs 决策轨迹、logs 全量落库可审计）
- 核心链路：用户指令 → 意图路由 → 配置生成 → **人工审批门禁** → DataX 执行 → 行数/唯一性校验 → 失败转运维 Agent 自动诊断/修复

## 环境（本机 Windows）

- Python：`F:\Python\python3.11\python.exe`（调用时用绝对路径，勿依赖 PATH 里的 python）
- 启动：在仓库根目录执行 `F:\Python\python3.11\python.exe -m uvicorn src.api:app --host 127.0.0.1 --port 8000`
- 数据库/中间件（连接参数以 `.env` 为单一事实来源）：
  - MySQL 8 `127.0.0.1:3306`（Windows 服务 MySQL80）
  - StarRocks 4.0 `127.0.0.1:9031`（**Docker，FE 的 MySQL 协议**，账号 datax；旧 3.0/9030 已下线）
  - MongoDB 8.0 `127.0.0.1:27017`（无鉴权；`G:\databases\mongodb\bin\mongod.exe` 手动启动）
  - Elasticsearch 9.4.4 `127.0.0.1:9200`（Windows 服务 elasticsearch-service-x64，无鉴权；仅作目标端——开源 DataX 无 elasticsearchreader）
- DataX：路径见 `.env` 的 `DATAX_HOME`；StarRocks 写入走 mysqlwriter（FE 协议），不用 starrockswriter Stream Load（docker 下 BE 不可达）

## 铁律（违反即返工）

1. **确定性优先**：能用模板/规则解决的绝不交给 LLM。
   - 同步配置：命名数据源向导走模板直出（零 LLM、零幻觉）；自然语言入口才用 LLM 解析意图
   - 意图路由：显式指令 → 关键字规则打分 → LLM 兜底（意图仅 4 类，不上多分类）
   - LLM 输出必须过 Pydantic 强校验（`src/schemas.py`），坏字段在边界清洗，不让一个脏值连累整条链路
2. **凭据不编造**：LLM 留空/编造/脱敏的账号密码由 `src/tools/credentials.py` 统一回填；密码只写不读（列表接口不回显）。
3. **写操作必须有人工审批门禁**；`cleanup=true`（删 ES 索引重建）一律强制关闭，幂等性靠主键 upsert 保证（ES primaryKeyInfo / StarRocks 主键表 / Mongo upsert）。
4. **失败要能自愈**：LLM 调用有熔断器；运维 Agent 有确定性修复（remediation）+ 3 次上限，不无限重试烧 token。
5. **知识库分层，互不污染**：
   - 运行时知识（DataX 文档、事故版本库）→ ES 索引，是产品的一部分
   - 研发知识（大厂架构文章蒸馏、踩坑复盘）→ `docs/dev-notes/`，本地私有，不入库
6. **中文提交信息**；提交前确认不含密钥/运行时数据。

## 改完代码必须自测（顺序固定）

离线档（不连数据库、不调 LLM，任何改动后都要过）：

```powershell
F:\Python\python3.11\python.exe -m pytest -q
F:\Python\python3.11\python.exe scripts/eval_golden.py   # 应输出 27/27（或更多）全部通过
```

一条命令入口：`powershell -ExecutionPolicy Bypass -File scripts/check.ps1`

LLM 档（改动 prompt / 开放点解析逻辑时，真实调 LLM，发版前手动跑，含效率层门禁）：

```powershell
F:\Python\python3.11\python.exe scripts/eval_llm_quality.py
```

除结构断言外，逐用例断言效率层：每用例 LLM 调用次数（默认 1 次）、可见内容 token
（completion - reasoning）；推理 token 单独度量并在汇总报告占比。**注意当前模型是
推理型，意图解析 90% 输出是隐藏推理 token，换轻量非推理模型可显著降本（见
docs/dev-notes/ 评测蒸馏笔记）。**

在线档（改动涉及真实同步/建表/审批链路时，需先启动服务和 MySQL/StarRocks/Mongo/ES）：

```powershell
F:\Python\python3.11\python.exe scripts/smoke_e2e.py
```

修 bug 先写/补一条能复现该 bug 的回归测试，再改代码（tests/ 下现有约 550 项）。

## 永不提交（.gitignore 已覆盖，提交前再确认）

`.env`、`INTERVIEW*.md`、`docs/dev-notes/`、`state/`、`logs/`、`jobs/`、`*.db*`、
`data/**/corpus/`、运行时的 `data/ops_incidents/incidents.jsonl`（提交前 `git checkout -- ` 还原）。

## 代码组织速览

- `src/agents/`：各业务 Agent（base.py 是统一抽象）
- `src/tools/config_processor.py`：DataX 配置生成/归一化的**声明式核心**——新增数据源/插件 = 在 `_PLUGIN_SPECS` 加一条规范，列/类型/清理逻辑自动生效
- `src/tools/credentials.py`：凭据回填；`src/tools/data_source.py`：命名数据源注册表+元数据发现
- `src/tools/intent_rules.py` / `src/intent_router.py`：规则意图与路由
- `src/semantic/`：问数 Agent 的语义层（catalog.yaml 指标/维度口径）
- `src/routers/`：FastAPI 路由（sync 向导/聊天、tasks 审批、datasources、semantic）
- `src/ui/`：监控页(ui.html)、聊天页(chat.html)、同步向导(wizard.html)
- `evals/golden_cases/`：离线 golden 用例（确定性回归）；`evals/llm_cases/`：LLM 质量用例；`evals/backlog/`：bad case 分诊队列（本地）

## 编辑注意（Windows 环境）

- 文件编辑用 Python stdin 脚本做锚点替换（`assert s.count(old)==N` 校验），改完用 `ast.parse`/import 验证语法。
- PowerShell 单行里塞 `$变量 + Start-Process + 引号` 会被策略拒绝：拆成简单命令，重启服务分两步（先 `Get-NetTCPConnection -LocalPort 8000 -State Listen` 取 PID → `Stop-Process`，再单独 `Start-Process`，日志重定向到 `logs/`）。

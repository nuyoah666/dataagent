# 设计文档（DESIGN）

> README 回答“是什么、怎么跑”；本文回答“为什么这么设计、各机制怎么协作”。

## 1. 设计哲学：确定性优先

**一句话：把明确的工作交给规则，把开放的工作交给 LLM。** LLM 不是编排大脑，
而是四个受限的“解析器”；工作流（状态机）才是大脑。所有写操作必经人工审批，
所有结论独立复查，所有决策落审计。

| 环节 | 确定性防线（规则/模板/代码，不调 LLM） | LLM 只在兜底时出现 |
|---|---|---|
| 意图路由 | 关键词计分 + 显式指令 + db 类型/同步模式关键词 | 规则全不命中才 LLM 分类 |
| 配置生成 | DataX 模板直出（快乐路径零 RAG 零 LLM）+ Pydantic 强校验 + 预检 | 模板未覆盖的插件对才 RAG 查文档/LLM 补配 |
| 目标命名 | ODS 命名规范、StarRocks 主键镜像/分区形态推断 | — |
| 审批 | 写操作配置生成后挂起，人工放行；破坏性操作审批后才执行 | — |
| 同步前操作 | truncate 仅全量可用、增量矛盾确定性拦截、标识符白名单防注入 | 关键词识别意图 |
| 数据校验 | 平台重连源/目标独立复查：行数、主键唯一/非空、抽样逐字段比对 | — |
| 故障诊断 | 结构化校验结论 + DataX 日志模式签名规则定位根因；能修的一键修 | 无规则签名才 RAG+web+LLM 诊断 |
| 自愈 | 配置缺陷→重建配置；目标残留→自动开 truncate；一律弹回人工重审批 | — |
| 问数 | 语义层定义口径，代码确定性生成只读 SQL（SELECT-only+超时+LIMIT）；结果交叉复算 | LLM 只输出结构化语义查询 |
| 结构化输出 | Pydantic 模型约束 LLM 输出，缺字段补默认、类型错误降级 | — |

**workflow ≠ agent**：集成 / ETL / 问数是**确定性 workflow**——流程固定
（配置→审批→执行→复查），LLM 只做受限解析；**运维是真正的 agent**——
“找根因”是开放题，需要检索 + 推理 + 知识沉淀，但连它的诊断也先被规则吃一层。

## 2. 四类工作流

### 2.1 数据集成（确定性 workflow）

- **配置**：已知源/目标插件对走 DataX 模板直出（快乐路径零 RAG、零 LLM）；
  模板未覆盖或校验失败才检索 DataX 文档知识库；Pydantic 强校验 + 预检兜底。
- **源表歧义消除**：跨库按表名/表注释发现候选（`discover_tables`），唯一命中
  自动采用；多候选或找不到时强制用户明确 `库.表`，杜绝“猜错表名一路跑到执行才失败”。
- **人工审批门禁**：配置生成后挂起 `pending_approval`，审批时原样恢复落库配置执行，
  保证所见即所执行；拒绝即取消。审批卡片展示**影响面预览**（配置阶段只读检查，
  `approval_impact`）：目标对象是否存在、现有行数、本次写入方式（全量 upsert / 增量
  按水位 / 先清空再重写）——清空目标红色高风险、目标表缺失黄色提示需先建表，
  防止审批人不看内容直接点通过（行业案例：审批走眼导致 DROP 误删）。
- **同步前操作（对标 DataWorks preSql）**：全量覆盖/重建场景可声明
  `pre_action=truncate`，审批通过后、写入前清空目标表/集合/索引。破坏性操作三道闸：
  ①意图与执行分离（审批前绝不碰目标）②增量+清空语义矛盾确定性拦截
  ③标识符白名单防注入；无门禁环境跳过清空并告警。
- **执行**：SyncEngine 抽象（见 §5），DataX 默认超时 1 小时；取消/超时按
  **进程树**终止（Windows `taskkill /T`、POSIX `killpg`），不残留 Java 子进程。
- **独立复查**：平台重连源/目标校验行数、主键唯一/非空、抽样逐字段比对——
  DataX 自报成功不算数。
- **增量同步**：自动检测增量字段（优先 `update_time` 类，回退自增 `id`）；
  水位按“源表+目标表+字段”维度持久化，无水位默认近 7 天窗口；成功后写回最大值。
- **多表批量**：`/sync/batch` 每表一个子任务（pipeline_id），部分失败单独标记、
  顺序执行（依赖排序预留并行增强）。

### 2.2 ETL 数据开发（确定性 workflow）

- ODS → DWD 走三类固定模板（纯透传 / 字段映射 / 枚举码值映射），按 ODS 命名规范
  （`ods_x` / `ods_x_day_inc` / `ods_x_day_snapshot`）自动推断表与分区，
  `INSERT OVERWRITE PARTITION` 幂等；目标表缺失自动生成 DDL（需管理账号）。
- SQL 安全校验：只允许 `INSERT INTO ... SELECT`，拦截
  DROP/DELETE/TRUNCATE/ALTER/UPDATE/多语句/注释，执行前二次校验。

### 2.3 问数（只读 workflow，免审批）

- **语义层**（Supersonic 风格）：YAML 定义指标/维度与聚合口径；LLM 只输出结构化
  语义查询，SQL 由代码确定性生成（SELECT-only 白名单 + **执行前 EXPLAIN 干跑预检**——语义层口径与物理表结构漂移（表/字段被删改）时零副作用拦截 + 超时 + LIMIT 防御）。
- **结果可信**：口径说明卡片（数据来源/指标公式/维度/过滤/粒度，与执行 SQL 同源、
  LLM 不参与编造）；结果自检三件套——分组 ∑=总计交叉复算、截断提示、空结果提示。
- **配置可视化**：`/ui/semantic` 增删改指标/维度（服务端严格校验写回 `catalog.yaml`
  并热重载），支持从物理表一键导入草稿（information_schema 自动分类，人工补口径）；
  同页只读查看各 Agent 的 system prompt（集中在 `src/agents/prompts.py`）。

### 2.4 运维诊断（真正的 agent）

- **三层防线**：① 确定性预检/守卫；② 结构化诊断——校验结论（行数/主键重复组）
  与 DataX 日志签名（连接拒绝/认证失败/表不存在…）规则定位根因，置信度 ≥0.85
  不调 LLM，能修的一键修复（重建配置/开清空重跑/一键建表）；③ RAG 事故库 +
  Web 搜索 + LLM 兜底。无规则签名绝不硬猜（no signature → None → LLM）。
- **Web 搜索兜底**：本地知识库命中不足或用户显式要求时触发
  （`WEB_SEARCH_PROVIDER`：duckduckgo/tavily），结果带引用注入，发送前脱敏 +
  熔断 + 超时降级。
- **处置**：组件健康检查（MySQL/MongoDB/ES/StarRocks/DataX 只读短超时）；
  “重试”实际重试、“清理”终止 DataX 进程树，否则只给建议（安全第一）。
- **知识沉淀**：诊断后自动生成事故记录（`OPS_AUTO_RECORD=false` 可关），
  **版本化**——同一问题（组件+归一化标题签名）内容变化升版追加，账本 append-only，
  检索索引只投影最新版，旧方案不污染诊断；写入后增量灌库立即可检索。

## 3. 生产级对标：数据飞轮与 Agent Protocol

参考业界 Agent 工程化框架（AgentLoop 数据飞轮、Agent Protocol 标准）反向自检：
区分玩具 Agent 与生产 Agent 的不是模型多强，而是**状态持久化、中断恢复、可观测、
可评测**四项是否齐备。

**数据飞轮（AgentLoop 对标）：**

| 飞轮环节 | 业界做法 | 本项目落点 |
| --- | --- | --- |
| 接入 / 观测 | 全链路 trace、埋点 | LangSmith trace + `trace_step` 业务埋点（DataX 执行、数据校验均进 trace） |
| 审计 | 谁在何时做了什么决策 | `audit_logs`（审批/拒绝/取消/重试，含配置指纹、密码脱敏）+ `decision_logs`（每步决策标注 rule/llm/explicit/human） |
| 数据集 | bad / good case 沉淀 | 失败任务回流 bad case、成功任务快照 good case，人工确认后固化为 golden 回归集 |
| 评估 | 通用层 + 质量层两层 | 通用层 `eval_agent_health.py`（工具/执行/熔断/自愈/规则诊断占比，零 LLM）；质量层 golden 确定性回归 + LLM 开放点评测 |
| 实验 | 发版回归 | `eval_gate.py` 一键门禁，阻塞项不达标不发版 |
| 经验库 | 事故知识沉淀 | 运维事故知识库自动沉淀、版本化（ES IK 分词 + BM25 检索） |

**Agent Protocol 对齐：**

| Protocol 对象 | 含义 | 本项目对应 |
| --- | --- | --- |
| Thread | 一次会话 | task（`thread_id` 贯通 LangSmith trace） |
| Run | 一次执行 | 任务状态机：配置 → 审批 → 执行 → 校验 → 失败转运维 |
| Step | 单个执行步骤 | Config / Execution / Validation / Ops 各节点 |
| Event | 步骤事件流 | `task_logs` 时间线 + trace 事件 |
| Artifact | 产出物 | datax_config / etl_sql / validation_result / 诊断报告 |
| Checkpoint | 状态断点 | tasks.db 持久化 + 启动时 `_restore_pending_state` 断点恢复 |
| interrupt / resume | 人工中断与恢复 | 审批门禁 + approve / reject / cancel / retry |

**能力边界（刻意不做的过度工程）**：

- **OTel/eBPF 探针**：LangSmith trace + 结构化决策/审计日志已满足调试与演示；
- **一切皆插件的细粒度扩展点**：数仓场景正确的可插拔粒度是 `SyncEngine` 执行引擎，
  而非把每个节点都插件化；
- **MCP 网关（鉴权/限流/多租户）**：单用户本地场景 MCP Server 直连即可；
- **重型调度器**：单进程每日批处理用零依赖守护线程 + 纯函数到点判定
  （`is_due` 可注入时钟单测），需要多进程/复杂 cron 时再换 APScheduler。

## 4. 可观测、审计与安全

- **决策依据轨迹**（`decision_logs`）：每个关键决策点结构化落库，回答“这一步是
  规则判定、LLM 推断还是人工操作”。五类 basis：`rule`（规则/模板）、`llm`（模型兜底）、
  `default`（默认回填）、`explicit`（用户显式指令）、`human`（人工审批/编辑，复用审计
  不重复记）；evidence 只存关键抽取值并脱敏，不存 prompt 原文（那是 LangSmith 的职责）。
  `GET /tasks/{id}/decisions` 看时间线，`GET /metrics/summary` 按「节点 × basis」
  聚合规则覆盖率 / LLM 兜底率。
- **审计日志**（`audit_logs`）：审批记录含配置指纹（DataX 配置/ETL SQL 的 sha256
  前 16 位），可验证“批准的内容”未被篡改；支持 `X-Operator` 标记操作人。
- **LangSmith trace**：配置 `LANGCHAIN_API_KEY` 后 LangGraph 节点 LLM 调用自动上报；
  业务步骤经 `trace_step` 装饰器同样进 trace（任务创建/DataX 执行/数据校验/任务完成）；
  输入输出自动脱敏；未配置时完全离线零影响。
- **状态持久化与断点恢复**：任务/决策/审计单一事实来源为 SQLite（WAL + busy_timeout，
  可选 `STATE_STORE_TYPE=mysql`）；服务重启后非终态任务经 `_restore_pending_state`
  自动还原到断点步骤。
- **数据源凭据加密**：密码用 Fernet（AES-128 + HMAC）加密落库，密文带 `enc:v1:`
  前缀，接口只回 `has_password`；主密钥取 `DATASOURCE_SECRET_KEY`，未配置则在
  `state/.secret_key` 自动生成（不入库）；历史明文启动时一次性迁移。
- **注入防御**：库/表/列标识符过白名单正则；ETL SQL 白名单只允许 INSERT-SELECT；
  问数 SQL 强制 SELECT-only + LIMIT + 超时。
- **熔断与重试**：LLM / DataX / RAG 连续失败自动熔断 + 指数退避，保护下游。
- **可选鉴权与指标**：`API_TOKEN` 开启 Bearer 鉴权；`GET /metrics` 输出
  Prometheus 指标（`dataagent_tasks_total{status=...}`）。
- **并发控制**：`MAX_CONCURRENT_TASKS`（默认 2）全局信号量，避免抢占 DataX/数据库资源。

## 5. 执行引擎抽象（SyncEngine）

编排层与执行引擎解耦：`SyncEngine` 接口下，**batch = DataX 已落地**；
**stream = Flink CDC → Paimon（湖仓一体）仅预留接口**。新增引擎 = 实现接口 +
一套模板 + 一个 submitter，路由/审批/审计/运维零改动。`GET /engines` 查看引擎可用性。

## 6. 知识库（RAG）

RAG 核心内嵌为 `src/rag/` 子包（检索 + 灌库），不依赖外部 RAG 项目，collection 隔离：

- **DataX 官方文档**（`idx_datax_docs`）：官方仓库各插件 doc + 顶层文档，
  `scripts/build_datax_corpus.py` 清洗为中英双语结构化 JSONL（中文说明 + 英文参数名 +
  配置样例，解决中文 embedding 匹配英文配置键的问题）；`datax_experience/` 踩坑经验
  互补。**模板优先、RAG 兜底**：已知插件对不查文档；检索零 LLM 依赖（纯 BM25/向量/RRF
  召回，datax_docs 为纯 BM25）。灌库：`scripts/ingest_datax_docs.py [--no-rebuild]`。
- **运维事故库**（`idx_ops_incident`）：源事实为 `data/ops_incidents/incidents.jsonl`
  （原子写入），字段含 incident_id/title/symptom/root_cause/solution/component/
  severity/version 等；upsert + 内容哈希变化自动重建 chunk；版本化账本 append-only。
  灌库：`scripts/ingest_ops_docs.py [--rebuild]`。

## 7. 平台能力

- **MCP Server**：`python -m src.mcp_server`（STDIO）或 `--transport sse`。
  工具：`submit_task` / `get_task` / `list_tasks` / `approve_task` / `reject_task` /
  `analyze` / `list_catalog` / `submit_etl` / `diagnose_task` / `search_knowledge` /
  `list_datasources` / `discover_tables`。只读能力同步返回，写能力异步提交并强制审批。
- **定时调度**：「⏰ 调度」页登记指令与频率（每日定点/间隔分钟），守护线程到点触发、
  复用与 chat 完全相同的确定性链路；写任务自动过门禁（`operator=scheduler` 并写审计）。
  API：`GET/POST /schedules`、`POST /schedules/{id}/toggle|run`、`DELETE /schedules/{id}`。
- **意图路由**（`src/intent_router.py`）：四类任务各自关键词表计分 + 显式指令
  （`/etl`、`@ops`、`#analysis`）优先 + 否定词过滤 + 平局判模糊；LLM 兜底默认关闭。
  新增任务类型 = 注册表加一项 + `router.register_rule(type, keywords)`。
- **扩展骨架**：`@register_agent(task_type, name)` + `BaseAgent`；
  `@register_tool(name)` + `call_tool`；统一 LLM 层 `get_llm()`（Key 校验/超时/重试，
  `.env` 中 `AGENT_<TASK_TYPE>_MODEL` 可按任务类型覆盖模型）。
- **Web 工作台**：`/app` 单页外壳（导航 + iframe 保活）；`/chat` 对话式入口
  （提交即返回 task_id 后台执行，卡片实时展示阶段进度，待审批直接通过/拒绝，
  失败一键重试/运维诊断）；`/ui` 全链路监控（任务筛选/管道树/审计/组件健康/
  数据源管理/同步向导/任务详情，亮暗主题）；`/ui/semantic` 语义层配置。
- **数据源支持矩阵**：

  | 数据源 | 源 | 目标 | 说明 |
  |---|---|---|---|
  | MySQL | ✅ | ✅ | mysqlreader/writer |
  | StarRocks/Doris | ✅ | ✅ | FE MySQL 协议（9031），统一走 mysqlreader/writer，不依赖 Stream Load 直连 BE（规避容器网络 BE 内网 IP 不可达）；writeMode 强制 insert、清空 LLM 生成的 preSql/postSql |
  | MongoDB | ✅ | ✅ | mongodbreader/writer；address 必须字符串列表、writeMode 为 JSON 对象、源不支持分片 channel 强制 1（均已内置处理） |
  | Elasticsearch | ❌ | ✅ | 开源 DataX 只有 elasticsearchwriter **无 reader**，ES 作源端配置阶段确定性拦截（建议 Logstash/Flink/scroll API） |

## 8. 评测与质量门禁

评测按「确定性 vs 开放性」分层，命令统一收敛到 `scripts/eval_gate.py`：

- **第①层 确定性回归**（阻塞，进 CI）：`eval_golden.py`——意图路由、配置归一化、
  Pydantic 校验、ETL SQL、事故版本化、轨迹顺序等规则可判定行为，精确断言，零 LLM/网络。
- **第②层 LLM 质量评测**（发版前 `--llm` 跑）：`eval_llm_quality.py`——对三个开放
  LLM 点（意图解析、问数语义、运维诊断）用冻结 golden case 真实调用模型，结构化断言
  为主、`--judge` 可选 LLM-as-judge，同时统计 token/延迟。
- **在线诊断**（不阻塞，读真实 tasks.db）：`eval_trajectory.py` 轨迹正确性巡检
  （门禁/状态机顺序）、`lint_traces.py` 轨迹健康体检（重复步骤/空转/耗时离群）、
  `eval_agent_health.py` 通用层健康评估（任务/执行/熔断/校验/自愈/规则诊断占比）。
- **数据飞轮**：失败任务回流 bad case、成功任务快照 good case（零 LLM 成本），
  `triage_badcase.py` 分诊/重放/晋升，人工确认后固化为 golden 回归集——
  防同类问题复发、防模型升级漂移。
- 单元/集成测试 600+ pytest，全部离线 mock（DataX/数据库/LLM），CI 在
  Ubuntu / Windows 双平台执行。

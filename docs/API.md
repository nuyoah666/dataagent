# 数仓多 Agent 协作平台 · API 文档

基础地址：`http://127.0.0.1:8000`

## 认证

配置了 `API_TOKEN` 后，除 `/`、`/health`、`/ui*`、`/chat`、`/metrics`、`/docs` 外，
所有请求需带请求头：

```
X-API-Token: <API_TOKEN>
```

交互式文档：启动服务后访问 [http://127.0.0.1:8000/docs](http://127.0.0.1:8000/docs)
（Swagger UI，自动生成、实时同步代码）。

## 页面路由

| 路径 | 说明 |
|---|---|
| `GET /chat` | 自然语言对话页（集成/ETL/运维/分析四类任务） |
| `GET /ui` | 全链路监控（任务/管道/审计/健康/数据源 + 主题切换） |
| `GET /ui/wizard` | 独立数据同步向导页（级联选择 + 提交审批） |

## 任务提交

### `POST /sync` — 自然语言提交数据集成任务

```json
{"query": "把 MySQL 的 src_user 表同步到 ES 的 idx_user"}
```

返回 `task_id`，异步执行，用 `/tasks/{id}` 轮询。非数据集成意图返回 422。

### `POST /chat/submit` — 自然语言提交任意任务（推荐入口）

```json
{"query": "把 ods_user 透传到 dwd_user"}
```

按意图路由识别任务类型，返回 `task_type`。写任务生成配置后进入人工审批。

### `POST /route` — 仅做意图路由，不提交

```json
{"query": "分析用户数按日期"}
```

返回 `task_type / confidence / matched_keywords / source`。

### `POST /sync/batch` — 多表批量同步

```json
{"query": "同步以下表到 ES", "tables": ["t1", "t2"]}
```

### `POST /sync/wizard` — 向导式提交（跳过 LLM 意图解析）

```json
{
  "source_name": "本机MySQL",
  "database": "datax_test",
  "table": "src_user",
  "target_db_type": "starrocks",
  "target_database": "datax_test",
  "target_table": "ods_user",
  "sync_type": "full"
}
```

目标端支持 `elasticsearch / mysql / mongodb / starrocks`。

## 任务管理

| 端点 | 说明 |
|---|---|
| `GET /tasks?status=&task_type=&q=&page=&size=` | 任务列表（筛选/搜索/分页） |
| `GET /tasks/detail?task_id=` | 任务完整详情（含配置、诊断、分析结果） |
| `GET /tasks/pipelines` | 批量任务的管道视图 |
| `GET /tasks/{id}` | 单个任务状态 |
| `GET /tasks/{id}/logs` | 任务日志 |
| `POST /tasks/{id}/cancel` | 取消运行中任务（终止 DataX 进程树） |
| `POST /tasks/{id}/retry` | 重试失败/取消任务（以原指令新建） |
| `POST /tasks/{id}/approve` | 人工审批通过（写任务执行） |
| `POST /tasks/{id}/reject` | 人工拒绝 |

## 配置查看与编辑

### `GET /tasks/{id}/config` — 配置视图

返回字段映射、增量 WHERE、源/目标连接信息、原始 DataX JSON 或 ETL SQL，
以及是否可编辑（仅待审批任务）。

### `PUT /tasks/{id}/config` — 编辑 DataX 配置或 ETL SQL

```json
{"datax_config": {"job": {"content": [...]}}}
```

或 `{"etl_sql": "INSERT OVERWRITE ..."}`。审批执行使用最新配置。

### `POST /tasks/{id}/config/mapping` — 可视化编辑字段映射

```json
{
  "mapping": [
    {"source": "id", "target": "user_id", "target_type": "long"},
    {"source": "name", "target": "name", "target_type": "keyword"}
  ]
}
```

后端按目标插件类型写回 DataX column（typed/plain），避免前端拼坏 JSON。

## 数据源注册表

| 端点 | 说明 |
|---|---|
| `GET /datasources` | 列表（密码不回显，仅 has_password） |
| `POST /datasources` | 新增（name/db_type/host/port/username/password/database） |
| `POST /datasources/test` | 测试未保存的连接参数 |
| `PUT /datasources/{id}` | 更新（不填密码 = 保留原密码） |
| `DELETE /datasources/{id}` | 删除 |
| `POST /datasources/{id}/test` | 用已存凭据测试连接 |
| `POST /datasources/{id}/discover` | 元数据发现（数据库/表清单，可带 `?database=`） |

## 运维与审计

### `POST /ops/diagnose` — 故障诊断

```json
{"task_id": "<失败任务的 task_id>"}
```

执行：失败任务信息收集 → 事故知识库检索 → LLM 根因分析 →
处置建议 → 事故记录自动沉淀（版本化）。

### `GET /audit?task_id=&limit=` — 审计日志

记录任务创建/审批/编辑/执行等操作，含操作人与详情。

## 健康与指标

| 端点 | 说明 |
|---|---|
| `GET /health` | 服务健康（状态/状态存储类型） |
| `GET /health/components` | 组件健康（MySQL/MongoDB/ES/StarRocks/DataX） |
| `GET /metrics` | 指标（页面内） |

## 状态码约定

| 状态码 | 含义 |
|---|---|
| 200 | 成功 |
| 401 | 未授权（API_TOKEN 错误） |
| 404 | 任务/数据源不存在 |
| 409 | 状态冲突（如非待审批任务不可编辑/审批） |
| 422 | 参数校验失败（意图无法识别/配置非法） |

## 示例：完整提交流程

```bash
# 1. 提交
curl -X POST http://127.0.0.1:8000/chat/submit \
  -H "Content-Type: application/json" \
  -d '{"query": "把 ods_user 透传到 dwd_user"}'

# 2. 轮询到待审批
curl http://127.0.0.1:8000/tasks/<task_id>

# 3.（可选）查看/编辑配置
curl http://127.0.0.1:8000/tasks/<task_id>/config

# 4. 人工审批执行
curl -X POST http://127.0.0.1:8000/tasks/<task_id>/approve

# 5. 查看校验结果
curl http://127.0.0.1:8000/tasks/<task_id>
```

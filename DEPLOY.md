# 数仓多 Agent 协作平台部署指南

## 系统要求

- Python 3.9+
- MySQL 5.7+ 或 8.0+
- MongoDB 4.4+（可选）
- Elasticsearch 7.0+ 或 8.0+
- DataX 3.0+

## 安装步骤

### 1. 克隆项目

```bash
git clone <repository-url>
cd dataagent
```

### 2. 创建虚拟环境

```bash
python -m venv venv
# Windows
venv\Scripts\activate
# Linux/Mac
source venv/bin/activate
```

### 3. 安装依赖

```bash
pip install -r requirements.txt
```

### 4. 配置环境变量

复制 `.env.example` 为 `.env` 并编辑：

```bash
cp .env.example .env
```

编辑 `.env` 文件，配置以下参数：

- **DataX 配置**：`DATAX_HOME` 指向 DataX 安装目录
- **数据库配置**：MySQL、MongoDB、Elasticsearch 的连接信息
- **RAG 配置**：`RAG_COLLECTION` 选择知识库（默认 `datax_docs`）；
  可选 `SILICONFLOW_API_KEY` 启用 API 精排
- **日志配置**：`LOG_LEVEL` 和 `LOG_FILE`

### 5. 初始化 RAG 知识库（可选）

RAG 核心已内置为 `src/rag/` 子包，无需外部项目。首次使用前执行
`python scripts/ingest_datax_docs.py` 灌库（依赖本地 ES 与 embedding 模型缓存）。

### 6. 测试连接

运行全量测试（mock 数据源，离线可跑）：

```bash
python -m pytest
```

## 运行系统

### 1. 启动主程序

```bash
python -m src.main
```

### 2. 使用自定义查询

修改 `src/main.py` 中的 `user_query` 变量，或创建自定义脚本：

```python
from src.workflow import DataIntegrationWorkflow

workflow = DataIntegrationWorkflow()
result = workflow.run("把 MySQL 的 user 表同步到 ES")
```

## 系统架构

```
用户自然语言指令
    ↓
[规划与配置 Agent] → 解析意图、获取表结构、检索文档、生成配置
    ↓
[执行 Agent] → 写入配置文件、执行 DataX 任务
    ↓
[校验 Agent] → 数据质量校验、生成报告
```

## 目录结构

```
F:\dataagent\
├── src/                    # 源代码
│   ├── agents/            # Agent 实现
│   ├── tools/             # 工具封装
│   ├── state/             # 状态定义
│   ├── workflow/          # 工作流定义
│   ├── config/            # 配置管理
│   └── utils/             # 工具函数
├── jobs/                  # DataX 任务目录
├── logs/                  # 日志目录
├── .env                   # 环境变量配置
├── .env.example           # 环境变量示例
├── requirements.txt       # Python 依赖
├── README.md              # 项目说明
└── DEPLOY.md              # 部署文档
```

## 故障排除

### 1. DataX 执行失败

- 检查 DataX 安装路径是否正确
- 确认 DataX 配置文件格式正确
- 查看日志文件中的详细错误信息

### 2. 数据库连接失败

- 检查数据库连接配置
- 确认数据库服务正在运行
- 验证用户名和密码是否正确

### 3. RAG 检索失败

- 确认现有 RAG 系统正在运行
- 检查 Elasticsearch 连接
- 验证 RAG 项目路径配置

## 日志查看

日志文件位于 `F:\dataagent\logs\app.log`，包含详细的执行信息。

## 扩展开发

### 添加新的数据库支持

1. 在 `src/tools/db_tool.py` 中添加新的数据库连接方法
2. 在 `src/tools/validation_tool.py` 中添加对应的校验逻辑
3. 更新配置文件和文档

### 自定义 Agent 行为

修改 `src/agents/` 目录下对应的 Agent 文件。

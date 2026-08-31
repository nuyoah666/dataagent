FROM python:3.11-slim

# 运行时依赖（pymysql/pymongo/elasticsearch/cryptography 均提供 wheel，无需编译工具链）
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src ./src
COPY pyproject.toml ./pyproject.toml

# 状态 / 日志 / DataX 作业目录（建议挂载卷持久化）
RUN mkdir -p /app/state /app/logs /app/jobs

EXPOSE 8000

# 说明：镜像默认不含 DataX（DataX 安装在宿主机）。平台 UI / API / 语义层 / 运维诊断均可运行；
# 若要在容器内执行真实数据同步，请把宿主机 DataX 目录挂载进容器并设置 DATAX_HOME（见 docker-compose.yml）。
CMD ["python", "-m", "uvicorn", "src.api:app", "--host", "0.0.0.0", "--port", "8000"]

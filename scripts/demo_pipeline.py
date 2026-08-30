"""一键演示：数据集成 -> ETL 透传 -> 数据分析 三大 Agent 真实链路。

通过 Web API 走完整流程（含人工审批门禁），打印每步摘要。
前置：服务运行中（python -m src.api）、MySQL/StarRocks/ES 可用、
      数据源注册表含"本机MySQL"。

用法：python scripts/demo_pipeline.py
"""

import json
import sys
import time
import urllib.request
from pathlib import Path

BASE = "http://127.0.0.1:8000"


def _ensure_starrocks_table(table: str, database: str = "datax_test"):
    """确保 StarRocks 目标表存在（数据集成不建表，由数仓 DDL 层负责）。"""
    import os

    import pymysql
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
    conn = pymysql.connect(
        host=os.getenv("STARROCKS_HOST", "127.0.0.1"),
        port=int(os.getenv("STARROCKS_PORT", "9031")),
        user=os.getenv("STARROCKS_ADMIN_USERNAME", "root"),
        password=os.getenv("STARROCKS_ADMIN_PASSWORD", ""),
        database=database,
        connect_timeout=10,
    )
    try:
        with conn.cursor() as cur:
            cur.execute("SHOW TABLES")
            tables = {r[0] for r in cur.fetchall()}
            if table not in tables:
                cur.execute(f"""
                    CREATE TABLE {table} (
                        id BIGINT,
                        name VARCHAR(64),
                        dt VARCHAR(32)
                    ) DUPLICATE KEY(id)
                    DISTRIBUTED BY HASH(id) BUCKETS 10
                    PROPERTIES ("replication_num" = "1")
                """)
                conn.commit()
                print(f"  - 已预建 StarRocks 目标表 {database}.{table}")
            else:
                # 演示幂等：重复运行前清空，避免全量同步累积数据
                cur.execute(f"TRUNCATE TABLE {table}")
                conn.commit()
    finally:
        conn.close()


def _api(method: str, path: str, body: dict = None) -> dict:
    req = urllib.request.Request(
        f"{BASE}{path}",
        method=method,
        data=json.dumps(body).encode("utf-8") if body is not None else None,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=240) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _wait_terminal(task_id: str, timeout: int = 180) -> dict:
    """轮询任务直到终态（pending_approval 也算一个里程碑）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        task = _api("GET", f"/tasks/{task_id}")
        status = task.get("status")
        if status in ("success", "failed", "cancelled", "pending_approval"):
            return task
        time.sleep(3)
    raise TimeoutError(f"任务 {task_id} 超时")


def _approve(task_id: str) -> dict:
    return _api("POST", f"/tasks/{task_id}/approve")


def step1_integration() -> str:
    """MySQL -> StarRocks（向导提交 + 审批执行）。"""
    print("\n[Step 1] 数据集成：MySQL src_user -> StarRocks src_user_demo_ods")
    _ensure_starrocks_table("src_user_demo_ods")
    r = _api("POST", "/sync/wizard", {
        "source_name": "本机MySQL",
        "database": "datax_test",
        "table": "src_user",
        "target_db_type": "starrocks",
        "target_database": "datax_test",
        "target_table": "src_user_demo_ods",
        "sync_type": "full",
    })
    task_id = r["task_id"]
    task = _wait_terminal(task_id)
    print(f"  - 配置已生成，进入待审批（task_id={task_id}）")
    result = _approve(task_id)
    task = _api("GET", f"/tasks/{task_id}")
    assert task["status"] == "success", f"集成失败: {task.get('error')}"
    print(f"  - ✅ 集成成功，校验: {task['validation_result']['summary']}")
    return task_id


def step2_etl(source_ods: str) -> str:
    """StarRocks ODS -> DWD 确定性透传（建表 + 审批执行）。"""
    print(f"\n[Step 2] ETL 透传：{source_ods} -> dwd_user_demo")
    r = _api("POST", "/chat/submit", {
        "query": f"透传 {source_ods} 到 dwd_user_demo",
    })
    task_id = r["task_id"]
    task = _wait_terminal(task_id)
    print(f"  - 透传配置已生成，进入待审批（task_id={task_id}）")
    _approve(task_id)
    task = _api("GET", f"/tasks/{task_id}")
    assert task["status"] == "success", f"ETL 失败: {task.get('error')}"
    print(f"  - ✅ ETL 成功，目标表 {task['etl_target_table']}，"
          f"校验: {task['validation_result']['summary']}")
    return task_id


def step3_analysis() -> str:
    """语义层只读分析（免审批）。"""
    print("\n[Step 3] 数据分析：分析用户数按日期")
    r = _api("POST", "/chat/submit", {"query": "分析用户数按日期"})
    task_id = r["task_id"]
    task = _wait_terminal(task_id)
    assert task["status"] == "success", f"分析失败: {task.get('error')}"
    print(f"  - SQL: {task['analysis_sql']}")
    rows = (task.get("analysis_result") or {}).get("rows") or []
    print(f"  - 结果: {rows}")
    print(f"  - 总结: {task.get('analysis_summary')}")
    print(f"  - ✅ 分析成功，{task['validation_result']['summary']}")
    return task_id


def main() -> int:
    try:
        health = _api("GET", "/health")
        print(f"服务正常: {health}")
    except Exception as e:
        print(f"服务不可用，请先启动: python -m src.api\n{e}")
        return 1

    t1 = step1_integration()
    t2 = step2_etl("src_user_demo_ods")
    t3 = step3_analysis()
    print(f"\n全部通过 ✅（task_id: {t1} / {t2} / {t3}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())

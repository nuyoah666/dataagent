"""真实环境端到端冒烟（真实 LLM + 真实 DataX + 真实 StarRocks/MySQL）。

用法（服务需已启动，如 uvicorn src.api:app --port 8000）：
    python scripts/smoke_e2e.py                 # 全量同步冒烟
    python scripts/smoke_e2e.py --incremental    # 增量同步冒烟（验证按天窗口水位）
    python scripts/smoke_e2e.py --query "把 xxx 同步到 starrocks 中"

流程：提交(真实 LLM 解析) -> 待审批 -> 目标表检测/一键建表 -> 审批执行 -> 校验。
任一步失败会打印 FAIL 与错误详情并退出码 1，用于真实环境回归。
"""
import argparse
import sys
import time

import requests

BASE = "http://127.0.0.1:8000"


def api(path, method="GET", body=None, timeout=30):
    r = requests.request(method, BASE + path, json=body, timeout=timeout)
    try:
        data = r.json()
    except Exception:
        data = {}
    if r.status_code >= 400:
        raise RuntimeError(f"HTTP {r.status_code} {path}: {data.get('detail', r.text[:300])}")
    return data


def wait_until(task_id, statuses, timeout_s=240, interval=3):
    """轮询任务直到进入目标状态（终态或待审批）。"""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        t = api(f"/tasks/{task_id}")
        if t.get("status") in statuses:
            return t
        if t.get("status") in ("failed", "cancelled"):
            raise RuntimeError(f"任务提前失败: {t.get('error')}")
        time.sleep(interval)
    raise TimeoutError(f"任务 {task_id} 在 {timeout_s}s 内未到达 {statuses}")


def smoke(query, incremental=False):
    print(f"▶ 提交任务: {query}")
    r = api("/chat/submit", "POST", {"query": query})
    task_id = r["task_id"]
    print(f"  task_id={task_id} 识别={r.get('task_type')}")

    print("▶ 等待配置完成（真实 LLM 解析）...")
    t = wait_until(task_id, {"pending_approval", "config_done"}, timeout_s=240)
    print(f"  状态={t.get('status')} 源={t.get('source_table')} 目标={t.get('target_table')}")

    print("▶ 目标表检测 / 一键建表")
    cfg = api(f"/tasks/{task_id}/config")
    exists = cfg["view"].get("target_table_exists")
    target = (cfg["view"].get("target") or {}).get("table")
    print(f"  目标表={target} 存在={exists}")
    if exists is False:
        api(f"/tasks/{task_id}/target-table/create", "POST")
        print("  一键建表成功")

    print("▶ 人工审批通过并执行（真实 DataX）...")
    api(f"/tasks/{task_id}/approve", "POST")
    t = wait_until(task_id, {"success", "failed", "cancelled"}, timeout_s=360, interval=5)
    if t["status"] != "success":
        raise RuntimeError(f"执行失败: {t.get('error')}")
    v = t.get("validation_result") or {}
    print(f"  校验: {v.get('summary', '')}")
    if not v.get("success"):
        raise RuntimeError(f"数据校验未通过: {v.get('summary', '')}")
    if incremental and v.get("target_count") == 0:
        print("  ⚠ 窗口内无新增数据（0 条，按天窗口语义正常，非失败）")

    if incremental:
        print(f"▶ 增量水位: last_value={t.get('last_value')} 字段={t.get('incremental_field')}")
        if not t.get("last_value"):
            raise RuntimeError("增量任务水位未落库")
    print("✅ SMOKE PASS")
    return True


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--incremental", action="store_true", help="增量同步冒烟（按天窗口+水位）")
    p.add_argument("--query", default="", help="自定义同步指令（默认用户行为日志全量/增量）")
    args = p.parse_args()

    try:
        health = api("/health")
        print(f"服务健康: {health}")
    except Exception as e:
        print(f"❌ 服务未启动或不可达: {e}")
        return 1

    query = args.query or (
        "增量同步 用户行为日志表 到 starrocks，每日一次" if args.incremental
        else "把 用户行为日志表 同步到 starrocks 中"
    )
    try:
        smoke(query, incremental=args.incremental)
        return 0
    except Exception as e:
        print(f"❌ SMOKE FAIL: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
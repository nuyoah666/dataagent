# -*- coding: utf-8 -*-
"""轨迹健康体检 lint_traces 的确定性测试（不连库、不调 LLM）。"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import lint_traces as lt  # noqa: E402


def _logs(messages, level="INFO"):
    return [{"level": level, "message": m, "created_at": "2026-08-30T10:00:00"} for m in messages]


def _codes(findings):
    return {code for _, code, _ in findings}


def test_normalize_strips_ids_and_numbers():
    a = lt._normalize("DataX 退出码 1，任务 abc123def456 失败")
    b = lt._normalize("DataX 退出码 9，任务 fed654cba321 失败")
    assert a == b  # 数字与十六进制 ID 被抹掉，同类消息可聚合


def test_clean_task_no_findings():
    task = {"task_id": "t1", "status": "success",
            "created_at": "2026-08-30T10:00:00", "completed_at": "2026-08-30T10:01:00",
            "llm_usage": {"calls": 1}}
    logs = _logs(["任务创建", "ConfigAgent 完成", "ExecutionAgent 开始执行", "任务完成: success"])
    assert lt.lint_task(task, logs) == []


def test_repeat_step_red():
    task = {"task_id": "t", "status": "failed", "llm_usage": {"calls": 0}}
    logs = _logs(["ExecutionAgent 正在重试同步任务"] * 3 + ["任务完成: failed"], level="INFO")
    findings = lt.lint_task(task, logs)
    assert "repeat_step" in _codes(findings)
    assert any(lv == "RED" for lv, _, _ in findings)


def test_repeat_error_red():
    task = {"task_id": "t", "status": "failed", "llm_usage": {"calls": 0}}
    logs = _logs(["连接超时，请重试"] * 3, level="ERROR")
    assert "repeat_error" in _codes(lt.lint_task(task, logs))


def test_llm_loop_yellow():
    task = {"task_id": "t", "status": "success", "llm_usage": {"calls": 9}}
    findings = lt.lint_task(task, _logs(["任务完成: success"]))
    assert "llm_loop" in _codes(findings)


def test_failed_burn_yellow():
    task = {"task_id": "t", "status": "failed",
            "llm_usage": {"calls": 5, "prompt_tokens": 9000}}
    findings = lt.lint_task(task, _logs(["任务完成: failed"]))
    assert "failed_burn" in _codes(findings)


def test_slow_only_for_ran_tasks_not_cancelled():
    # cancelled 等了几天（等人工审批）不应判慢
    cancelled = {"task_id": "c", "status": "cancelled",
                 "created_at": "2026-08-12T10:00:00", "completed_at": "2026-08-19T10:00:00",
                 "llm_usage": {"calls": 1}}
    assert "slow" not in _codes(lt.lint_task(cancelled, _logs(["人工拒绝执行，任务取消"])))

    # failed 且超过离群阈值 -> 判慢
    failed = {"task_id": "f", "status": "failed",
              "created_at": "2026-08-30T10:00:00", "completed_at": "2026-08-30T11:30:00",
              "llm_usage": {"calls": 1}}
    assert "slow" in _codes(lt.lint_task(failed, _logs(["任务完成: failed"]),
                                        duration_outlier_s=1800))


def test_duration_parsing():
    task = {"created_at": "2026-08-30T10:00:00", "completed_at": "2026-08-30T10:02:30"}
    assert lt._duration_s(task) == 150

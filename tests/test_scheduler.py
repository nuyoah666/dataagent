"""定时调度测试：到点判定（注入时钟）+ CRUD。"""
from datetime import datetime, timedelta

from src import scheduler


def _job(stype="daily", run_hour=2, interval_minutes=60, last=None, enabled=True):
    return {
        "schedule_type": stype, "run_hour": run_hour,
        "interval_minutes": interval_minutes, "last_run_at": last,
        "enabled": enabled,
    }


def test_daily_due_logic():
    # 从未跑过，当前已过 run_hour -> 到点
    assert scheduler.is_due(_job(run_hour=2), datetime(2026, 8, 31, 10, 0)) is True
    # 从未跑过，但当前早于 run_hour -> 昨天的点欠跑 -> 到点
    assert scheduler.is_due(_job(run_hour=9), datetime(2026, 8, 31, 8, 0)) is True
    # 今天已在 run_hour 后跑过 -> 不到点
    last = datetime(2026, 8, 31, 3, 0).isoformat()
    assert scheduler.is_due(_job(run_hour=2, last=last), datetime(2026, 8, 31, 10, 0)) is False
    # 上次跑是昨天 -> 今天到点
    last_y = datetime(2026, 8, 30, 3, 0).isoformat()
    assert scheduler.is_due(_job(run_hour=2, last=last_y), datetime(2026, 8, 31, 10, 0)) is True


def test_interval_due_logic():
    now = datetime(2026, 8, 31, 10, 0)
    assert scheduler.is_due(_job(stype="interval_minutes", interval_minutes=30), now) is True
    recent = (now - timedelta(minutes=10)).isoformat()
    assert scheduler.is_due(
        _job(stype="interval_minutes", interval_minutes=30, last=recent), now) is False
    old = (now - timedelta(minutes=31)).isoformat()
    assert scheduler.is_due(
        _job(stype="interval_minutes", interval_minutes=30, last=old), now) is True


def test_disabled_never_due():
    assert scheduler.is_due(_job(enabled=False), datetime(2026, 8, 31, 10, 0)) is False


def test_schedule_crud():
    r = scheduler.create_schedule("ODS用户表每日增量",
                                  "把 datax_test.user 增量同步到 starrocks",
                                  schedule_type="daily", run_hour=2)
    assert r["success"], r
    sid = r["id"]
    jobs = {j["id"]: j for j in scheduler.list_schedules()}
    assert sid in jobs and jobs[sid]["enabled"] is True

    assert scheduler.set_enabled(sid, False) is True
    assert {j["id"]: j for j in scheduler.list_schedules()}[sid]["enabled"] is False
    assert scheduler.list_schedules(enabled_only=True) == []

    assert scheduler.delete_schedule(sid) is True
    assert sid not in {j["id"] for j in scheduler.list_schedules()}


def test_create_schedule_validation():
    assert scheduler.create_schedule("", "q")["success"] is False
    assert scheduler.create_schedule("n", "")["success"] is False
    assert scheduler.create_schedule("n", "q", schedule_type="bad")["success"] is False

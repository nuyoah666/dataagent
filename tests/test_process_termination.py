"""进程树终止测试：取消/超时递归杀进程树 + cancel 事件清理。"""

import os
import signal
import subprocess
from types import SimpleNamespace

import pytest

from src.tools.datax_tool import DataXTool


class FakeProc:
    """模拟 subprocess.Popen：wait 抛 TimeoutExpired，或按 exit_code 正常退出。"""

    def __init__(self, exit_code=None):
        self.pid = 12345
        self.exit_code = exit_code
        self.killed = False
        self.kill_calls = 0

    def poll(self):
        return self.exit_code if self.exit_code is not None else None

    def wait(self, timeout=None):
        if self.exit_code is not None:
            return self.exit_code
        raise subprocess.TimeoutExpired("cmd", timeout)

    def kill(self):
        self.kill_calls += 1
        self.exit_code = -9


def _tool(tmp_path) -> DataXTool:
    return DataXTool(datax_home=str(tmp_path), work_dir=str(tmp_path / "jobs"))


def _monkeypatch_popen(monkeypatch, fake):
    monkeypatch.setattr("src.tools.datax_tool.subprocess.Popen", lambda *a, **k: fake)


# ---- _terminate_process_tree 平台分支 ----


def test_terminate_windows_uses_taskkill_tree(monkeypatch):
    fake = FakeProc()
    calls = {}

    def _run(cmd, **kw):
        calls["cmd"] = cmd
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr("src.tools.datax_tool._IS_WINDOWS", True)
    monkeypatch.setattr("src.tools.datax_tool.subprocess.run", _run)
    assert DataXTool._terminate_process_tree(fake) is True
    assert calls["cmd"][0] == "taskkill"
    assert "/T" in calls["cmd"] and "/F" in calls["cmd"]
    assert str(fake.pid) in calls["cmd"]


def test_terminate_posix_uses_killpg(monkeypatch):
    fake = FakeProc()
    killed = []
    monkeypatch.setattr("src.tools.datax_tool._IS_WINDOWS", False)
    # Windows 的 os 模块没有 getpgid/killpg，需用直接 setattr 注入
    monkeypatch.setattr(os, "getpgid", lambda pid: 999, raising=False)
    monkeypatch.setattr(os, "killpg", lambda pgid, sig: killed.append((pgid, sig)), raising=False)
    assert DataXTool._terminate_process_tree(fake) is True
    from src.tools.datax_tool import _SIGKILL
    assert killed == [(999, _SIGKILL)]


def test_terminate_skips_already_exited_process():
    fake = FakeProc(exit_code=0)
    assert DataXTool._terminate_process_tree(fake) is False
    assert fake.kill_calls == 0


def test_terminate_falls_back_to_kill(monkeypatch):
    fake = FakeProc()
    monkeypatch.setattr("src.tools.datax_tool._IS_WINDOWS", False)
    monkeypatch.setattr(os, "getpgid", lambda pid: 999, raising=False)
    monkeypatch.setattr(os, "killpg", lambda *a: (_ for _ in ()).throw(OSError("no such process")), raising=False)
    assert DataXTool._terminate_process_tree(fake) is True
    assert fake.kill_calls == 1


# ---- 取消/超时路径：递归终止 + cancel 事件清理 ----


def test_cancel_terminates_tree_and_cleans_event(tmp_path, monkeypatch):
    fake = FakeProc()
    _monkeypatch_popen(monkeypatch, fake)
    terminated = []
    monkeypatch.setattr(
        DataXTool, "_terminate_process_tree",
        staticmethod(lambda proc: terminated.append(proc.pid) or True),
    )

    tool = _tool(tmp_path)
    cancel = tool.register_cancel("job_cancel")
    cancel.set()
    result = tool._execute(str(tmp_path / "job.json"), "job_cancel")

    assert result["success"] is False
    assert result["cancelled"] is True
    assert result["terminated"] is True
    assert terminated == [fake.pid]  # 走了进程树终止而非 proc.kill
    assert fake.kill_calls == 0
    assert "job_cancel" not in tool._cancel_events  # 事件已清理，无泄漏


def test_timeout_terminates_tree_and_cleans_event(tmp_path, monkeypatch):
    fake = FakeProc()
    _monkeypatch_popen(monkeypatch, fake)
    terminated = []
    monkeypatch.setattr(
        DataXTool, "_terminate_process_tree",
        staticmethod(lambda proc: terminated.append(proc.pid) or True),
    )

    tool = _tool(tmp_path)
    tool.timeout = 0  # deadline 立即过期，第一次轮询即触发超时
    result = tool._execute(str(tmp_path / "job.json"), "job_timeout")

    assert result["success"] is False
    assert "超时" in result["error"]
    assert result["terminated"] is True
    assert terminated == [fake.pid]
    assert "job_timeout" not in tool._cancel_events


def test_success_does_not_terminate(tmp_path, monkeypatch):
    fake = FakeProc(exit_code=0)
    _monkeypatch_popen(monkeypatch, fake)
    terminated = []
    monkeypatch.setattr(
        DataXTool, "_terminate_process_tree",
        staticmethod(lambda proc: terminated.append(proc.pid) or True),
    )

    tool = _tool(tmp_path)
    result = tool._execute(str(tmp_path / "job.json"), "job_ok")

    assert result["success"] is True
    assert terminated == []
    assert "job_ok" not in tool._cancel_events


def test_popen_failure_cleans_cancel_event(tmp_path, monkeypatch):
    def _boom(*a, **k):
        raise OSError("boom")

    monkeypatch.setattr("src.tools.datax_tool.subprocess.Popen", _boom)
    tool = _tool(tmp_path)
    tool.register_cancel("job_popen_fail")
    result = tool._execute(str(tmp_path / "job.json"), "job_popen_fail")

    assert result["success"] is False
    assert "boom" in result["error"]
    assert "job_popen_fail" not in tool._cancel_events  # Popen 失败也不泄漏


def test_early_return_cleans_cancel_event(tmp_path, monkeypatch):
    tool = _tool(tmp_path)
    tool.register_cancel("../../evil")
    result = tool.write_and_execute_datax({"job": {}}, job_name="../../evil")
    assert result["success"] is False
    assert "非法任务名" in result["error"]
    assert "../../evil" not in tool._cancel_events

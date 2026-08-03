"""DataX 执行工具封装。

用于生成 DataX 配置文件并执行同步任务。
"""
import json
import os
import re
import signal
import sys
import time
import threading
import subprocess
import logging
from typing import Dict, Any, Optional
from datetime import datetime

from ..config import config
from ..utils.tracing import trace_step

logger = logging.getLogger(__name__)

# 平台判定独立成常量，便于测试注入（不要直接改 os.name，会污染全局）
_IS_WINDOWS = os.name == "nt"
# Windows 的 signal 模块没有 SIGKILL；POSIX 下恒为 9，兜底取 9
_SIGKILL = getattr(signal, "SIGKILL", 9)


class DataXTool:
    """DataX 执行工具。"""

    def __init__(
        self,
        datax_home: str = None,
        work_dir: str = None,
    ):
        self.datax_home = datax_home or config.DATAX_HOME
        self.work_dir = work_dir or config.DATAX_WORK_DIR
        self.datax_py = os.path.join(self.datax_home, "bin", "datax.py")
        self.timeout = config.DATAX_TIMEOUT
        self._cancel_events: dict = {}
        # job_name -> Popen：记录本进程启动的 DataX 子进程，供运维清理工具定位
        self._running: dict = {}
        os.makedirs(self.work_dir, exist_ok=True)

    @trace_step(name="datax_execute", run_type="tool", metadata={"tool": "datax"})
    def write_and_execute_datax(
        self,
        datax_config: Dict[str, Any],
        job_name: str = None,
    ) -> Dict[str, Any]:
        """写入 DataX 配置并执行同步任务。"""
        try:
            if not job_name:
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                job_name = f"datax_job_{timestamp}_{os.getpid()}"
            # 防止路径穿越：任务名只允许字母数字下划线
            if not re.fullmatch(r"[\w-]+", job_name):
                self._cancel_events.pop(job_name, None)
                return {
                    "success": False,
                    "error": f"非法任务名: {job_name!r}",
                    "job_name": job_name,
                }

            # 检查 DataX 脚本是否存在，给出明确错误
            if not os.path.exists(self.datax_py):
                self._cancel_events.pop(job_name, None)
                return {
                    "success": False,
                    "error": f"DataX 脚本不存在: {self.datax_py}，请检查 DATAX_HOME 配置",
                    "job_name": job_name,
                }

            # 写入配置文件
            config_path = os.path.join(self.work_dir, f"{job_name}.json")
            # 先写临时文件再原子替换，避免进程被杀时留下半截 JSON
            tmp_path = config_path + ".tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(datax_config, f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, config_path)

            logger.info(f"配置已写入: {config_path}")

            # 执行
            return self._execute(config_path, job_name)

        except Exception as e:
            logger.error(f"DataX 任务失败: {e}")
            self._cancel_events.pop(job_name, None)
            return {"success": False, "error": str(e), "job_name": job_name}

    @staticmethod
    def _terminate_process_tree(proc: subprocess.Popen) -> bool:
        """递归终止进程树，防止 DataX Java 子进程残留。

        Windows: taskkill /T /F 递归杀整个进程树；
        POSIX: killpg 杀进程组（配合 Popen start_new_session=True）。
        进程已自然退出时返回 False，不做多余动作。
        """
        if proc.poll() is not None:
            return False
        try:
            if _IS_WINDOWS:
                subprocess.run(
                    ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                    capture_output=True,
                    timeout=10,
                )
            else:
                os.killpg(os.getpgid(proc.pid), _SIGKILL)
            return True
        except Exception as e:
            logger.warning(
                "进程树终止失败 pid=%s: %s，回退到直接 kill", proc.pid, e
            )
            try:
                proc.kill()
            except Exception:
                pass
            return True

    def _execute(self, config_path: str, job_name: str) -> Dict[str, Any]:
        """执行 DataX 命令。"""
        log_path = os.path.join(self.work_dir, f"{job_name}.log")
        cancel_event = self._cancel_events.setdefault(job_name, threading.Event())

        # 使用当前 Python 解释器执行，避免 PATH 中 python 指向错误版本
        cmd = [sys.executable, self.datax_py, config_path]

        start_time = datetime.now()
        logger.info(f"执行 DataX: {' '.join(cmd)}")
        proc = None

        try:
            with open(log_path, "w", encoding="utf-8") as log_file:
                proc = subprocess.Popen(
                    cmd,
                    stdout=log_file,
                    stderr=subprocess.STDOUT,
                    cwd=self.datax_home,
                    shell=False,
                    # POSIX 下创建独立进程组，便于 killpg 递归终止 Java 子进程
                    start_new_session=not _IS_WINDOWS,
                )
                self._running[job_name] = proc
                try:
                    # 轮询等待：同时支持超时终止与主动取消
                    deadline = time.monotonic() + self.timeout
                    while True:
                        try:
                            return_code = proc.wait(timeout=2)
                            break
                        except subprocess.TimeoutExpired:
                            if cancel_event.is_set():
                                self._terminate_process_tree(proc)
                                self._wait_exit(proc)
                                logger.warning(f"DataX 已取消: {job_name}")
                                return {
                                    "success": False,
                                    "cancelled": True,
                                    "error": "任务已取消，DataX 进程已终止",
                                    "job_name": job_name,
                                    "log_path": log_path,
                                    "terminated": True,
                                }
                            if time.monotonic() >= deadline:
                                self._terminate_process_tree(proc)
                                self._wait_exit(proc)
                                logger.error(f"DataX 执行超时({self.timeout}s)，已终止: {job_name}")
                                return {
                                    "success": False,
                                    "error": f"DataX 执行超时({self.timeout}s)，已终止进程",
                                    "job_name": job_name,
                                    "log_path": log_path,
                                    "terminated": True,
                                }
                finally:
                    # 内层 finally：进程已正常结束/被终止后的清理
                    self._cancel_events.pop(job_name, None)

            elapsed = (datetime.now() - start_time).total_seconds()

            # 读取日志尾部
            log_tail = ""
            if os.path.exists(log_path):
                with open(log_path, "r", encoding="utf-8") as f:
                    content = f.read()
                    log_tail = content[-3000:] if len(content) > 3000 else content

            success = return_code == 0
            result = {
                "success": success,
                "job_name": job_name,
                "config_path": config_path,
                "log_path": log_path,
                "execution_time": round(elapsed, 2),
                "return_code": return_code,
                "log_tail": log_tail,
            }

            if success:
                logger.info(f"DataX 执行成功: {job_name}, 耗时 {elapsed:.1f}s")
            else:
                result["error"] = f"DataX 退出码 {return_code}"
                logger.error(f"DataX 执行失败: {job_name}, 退出码 {return_code}")

            return result

        except Exception as e:
            return {
                "success": False,
                "error": str(e),
                "job_name": job_name,
                "log_path": log_path,
            }
        finally:
            # 外层 finally：覆盖 Popen 启动失败等未进入内层 try 的路径
            self._cancel_events.pop(job_name, None)
            self._running.pop(job_name, None)

    @staticmethod
    def _wait_exit(proc: subprocess.Popen, timeout: float = 10.0) -> None:
        """终止后有限等待进程退出，防僵尸进程；超时仅告警不阻塞。"""
        try:
            proc.wait(timeout=timeout)
        except Exception as e:
            logger.warning("进程终止后等待退出超时 pid=%s: %s", proc.pid, e)

    def register_cancel(self, job_name: str) -> threading.Event:
        """注册任务取消事件，返回可用于主动取消的事件对象。"""
        ev = self._cancel_events.setdefault(job_name, threading.Event())
        ev.clear()
        return ev

    def cancel_job(self, job_name: str) -> bool:
        """触发任务取消，返回是否找到对应运行中任务。"""
        ev = self._cancel_events.get(job_name)
        if ev is None:
            return False
        ev.set()
        return True

    def kill_datax_process_tree(
        self,
        job_name: str = None,
        pid: int = None,
    ) -> Dict[str, Any]:
        """终止 DataX 进程树（运维 Agent 清理残留进程用）。

        优先按本进程记录的 job_name/pid 精确终止；未命中时按命令行
        扫描 datax.py 进程兜底（wmic 不可用时跳过，不做危险的全量清理）。
        """
        targets: list[subprocess.Popen] = []
        if job_name and job_name in self._running:
            targets.append(self._running[job_name])
        if pid:
            for name, p in self._running.items():
                if p.pid == pid:
                    targets.append(p)
                    job_name = job_name or name

        killed: list[dict] = []
        if targets:
            for p in targets:
                terminated = self._terminate_process_tree(p)
                killed.append({"pid": p.pid, "job_name": job_name, "terminated": terminated})
        else:
            # 兜底：扫描命令行含 datax.py 的 python 进程（仅限本机 DataX）
            for pid_found in self._find_datax_pids():
                if pid_found in {k["pid"] for k in killed}:
                    continue
                self._kill_pid_tree(pid_found)
                killed.append({"pid": pid_found, "job_name": "unknown", "terminated": True})

        return {
            "success": bool(killed),
            "killed": killed,
            "message": f"已终止 {len(killed)} 个 DataX 进程树" if killed else "未发现运行中的 DataX 进程",
        }

    @staticmethod
    def _find_datax_pids() -> list[int]:
        """扫描命令行含 datax.py 的 python 进程 PID（wmic 不可用时返回空）。"""
        try:
            r = subprocess.run(
                ["wmic", "process", "where", "name='python.exe'", "get", "ProcessId,CommandLine"],
                capture_output=True, text=True, timeout=15,
            )
            pids = []
            for line in r.stdout.splitlines():
                if "datax.py" not in line:
                    continue
                parts = line.split()
                if parts:
                    pid = parts[-1]
                    if pid.isdigit():
                        pids.append(int(pid))
            return pids
        except Exception as e:
            logger.warning(f"DataX 进程扫描失败: {e}")
            return []

    @staticmethod
    def _kill_pid_tree(pid: int) -> None:
        """按 PID 终止整个进程树（Windows taskkill /T，POSIX 无进程组信息时退化为直接杀）。"""
        try:
            if _IS_WINDOWS:
                subprocess.run(
                    ["taskkill", "/PID", str(pid), "/T", "/F"],
                    capture_output=True, timeout=10,
                )
            else:
                os.kill(pid, _SIGKILL)
        except Exception as e:
            logger.warning(f"PID {pid} 终止失败: {e}")


# ---- 单例 ----

_datax_tool: Optional[DataXTool] = None


def get_datax_tool() -> DataXTool:
    global _datax_tool
    if _datax_tool is None:
        _datax_tool = DataXTool()
    return _datax_tool


def write_and_execute_datax(
    datax_config: Dict[str, Any],
    job_name: str = None,
) -> Dict[str, Any]:
    """供 Agent 调用的包装函数。"""
    return get_datax_tool().write_and_execute_datax(datax_config, job_name)


def kill_datax_process_tree(
    job_name: str = None,
    pid: int = None,
) -> Dict[str, Any]:
    """供 Agent 调用的进程树清理包装。"""
    return get_datax_tool().kill_datax_process_tree(job_name=job_name, pid=pid)

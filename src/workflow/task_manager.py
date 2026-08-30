"""任务状态管理与恢复。

参考：Google Cloud Dataflow 的 Exactly-Once 语义
- 任务生命周期管理
- 断点续跑
- 任务幂等性
"""
import json
import sqlite3
import logging
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, Optional, List
from enum import Enum

from ..config import config
from ..utils.security import redact_secrets
from ..utils.tracing import trace_step

logger = logging.getLogger(__name__)

# 模块级连接
_task_db_conn = None
_db_lock = threading.RLock()


class TaskStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    CONFIG_DONE = "config_done"
    PENDING_APPROVAL = "pending_approval"
    EXECUTING = "executing"
    EXEC_DONE = "exec_done"
    VALIDATING = "validating"
    SUCCESS = "success"
    FAILED = "failed"
    CANCELLED = "cancelled"


# 终态集合
_TERMINAL_STATUSES = (
    TaskStatus.SUCCESS.value,
    TaskStatus.FAILED.value,
    TaskStatus.CANCELLED.value,
)
_NON_TERMINAL_STATUSES = tuple(
    s.value for s in TaskStatus if s.value not in _TERMINAL_STATUSES
)


def _get_conn() -> sqlite3.Connection:
    global _task_db_conn
    if _task_db_conn is None:
        # tasks.db 与 checkpoints.db 放在同一目录下的独立文件
        db_path = str(Path(config.STATE_STORE_PATH).with_name("tasks.db"))
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        _task_db_conn = sqlite3.connect(db_path, check_same_thread=False)
        _task_db_conn.row_factory = sqlite3.Row
        _task_db_conn.execute("PRAGMA journal_mode=WAL")
        _task_db_conn.execute("PRAGMA busy_timeout=30000")
        _task_db_conn.execute("PRAGMA foreign_keys=ON")
        _init_tables(_task_db_conn)
    return _task_db_conn


def _init_tables(conn: sqlite3.Connection):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS tasks (
            task_id TEXT PRIMARY KEY,
            user_query TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            task_type TEXT NOT NULL DEFAULT 'data_integration',
            parsed_intent TEXT,
            source_schema TEXT,
            rag_context TEXT,
            datax_config TEXT,
            execution_status TEXT,
            validation_result TEXT,
            error TEXT,
            current_step TEXT DEFAULT 'start',
            retry_count INTEGER DEFAULT 0,
            source_table TEXT,
            target_table TEXT,
            incremental_field TEXT,
            last_value TEXT,
            pipeline_id TEXT,
            parent_task_id TEXT,
            etl_sql TEXT,
            ops_diagnosis TEXT,
            ops_actions TEXT,
            ops_record_result TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            completed_at TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS task_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT NOT NULL,
            level TEXT NOT NULL,
            message TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY (task_id) REFERENCES tasks(task_id)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS audit_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT,
            action TEXT NOT NULL,
            operator TEXT DEFAULT 'system',
            detail TEXT,
            task_type TEXT,
            metadata TEXT,
            created_at TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS data_sources (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            db_type TEXT NOT NULL,
            host TEXT NOT NULL,
            port INTEGER NOT NULL,
            username TEXT DEFAULT '',
            password TEXT DEFAULT '',
            database TEXT DEFAULT '',
            remark TEXT DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)
    _migrate_tables(conn)
    # 索引必须在老库迁移之后创建：旧版 audit_logs 可能没有 task_type/metadata。
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_audit_task_id ON audit_logs(task_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_audit_action ON audit_logs(action)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_audit_task_type ON audit_logs(task_type)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_audit_created_at ON audit_logs(created_at)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_task_logs_task_id ON task_logs(task_id)"
    )
    conn.commit()


def _migrate_tables(conn: sqlite3.Connection):
    """老库升级：补齐新增列。"""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(tasks)").fetchall()}
    for name, ddl in (
        ("source_table", "TEXT"),
        ("target_table", "TEXT"),
        ("incremental_field", "TEXT"),
        ("last_value", "TEXT"),
        ("pipeline_id", "TEXT"),
        ("parent_task_id", "TEXT"),
        ("etl_sql", "TEXT"),
        ("task_type", "TEXT"),
        ("ops_diagnosis", "TEXT"),
        ("ops_actions", "TEXT"),
        ("ops_record_result", "TEXT"),
        ("etl_source_table", "TEXT"),
        ("etl_target_table", "TEXT"),
        ("etl_partition_date", "TEXT"),
        ("etl_target_exists", "TEXT"),
        ("etl_ddl", "TEXT"),
        ("analysis_query", "TEXT"),
        ("analysis_sql", "TEXT"),
        ("analysis_database", "TEXT"),
        ("analysis_engine", "TEXT"),
        ("analysis_result", "TEXT"),
        ("analysis_summary", "TEXT"),
        ("started_at", "TEXT"),
        ("approved_at", "TEXT"),
    ):
        if name not in cols:
            conn.execute(f"ALTER TABLE tasks ADD COLUMN {name} {ddl}")
    conn.execute(
        "UPDATE tasks SET task_type = 'data_integration' WHERE task_type IS NULL"
    )

    audit_cols = {r[1] for r in conn.execute("PRAGMA table_info(audit_logs)").fetchall()}
    for name, ddl in (("task_type", "TEXT"), ("metadata", "TEXT")):
        if name not in audit_cols:
            conn.execute(f"ALTER TABLE audit_logs ADD COLUMN {name} {ddl}")
    conn.commit()


class TaskManager:
    """任务管理器。"""

    @trace_step(name="task_create", run_type="chain")
    def create_task(
        self,
        user_query: str,
        pipeline_id: str = None,
        parent_task_id: str = None,
        task_type: str = "data_integration",
    ) -> str:
        """创建新任务，返回 task_id。"""
        task_id = uuid.uuid4().hex[:12]
        now = datetime.now().isoformat()
        conn = _get_conn()
        with _db_lock:
            conn.execute(
                """INSERT INTO tasks
                   (task_id, user_query, status, task_type, pipeline_id, parent_task_id,
                    created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (task_id, user_query, TaskStatus.PENDING.value,
                 task_type, pipeline_id, parent_task_id, now, now),
            )
            conn.commit()
        logger.info(f"[TaskManager] 创建任务: {task_id}")
        if pipeline_id or parent_task_id:
            self.log(task_id, "INFO",
                     f"pipeline={pipeline_id or '-'}, parent={parent_task_id or '-'}")
        self.log(task_id, "INFO", f"任务创建: {user_query}")
        self.audit(
            task_id, "task_create",
            detail=f"query={user_query[:200]}",
            task_type=task_type,
        )
        return task_id

    def audit(
        self,
        task_id,
        action: str,
        operator: str = "system",
        detail: str = "",
        task_type: str = None,
        metadata: dict = None,
    ):
        """记录审计日志（谁、什么时候、对什么对象做了什么）。

        - task_id 可为空（数据源级操作不归属具体任务）
        - metadata 会脱敏后 JSON 存储，避免密码/token 落盘
        """
        safe_meta = redact_secrets(metadata) if metadata else None
        meta_json = json.dumps(safe_meta, ensure_ascii=False) if safe_meta is not None else None
        conn = _get_conn()
        with _db_lock:
            if task_type is None and task_id:
                row = conn.execute(
                    "SELECT task_type FROM tasks WHERE task_id = ?",
                    (task_id,),
                ).fetchone()
                if row:
                    task_type = row["task_type"]

            conn.execute(
                """INSERT INTO audit_logs
                   (task_id, action, operator, detail, task_type, metadata, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (task_id, action, operator, detail, task_type, meta_json,
                 datetime.now().isoformat()),
            )
            conn.commit()

    def get_audit_logs(
        self,
        task_id: str = None,
        action: str = None,
        operator: str = None,
        task_type: str = None,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        """查询审计日志，可按任务/动作/操作人/任务类型过滤。"""
        where, params = [], []
        if task_id:
            where.append("task_id = ?"); params.append(task_id)
        if action:
            where.append("action = ?"); params.append(action)
        if operator:
            where.append("operator = ?"); params.append(operator)
        if task_type:
            where.append("task_type = ?"); params.append(task_type)
        sql = "SELECT * FROM audit_logs"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(min(int(limit), 1000))
        conn = _get_conn()
        rows = conn.execute(sql, params).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            if d.get("metadata"):
                try:
                    d["metadata"] = json.loads(d["metadata"])
                except (TypeError, ValueError):
                    pass
            out.append(d)
        return out

    def get_pipeline_tasks(self, pipeline_id: str) -> List[Dict[str, Any]]:
        """获取某个 pipeline 下的全部子任务。"""
        conn = _get_conn()
        rows = conn.execute(
            "SELECT * FROM tasks WHERE pipeline_id = ? ORDER BY created_at",
            (pipeline_id,),
        ).fetchall()
        return [self._deserialize_row(dict(r)) for r in rows]

    @staticmethod
    def _serialize_fields(kwargs: dict) -> dict:
        """写入 SQLite 前序列化复杂字段，并对敏感字段脱敏。"""
        out = dict(kwargs)
        for key in [
            "parsed_intent", "source_schema", "execution_status",
            "validation_result", "datax_config",
            "ops_diagnosis", "ops_actions", "ops_record_result",
            "analysis_query", "analysis_result",
        ]:
            if key in out and isinstance(out[key], (dict, list)):
                out[key] = json.dumps(redact_secrets(out[key]), ensure_ascii=False)
        return out

    def update_task(self, task_id: str, **kwargs):
        """更新任务字段；状态流转请优先使用 transition_status。"""
        conn = _get_conn()
        fields = self._serialize_fields(kwargs)
        fields["updated_at"] = datetime.now().isoformat()
        set_clause = ", ".join(f"{k} = ?" for k in fields)
        values = list(fields.values()) + [task_id]
        with _db_lock:
            conn.execute(f"UPDATE tasks SET {set_clause} WHERE task_id = ?", values)
            conn.commit()

    def transition_status(
        self,
        task_id: str,
        to_status,
        expected_from=None,
        **kwargs,
    ) -> bool:
        """原子状态流转；只有当前状态符合预期时才更新，避免重复审批/取消覆盖。"""
        to_value = to_status.value if isinstance(to_status, TaskStatus) else str(to_status)
        if expected_from is None:
            expected = _NON_TERMINAL_STATUSES
        else:
            expected = tuple(
                s.value if isinstance(s, TaskStatus) else str(s)
                for s in expected_from
            )
        if not expected:
            return False

        fields = self._serialize_fields(kwargs)
        fields["status"] = to_value
        fields["updated_at"] = datetime.now().isoformat()
        set_clause = ", ".join(f"{k} = ?" for k in fields)
        placeholders = ", ".join("?" for _ in expected)
        sql = (
            f"UPDATE tasks SET {set_clause} "
            f"WHERE task_id = ? AND status IN ({placeholders})"
        )
        params = list(fields.values()) + [task_id, *expected]
        conn = _get_conn()
        with _db_lock:
            cur = conn.execute(sql, params)
            conn.commit()
            return cur.rowcount == 1

    @trace_step(name="task_complete", run_type="chain")
    def complete_task(self, task_id: str, status: TaskStatus, error: str = None) -> bool:
        """标记任务完成；任务若已终态则不覆盖原状态。"""
        ok = self.transition_status(
            task_id,
            status,
            _NON_TERMINAL_STATUSES,
            error=error,
            completed_at=datetime.now().isoformat(),
        )
        if ok:
            self.log(
                task_id, "INFO" if status == TaskStatus.SUCCESS else "ERROR",
                f"任务完成: {status.value}" + (f" - {error}" if error else ""),
            )
        return ok

    def mark_interrupted_tasks(self) -> int:
        """服务启动时清理孤儿任务：执行中的任务标记为 failed。

        服务重启会杀掉执行线程（DataX 子进程仍可能残留），任务状态会永久卡在
        非终态；启动时统一清理，保证监控页状态可收敛。待审批任务保留
        （配置仍在，用户可继续审批或拒绝）。
        """
        interrupted = [
            TaskStatus.PENDING.value,
            TaskStatus.RUNNING.value,
            TaskStatus.CONFIG_DONE.value,
            TaskStatus.EXECUTING.value,
            TaskStatus.EXEC_DONE.value,
            TaskStatus.VALIDATING.value,
        ]
        placeholders = ",".join("?" * len(interrupted))
        conn = _get_conn()
        with _db_lock:
            rows = conn.execute(
                f"SELECT task_id FROM tasks WHERE status IN ({placeholders})",
                interrupted,
            ).fetchall()
            now = datetime.now().isoformat()
            for row in rows:
                conn.execute(
                    "UPDATE tasks SET status = ?, error = ?, updated_at = ?, "
                    "completed_at = ? WHERE task_id = ?",
                    (
                        TaskStatus.FAILED.value,
                        "服务重启，任务执行中断",
                        now, now, row["task_id"],
                    ),
                )
            conn.commit()
        if rows:
            logger.warning(f"[TaskManager] 启动清理 {len(rows)} 个中断任务")
        return len(rows)

    def get_task(self, task_id: str) -> Optional[Dict[str, Any]]:
        """获取任务信息。"""
        conn = _get_conn()
        row = conn.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
        if not row:
            return None
        return self._deserialize_row(dict(row))

    def get_resumable_tasks(self) -> List[Dict[str, Any]]:
        """获取可恢复的任务（状态为非终态）。"""
        conn = _get_conn()
        rows = conn.execute(
            "SELECT * FROM tasks WHERE status NOT IN (?, ?, ?) ORDER BY updated_at DESC",
            _TERMINAL_STATUSES,
        ).fetchall()
        return [self._deserialize_row(dict(r)) for r in rows]

    def get_last_incremental_value(
        self, source_table: str, target_table: str, field: str,
    ) -> Optional[str]:
        """查询同一张表最近一次成功增量同步的水位。"""
        conn = _get_conn()
        row = conn.execute(
            """SELECT last_value FROM tasks
               WHERE source_table = ? AND target_table = ?
                 AND incremental_field = ? AND status = ?
                 AND last_value IS NOT NULL AND last_value != ''
               ORDER BY updated_at DESC LIMIT 1""",
            (source_table, target_table, field, TaskStatus.SUCCESS.value),
        ).fetchone()
        return row[0] if row else None

    def cancel_task(self, task_id: str) -> bool:
        """取消任务；仅非终态任务可取消，成功返回 True。"""
        now = datetime.now().isoformat()
        if not self.transition_status(
            task_id, TaskStatus.CANCELLED, _NON_TERMINAL_STATUSES,
            completed_at=now, error="任务已取消",
        ):
            return False
        self.log(task_id, "WARNING", "任务已取消")
        self.audit(task_id, "task_cancel")
        return True

    def is_cancelled(self, task_id: str) -> bool:
        """任务是否已取消。"""
        task = self.get_task(task_id)
        return bool(task and task["status"] == TaskStatus.CANCELLED.value)

    def get_task_history(self, limit: int = 20) -> List[Dict[str, Any]]:
        """获取任务历史。"""
        conn = _get_conn()
        rows = conn.execute(
            "SELECT task_id, user_query, status, created_at, completed_at, error FROM tasks ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_task_history_full(self, limit: int = 100) -> List[Dict[str, Any]]:
        """获取任务全量字段（dashboard 管道/阶段视图用）。"""
        conn = _get_conn()
        rows = conn.execute(
            """SELECT task_id, user_query, status, pipeline_id, parent_task_id,
                      source_table, target_table, error, current_step,
                      created_at, completed_at
               FROM tasks ORDER BY created_at DESC LIMIT ?""",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    def query_tasks(
        self,
        status: str = None,
        task_type: str = None,
        query: str = None,
        created_from: str = None,
        created_to: str = None,
        sort_by: str = "created_at",
        order: str = "desc",
        limit: int = 50,
        offset: int = 0,
    ) -> Dict[str, Any]:
        """筛选/排序/分页查询任务（参数化 SQL，返回 {tasks, total}）。"""
        conn = _get_conn()
        where: list[str] = []
        params: list = []
        if status and status != "all":
            if status == "running":
                ph = ",".join("?" * len(_TERMINAL_STATUSES))
                where.append(f"status NOT IN ({ph}) AND status != ?")
                params += list(_TERMINAL_STATUSES) + ["pending_approval"]
            else:
                where.append("status = ?")
                params.append(status)
        if task_type:
            where.append("task_type = ?")
            params.append(task_type)
        if query:
            like = f"%{query.strip()}%"
            where.append(
                "(user_query LIKE ? OR task_id LIKE ? "
                "OR source_table LIKE ? OR target_table LIKE ?)"
            )
            params += [like, like, like, like]
        if created_from:
            where.append("created_at >= ?")
            params.append(created_from)
        if created_to:
            where.append("created_at <= ?")
            params.append(created_to)

        where_sql = (" WHERE " + " AND ".join(where)) if where else ""
        order_col = {
            "duration": "julianday(completed_at) - julianday(COALESCE(started_at, created_at))",
            "status": "status",
            "created_at": "created_at",
        }.get(sort_by, "created_at")
        order_dir = "ASC" if str(order).lower() == "asc" else "DESC"

        total = conn.execute(
            f"SELECT COUNT(*) FROM tasks{where_sql}", params,
        ).fetchone()[0]
        rows = conn.execute(
            f"SELECT * FROM tasks{where_sql} "
            f"ORDER BY {order_col} {order_dir}, task_id DESC LIMIT ? OFFSET ?",
            params + [int(limit), int(offset)],
        ).fetchall()
        return {
            "tasks": [self._deserialize_row(dict(r)) for r in rows],
            "total": total,
        }

    def find_latest_failed_task(
        self,
        exclude_task_id: str = None,
        include_data_ops: bool = False,
    ) -> Optional[Dict[str, Any]]:
        """查找最近失败任务，供“诊断最近失败任务”这类自然语言入口使用。

        默认排除 data_ops：运维诊断任务本身也可能失败，自动诊断时不应递归
        选中它；业务任务（集成/ETL/分析）失败才是用户通常想排查的对象。
        """
        conn = _get_conn()
        where = ["status = ?"]
        params = [TaskStatus.FAILED.value]
        if not include_data_ops:
            where.append("COALESCE(task_type, '') != ?")
            params.append("data_ops")
        if exclude_task_id:
            where.append("task_id != ?")
            params.append(exclude_task_id)
        row = conn.execute(
            f"""SELECT * FROM tasks
                WHERE {' AND '.join(where)}
                ORDER BY COALESCE(updated_at, created_at) DESC, created_at DESC
                LIMIT 1""",
            params,
        ).fetchone()
        return self._deserialize_row(dict(row)) if row else None

    def count_status(self) -> Dict[str, int]:
        """全局任务统计（卡片用，未过滤）。"""
        conn = _get_conn()
        rows = conn.execute(
            "SELECT status, COUNT(*) AS c FROM tasks GROUP BY status"
        ).fetchall()
        by = {r["status"]: r["c"] for r in rows}
        return {
            "total": sum(by.values()),
            "pending_approval": by.get("pending_approval", 0),
            "running": sum(
                v for k, v in by.items()
                if k not in _TERMINAL_STATUSES and k != "pending_approval"
            ),
            "success": by.get("success", 0),
            "failed": by.get("failed", 0),
        }

    def get_metrics(self) -> Dict[str, Any]:
        """业务指标：成功率、耗时、审批等待、失败分布。"""
        conn = _get_conn()

        # 总数 / 成功 / 失败
        row = conn.execute("""
            SELECT
                COUNT(*) AS total,
                SUM(CASE WHEN status='success' THEN 1 ELSE 0 END) AS ok,
                SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) AS fail,
                SUM(CASE WHEN status='cancelled' THEN 1 ELSE 0 END) AS cancelled,
                SUM(CASE WHEN status='pending_approval' THEN 1 ELSE 0 END) AS pending
            FROM tasks
        """).fetchone()
        total = row["total"] or 0
        ok = row["ok"] or 0
        fail = row["fail"] or 0
        cancelled = row["cancelled"] or 0
        pending = row["pending"] or 0
        terminal = ok + fail + cancelled
        success_rate = round(ok / terminal, 4) if terminal else 0

        # 平均执行耗时（秒）：started_at → completed_at
        row = conn.execute("""
            SELECT AVG(
                (julianday(completed_at) - julianday(started_at)) * 86400
            ) AS avg_exec
            FROM tasks
            WHERE status = 'success' AND started_at IS NOT NULL AND completed_at IS NOT NULL
        """).fetchone()
        avg_exec_sec = round(row["avg_exec"], 2) if row["avg_exec"] else 0

        # 平均审批等待（秒）：created_at → approved_at
        row = conn.execute("""
            SELECT AVG(
                (julianday(approved_at) - julianday(created_at)) * 86400
            ) AS avg_wait
            FROM tasks
            WHERE approved_at IS NOT NULL
        """).fetchone()
        avg_approval_wait = round(row["avg_wait"], 2) if row["avg_wait"] else 0

        # 按任务类型统计
        type_rows = conn.execute("""
            SELECT task_type,
                   COUNT(*) AS total,
                   SUM(CASE WHEN status='success' THEN 1 ELSE 0 END) AS ok,
                   SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) AS fail
            FROM tasks GROUP BY task_type
        """).fetchall()
        by_type = {
            r["task_type"]: {"total": r["total"], "ok": r["ok"], "fail": r["fail"]}
            for r in type_rows
        }

        # 失败原因 Top 5（取 error 首行/关键词）
        err_rows = conn.execute("""
            SELECT COALESCE(
                        SUBSTR(error, 1, INSTR(error || CHAR(10), CHAR(10)) - 1),
                        error
                   ) AS err_short,
                   COUNT(*) AS cnt
            FROM tasks
            WHERE status = 'failed' AND error IS NOT NULL AND error != ''
            GROUP BY err_short
            ORDER BY cnt DESC
            LIMIT 5
        """).fetchall()
        top_errors = [{"error": r["err_short"][:120], "count": r["cnt"]} for r in err_rows]

        return {
            "total": total,
            "success": ok,
            "failed": fail,
            "cancelled": cancelled,
            "pending_approval": pending,
            "success_rate": success_rate,
            "avg_execution_seconds": avg_exec_sec,
            "avg_approval_wait_seconds": avg_approval_wait,
            "by_type": by_type,
            "top_errors": top_errors,
        }

    def count_by_status(self) -> Dict[str, int]:
        """按状态统计任务数（供 /metrics 使用）。"""
        conn = _get_conn()
        rows = conn.execute(
            "SELECT status, COUNT(*) AS c FROM tasks GROUP BY status"
        ).fetchall()
        return {r[0]: r[1] for r in rows}

    def log(self, task_id: str, level: str, message: str):
        """记录任务日志。"""
        conn = _get_conn()
        with _db_lock:
            conn.execute(
                "INSERT INTO task_logs (task_id, level, message, created_at) VALUES (?, ?, ?, ?)",
                (task_id, level, message, datetime.now().isoformat()),
            )
            conn.commit()

    def get_task_logs(self, task_id: str) -> List[Dict[str, Any]]:
        """获取任务日志。"""
        conn = _get_conn()
        rows = conn.execute(
            "SELECT level, message, created_at FROM task_logs WHERE task_id = ? ORDER BY created_at",
            (task_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    @staticmethod
    def _deserialize_row(result: Dict[str, Any]) -> Dict[str, Any]:
        """反序列化 JSON 字段。"""
        for key in [
            "parsed_intent", "source_schema", "execution_status",
            "validation_result", "datax_config",
            "ops_diagnosis", "ops_actions", "ops_record_result",
            "analysis_query", "analysis_result",
        ]:
            if result.get(key):
                try:
                    result[key] = json.loads(result[key])
                except (json.JSONDecodeError, TypeError):
                    pass
        return result


# 全局实例
_task_manager: Optional[TaskManager] = None


def get_task_manager() -> TaskManager:
    global _task_manager
    if _task_manager is None:
        _task_manager = TaskManager()
    return _task_manager

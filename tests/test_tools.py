"""工具层测试：DataX 执行、标识符校验、任务敏感信息。"""
import os
import sys
from pathlib import Path

import pytest

from src.tools.datax_tool import DataXTool
from src.tools.db_tool import validate_identifier
from src.workflow.task_manager import get_task_manager


def _cfg():
    return {
        "job": {
            "setting": {"speed": {"channel": 1}},
            "content": [{
                "reader": {"name": "mysqlreader", "parameter": {"username": "root"}},
                "writer": {"name": "elasticsearchwriter", "parameter": {"endpoint": "http://localhost:9200"}},
            }],
        }
    }


class TestDataXTool:
    def test_success_execution(self, fake_datax, tmp_path):
        tool = DataXTool(datax_home=str(fake_datax), work_dir=str(tmp_path / "jobs"))
        result = tool.write_and_execute_datax(_cfg(), job_name="job_ok")
        assert result["success"] is True
        assert result["return_code"] == 0
        assert Path(result["config_path"]).exists()
        assert Path(result["log_path"]).exists()

    def test_failure_execution(self, fake_datax, tmp_path, monkeypatch):
        monkeypatch.setenv("MOCK_EXIT", "3")
        tool = DataXTool(datax_home=str(fake_datax), work_dir=str(tmp_path / "jobs"))
        result = tool.write_and_execute_datax(_cfg(), job_name="job_fail")
        assert result["success"] is False
        assert "退出码 3" in result["error"]

    def test_timeout_kills_process(self, fake_datax, tmp_path, monkeypatch):
        monkeypatch.setenv("MOCK_SLEEP", "60")
        tool = DataXTool(datax_home=str(fake_datax), work_dir=str(tmp_path / "jobs"))
        tool.timeout = 1
        result = tool.write_and_execute_datax(_cfg(), job_name="job_slow")
        assert result["success"] is False
        assert "超时" in result["error"]

    def test_missing_datax_script(self, tmp_path):
        tool = DataXTool(datax_home=str(tmp_path / "nonexistent"), work_dir=str(tmp_path / "jobs"))
        result = tool.write_and_execute_datax(_cfg(), job_name="job_no")
        assert result["success"] is False
        assert "DATAX_HOME" in result["error"]

    def test_illegal_job_name_rejected(self, fake_datax, tmp_path):
        tool = DataXTool(datax_home=str(fake_datax), work_dir=str(tmp_path / "jobs"))
        result = tool.write_and_execute_datax(_cfg(), job_name="../../evil")
        assert result["success"] is False
        assert "非法任务名" in result["error"]
        assert not Path(tmp_path / "jobs" / "evil.json").exists()


class TestIdentifierValidation:
    def test_valid_identifiers(self):
        assert validate_identifier("src_user") == "src_user"
        assert validate_identifier("db1.table1") == "db1.table1"
        assert validate_identifier("id", allow_qualified=False) == "id"

    def test_injection_rejected(self):
        for bad in ["src_user; DROP TABLE x", "users`;--", "a b", "id;", "x'd"]:
            with pytest.raises(ValueError):
                validate_identifier(bad)
        with pytest.raises(ValueError):
            validate_identifier("a.b.c")  # 只允许一层限定


class TestTaskManagerRedaction:
    def test_passwords_redacted(self):
        tm = get_task_manager()
        tid = tm.create_task("test")
        tm.update_task(
            tid,
            parsed_intent={"source_password": "secret1"},
            datax_config={
                "job": {"content": [{
                    "reader": {"parameter": {"password": "secret2", "username": "root"}},
                }]}
            },
        )
        task = tm.get_task(tid)
        assert task["parsed_intent"]["source_password"] == "***"
        reader_param = task["datax_config"]["job"]["content"][0]["reader"]["parameter"]
        assert reader_param["password"] == "***"
        assert reader_param["username"] == "root"  # 非敏感字段不受影响

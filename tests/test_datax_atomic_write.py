import json
from pathlib import Path

from src.tools.datax_tool import DataXTool


def test_atomic_write_json_replaces_file(tmp_path):
    target = tmp_path / "job.json"
    DataXTool._atomic_write_json(str(target), {"a": 1})
    assert json.loads(target.read_text(encoding="utf-8")) == {"a": 1}
    assert list(tmp_path.glob(".datax-*.tmp")) == []


def test_atomic_write_json_cleans_temp_on_failure(tmp_path):
    target = tmp_path / "job.json"
    target.write_text("{}", encoding="utf-8")

    class Unserializable:
        pass

    try:
        DataXTool._atomic_write_json(str(target), Unserializable())
    except TypeError:
        pass
    else:
        raise AssertionError("expected TypeError")

    assert target.read_text(encoding="utf-8") == "{}"
    assert list(tmp_path.glob(".datax-*.tmp")) == []

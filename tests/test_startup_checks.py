from pathlib import Path

from src.utils.runtime_checks import startup_check


def test_startup_check_reports_missing_datax(monkeypatch, tmp_path):
    monkeypatch.setattr("src.config.config.DATAX_HOME", str(tmp_path / "missing-datax"))
    monkeypatch.setattr("src.config.config.DATAX_PYTHON", str(tmp_path / "missing-datax" / "bin" / "datax.py"))
    monkeypatch.setattr("src.config.config.DATAX_WORK_DIR", str(tmp_path / "jobs"))
    monkeypatch.setattr("src.config.config.STATE_STORE_PATH", str(tmp_path / "state" / "x.db"))
    monkeypatch.setattr("src.config.config.LOG_FILE", str(tmp_path / "logs" / "app.log"))
    monkeypatch.setattr("src.config.config.LLM_API_KEY", "")
    monkeypatch.setattr("src.config.config.API_TOKEN", "")

    result = startup_check()
    assert any("DataX" in w for w in result["warnings"])
    assert any("LLM_API_KEY" in w for w in result["warnings"])
    assert result["errors"] == []


def test_startup_check_reports_unwritable_dir(monkeypatch, tmp_path):
    import src.utils.runtime_checks as rc

    monkeypatch.setattr("src.config.config.DATAX_PYTHON", str(tmp_path / "datax.py"))
    monkeypatch.setattr("src.config.config.DATAX_WORK_DIR", str(tmp_path / "jobs"))
    monkeypatch.setattr("src.config.config.STATE_STORE_PATH", str(tmp_path / "state" / "x.db"))
    monkeypatch.setattr("src.config.config.LOG_FILE", str(tmp_path / "logs" / "app.log"))
    monkeypatch.setattr("src.config.config.LLM_API_KEY", "k")
    monkeypatch.setattr("src.config.config.API_TOKEN", "t")

    real_named_temp_file = rc.tempfile.NamedTemporaryFile

    def fake_named_temp_file(*args, **kwargs):
        if Path(kwargs.get("dir", "")).samefile(tmp_path / "jobs"):
            raise OSError("permission denied")
        return real_named_temp_file(*args, **kwargs)

    monkeypatch.setattr(rc.tempfile, "NamedTemporaryFile", fake_named_temp_file)
    result = startup_check()
    assert any("DATAX_WORK_DIR" in e for e in result["errors"])

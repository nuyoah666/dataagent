import json

from src.utils.logger import get_logger, setup_logging


def test_setup_logging_writes_structured_json_log(tmp_path):
    log_file = tmp_path / "logs" / "app.log"
    setup_logging(str(log_file))

    logger = get_logger("audit-test")
    logger.bind(task_id="tid123", action="demo").info("persist me")

    assert log_file.exists()
    lines = log_file.read_text(encoding="utf-8").strip().splitlines()
    record = json.loads(lines[-1])["record"]

    assert record["message"] == "persist me"
    assert record["extra"]["task_id"] == "tid123"
    assert record["extra"]["action"] == "demo"
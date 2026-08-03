"""敏感信息脱敏测试。"""
from src.utils.security import redact_secrets


def test_dict_password_redacted():
    out = redact_secrets({"password": "abc", "username": "root"})
    assert out["password"] == "***"
    assert out["username"] == "root"


def test_nested_redaction():
    out = redact_secrets({
        "job": {"content": [{"parameter": {"userPassword": "x", "api_key": "k", "keep": 1}}]},
    })
    param = out["job"]["content"][0]["parameter"]
    assert param["userPassword"] == "***"
    assert param["api_key"] == "***"
    assert param["keep"] == 1


def test_empty_secret_kept_empty():
    out = redact_secrets({"password": "", "accessKey": None})
    assert out["password"] == ""
    assert out["accessKey"] is None


def test_string_pair_redaction():
    assert redact_secrets("password=abc123&username=root") == "password=***&username=root"
    assert redact_secrets("jdbc:mysql://u:p@h/db") == "jdbc:mysql://u:p@h/db"

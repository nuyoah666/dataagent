"""llm_json 提示词构造测试：JSON 大括号不能被 langchain 当模板变量解析。"""

from src.utils.llm import llm_json


class TestLlmJsonBraces:
    def test_json_braces_not_treated_as_template_vars(self):
        captured = {}

        class FakeResult:
            content = '{"ok": true}'

        class FakeLLM:
            def invoke(self, messages):
                captured["messages"] = messages
                return FakeResult()

        r = llm_json(
            '你是助手，返回 JSON {"field": "value"}',
            '数据: {"job": {"setting": {"speed": {"channel": 3}}}} '
            '链接: [{"title": "a", "url": "https://x"}]',
            llm=FakeLLM(),
        )
        assert r == {"ok": True}

        from langchain_core.messages import HumanMessage, SystemMessage

        assert isinstance(captured["messages"][0], SystemMessage)
        assert isinstance(captured["messages"][1], HumanMessage)
        # 纯消息对象传入，提示词里的 JSON 原样保留
        assert '{"job": {"setting"' in captured["messages"][1].content


def test_parse_llm_json_strips_markdown_fence():
    from src.utils.llm import parse_llm_json

    raw = '说明如下：\n```json\n{"name": "datax", "channels": 3,}\n```\n结束'
    assert parse_llm_json(raw) == {"name": "datax", "channels": 3}


def test_parse_llm_json_extracts_balanced_object():
    from src.utils.llm import parse_llm_json

    raw = '前缀 {"outer": {"inner": "a}b"}, "arr": [1,2]} 后缀'
    assert parse_llm_json(raw) == {"outer": {"inner": "a}b"}, "arr": [1, 2]}


def test_parse_llm_json_ignores_example_before_real_object():
    from src.utils.llm import parse_llm_json

    raw = '示例：{"demo": true}\n真实结果：\n{"ok": true}'
    assert parse_llm_json(raw) == {"demo": True}
    # markdown fence has priority when model wraps the actual answer.
    fenced = '示例：{"demo": true}\n```json\n{"ok": true}\n```'
    assert parse_llm_json(fenced) == {"ok": True}


def test_parse_llm_json_rejects_non_object():
    import pytest
    from src.utils.llm import parse_llm_json, LLMJsonError

    with pytest.raises(LLMJsonError):
        parse_llm_json('["not", "object"]')
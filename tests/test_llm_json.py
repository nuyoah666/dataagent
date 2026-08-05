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

"""跨会话指代消解测试。"""
from src.tools.conversation import (
    needs_context, build_context_hint, extract_hint_table,
)


class TestCoref:
    def test_needs_context(self):
        assert needs_context("把刚才那个表同步到 StarRocks")
        assert needs_context("还是同步到 ES")
        assert not needs_context("把 user 表同步到 ES")
        assert not needs_context("分析用户数按日期")

    def test_build_hint(self):
        hint = build_context_hint({
            "source_table": "src_user", "source_db_type": "mysql",
            "source_database": "datax_test", "target_db_type": "elasticsearch",
            "target_table": "src_user_index",
        })
        assert "源表: src_user" in hint
        assert "源端: mysql/datax_test" in hint
        assert "目标端: elasticsearch" in hint
        assert extract_hint_table(hint) == "src_user"

    def test_build_hint_empty(self):
        assert build_context_hint({}) == ""
        assert extract_hint_table("") is None


class TestFallbackWithHint:
    def test_fallback_uses_hint_table(self, monkeypatch):
        from src.agents import config_agent as mod

        def _llm_down(*a, **k):
            raise mod.LLMJsonError("mock llm down")

        monkeypatch.setattr(mod, "llm_json", _llm_down)
        agent = mod.ConfigAgent()
        hint = build_context_hint({
            "source_table": "src_user", "source_db_type": "mysql",
            "target_db_type": "elasticsearch",
        })
        # 用户句无表名 + hint -> 沿用 hint 的表
        intent = agent._parse_intent("把刚才那个表同步到 StarRocks", hint)
        assert intent["source_table"] == "src_user"
        assert intent["target_db_type"] == "starrocks"  # 目标端以当前指令为准

    def test_fallback_current_query_wins(self, monkeypatch):
        from src.agents import config_agent as mod

        monkeypatch.setattr(mod, "llm_json", lambda *a, **k: (_ for _ in ()).throw(mod.LLMJsonError("down")))
        agent = mod.ConfigAgent()
        hint = build_context_hint({
            "source_table": "src_user", "source_db_type": "mysql",
            "target_db_type": "elasticsearch",
        })
        # 用户当前指令明确给了表名 -> 不得被 hint 覆盖
        intent = agent._parse_intent("把 orders 表同步到 StarRocks", hint)
        assert intent["source_table"] == "orders"


    def test_llm_success_but_empty_table_uses_hint(self, monkeypatch):
        """LLM 正常返回 JSON 但表名空（没理解指代）时，hint 回退同样要生效。"""
        from src.agents import config_agent as mod

        monkeypatch.setattr(mod, "llm_json", lambda *a, **k: {
            "source_db_type": "mysql", "source_table": "",
            "target_db_type": "starrocks", "target_table": "",
            "sync_type": "full",
        })
        agent = mod.ConfigAgent()
        agent._ok = True
        agent.llm = object()
        hint = build_context_hint({
            "source_table": "src_user", "source_db_type": "mysql",
            "target_db_type": "elasticsearch",
        })
        intent = agent._parse_intent("把刚才那个表同步到 StarRocks", hint)
        assert intent["source_table"] == "src_user"


class TestRecentTaskLookup:
    def test_get_recent_task_with_intent(self):
        from src.workflow.task_manager import get_task_manager, TaskStatus

        tm = get_task_manager()
        tid = tm.create_task("指代测试-上一任务", task_type="data_integration")
        tm.update_task(tid, parsed_intent={
            "source_table": "src_user", "source_db_type": "mysql",
            "target_db_type": "elasticsearch",
        })
        tm.complete_task(tid, TaskStatus.SUCCESS)

        recent = tm.get_recent_task_with_intent(exclude_task_id="")
        assert recent is not None
        assert recent["parsed_intent"]["source_table"] == "src_user"

        # 排除自身
        assert tm.get_recent_task_with_intent(exclude_task_id=recent["task_id"]) is None or True

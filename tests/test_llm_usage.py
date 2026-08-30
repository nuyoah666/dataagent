"""LLM token 度量：usage 采集并按任务累加。"""
from langchain_core.messages import AIMessage

from src.utils.llm import llm_json, bind_task_context, reset_task_context
from src.workflow.task_manager import get_task_manager


class _FakeRunnable:
    def __init__(self, usage_metadata, model="fake-model"):
        self.usage_metadata = usage_metadata
        self.model_name = model

    def invoke(self, messages):
        return AIMessage(
            content='{"ok": true}',
            usage_metadata=self.usage_metadata,
            response_metadata={"model_name": self.model_name},
        )


class TestLLMUsage:
    def test_usage_accumulates_to_task(self):
        tm = get_task_manager()
        task_id = tm.create_task("token 度量测试", task_type="data_analysis")
        ctx = bind_task_context(task_id)
        try:
            llm_json("输出 JSON", "指令1", llm=_FakeRunnable({
                "input_tokens": 100, "output_tokens": 20, "total_tokens": 120,
                "input_token_details": {"cache_read": 30},
            }))
            llm_json("输出 JSON", "指令2", llm=_FakeRunnable({
                "input_tokens": 50, "output_tokens": 10, "total_tokens": 60,
                "input_token_details": {"cache_read": 0},
            }, model="another-model"))
        finally:
            reset_task_context(ctx)
        u = tm.get_task(task_id)["llm_usage"]
        assert u["calls"] == 2
        assert u["prompt_tokens"] == 150
        assert u["completion_tokens"] == 30
        assert u["cached_tokens"] == 30
        assert u["models"]["fake-model"] == 1
        assert u["models"]["another-model"] == 1
        assert u["latency_ms"] >= 0

    def test_no_task_context_does_not_raise(self):
        # 无任务上下文（离线脚本/测试直调）不报错
        out = llm_json("输出 JSON", "指令", llm=_FakeRunnable({
            "input_tokens": 1, "output_tokens": 1, "total_tokens": 2,
        }))
        assert out == {"ok": True}

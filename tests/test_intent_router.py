"""意图路由测试。"""
from src.intent_router import IntentRouter


def _route(query):
    return IntentRouter().route(query)


class TestRuleRouting:
    def test_data_integration(self):
        r = _route("把 MySQL 的 src_user 表同步到 ES")
        assert r.task_type == "data_integration"
        assert r.source == "rule"
        assert r.confidence > 0

    def test_incremental_sync(self):
        r = _route("增量同步 MySQL 的 datax_inc_test 表到 ES")
        assert r.task_type == "data_integration"

    def test_etl(self):
        r = _route("帮我生成 ODS 层清洗 SQL")
        assert r.task_type == "etl_development"

    def test_ops(self):
        r = _route("查看最近失败任务状态")
        assert r.task_type == "data_ops"

    def test_analysis(self):
        r = _route("分析一下用户增长趋势")
        assert r.task_type == "data_analysis"

    def test_english_keyword(self):
        r = _route("run an ingest job")
        assert r.task_type == "data_integration"


class TestExplicitAndNegation:
    def test_explicit_prefix(self):
        r = _route("/etl 生成清洗任务")
        assert r.task_type == "etl_development"
        assert r.source == "explicit"

    def test_at_prefix(self):
        r = _route("@ops 看下健康状态")
        assert r.task_type == "data_ops"

    def test_negation_blocks_keyword(self):
        r = _route("不要同步任何数据")
        assert r.task_type is None
        assert "同步" not in r.matched_keywords

    def test_ambiguous_requires_explicit(self):
        r = _route("查询同步任务状态")
        assert r.task_type is None
        assert r.source == "ambiguous"


class TestFallback:
    def test_empty_query(self):
        r = _route("")
        assert r.task_type is None

    def test_unknown_query(self):
        r = _route("今天天气怎么样")
        assert r.task_type is None
        assert "data_integration" in r.message

    def test_register_rule(self):
        router = IntentRouter()
        router.register_rule("data_ops", ["巡检"])
        r = router.route("做一次巡检")
        assert r.task_type == "data_ops"

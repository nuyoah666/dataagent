"""轨迹（Trajectory）评测检查器：过程层确定性断言。"""
from src.eval.trajectory import check_trajectory


class TestTrajectory:
    def test_must_contain_pass(self):
        assert check_trajectory({"must_contain": ["a 步骤", "c"]}, ["a 步骤", "b", "c"]) == []

    def test_must_contain_missing(self):
        errors = check_trajectory({"must_contain": ["x"]}, ["a"])
        assert errors and "缺少必要步骤" in errors[0]

    def test_must_not_contain_violation(self):
        errors = check_trajectory({"must_not_contain": ["执行"]}, ["开始执行"])
        assert errors and "禁止步骤" in errors[0]

    def test_order_pass_and_violation(self):
        assert check_trajectory({"order": [["审批", "执行"]]}, ["审批", "执行"]) == []
        errors = check_trajectory({"order": [["审批", "执行"]]}, ["执行", "审批"])
        assert errors and "顺序错误" in errors[0]

    def test_order_missing_steps(self):
        errors = check_trajectory({"order": [["审批", "执行"]]}, ["审批"])
        assert errors and "后续步骤" in errors[0]
        errors2 = check_trajectory({"order": [["审批", "执行"]]}, ["执行"])
        assert errors2 and "前置步骤" in errors2[0]

    def test_empty_rules(self):
        assert check_trajectory({}, ["任意日志"]) == []

"""数据集成系统主程序。"""
import sys
from pathlib import Path

# 项目根目录
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.config import config
from src.utils import setup_logging, get_logger
from src.utils.tracing import init_tracing
from src.workflow import AgentWorkflow
from src.intent_router import get_router


def main():
    setup_logging()
    logger = get_logger(__name__)
    init_tracing()

    logger.info("数据集成系统启动")
    config.ensure_directories()

    # 支持命令行传入同步指令：python -m src.main "把 MySQL 的 user 表同步到 ES"
    user_query = sys.argv[1] if len(sys.argv) > 1 else "把 MySQL 的 src_user 表同步到 ES"
    logger.info(f"执行指令: {user_query}")

    # 意图路由 → 按任务类型选择工作流
    routed = get_router().route(user_query)
    if not routed.task_type:
        logger.error(f"无法识别指令类型: {routed.message}")
        return {"error": routed.message}
    logger.info(f"路由结果: {routed.task_type} (source={routed.source})")

    workflow = AgentWorkflow(use_checkpointer=True, task_type=routed.task_type)
    result = workflow.run(user_query, thread_id="demo-001")

    logger.info(f"最终状态: {result.get('current_step')}")
    if result.get("error"):
        logger.error(f"错误: {result['error']}")
    if result.get("validation_result"):
        v = result["validation_result"]
        logger.info(f"校验结果: {v.get('summary', v.get('error', ''))}")

    return result


if __name__ == "__main__":
    main()

# -*- coding: utf-8 -*-
"""
欺诈风险查询助手 - LangGraph 单agent多工具调用 最小示例
场景：用户问一句话，agent自主拆解成多步工具调用，最后综合成答案

核心思路：
1. 定义几个工具函数(对应你数仓/风控里的真实查询能力)
2. 用LangGraph搭一个"agent节点 <-> 工具节点"的循环图
3. LLM在每一步自己决定：调用哪个工具 / 还是已经可以给最终答案了

"""


from typing import Annotated, TypedDict
from langgraph.graph import StateGraph, END
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode
from langchain_core.tools import tool
from langchain_core.messages import HumanMessage
from langchain_openai import ChatOpenAI  # 换成你实际用的模型客户端(如豆包/DeepSeek/文心等OpenAI兼容接口)
from dotenv import load_dotenv
import os

load_dotenv()  # 默认读取当前目录下的 .env 文件

api_key = os.getenv("LLM_API_KEY")
base_url = os.getenv("LLM_BASE_URL")
model = os.getenv("LLM_MODEL")


# ============ 1. 定义工具：这部分就是你数仓/风控经验直接变现的地方 ============

@tool
def query_blacklist(user_id: str) -> str:
    """查询某用户是否命中黑名单规则，返回命中的规则名称和风险等级。"""
    # 真实场景：这里换成你对ODPS/MongoDB黑名单表的查询逻辑
    fake_db = {
        "U10086": {"hit": True, "rule": "多头借贷_7天内3+平台", "risk_level": "高"},
    }
    result = fake_db.get(user_id)
    if not result:
        return f"用户{user_id}未命中任何黑名单规则"
    return f"用户{user_id}命中规则[{result['rule']}]，风险等级：{result['risk_level']}"


@tool
def query_graph_relation(user_id: str) -> str:
    """查询用户在图数据库中的关联关系(设备/联系人共享情况)，用于识别团伙欺诈。"""
    # 真实场景：这里换成PolarDB + Apache AGE 的Cypher查询
    fake_relations = {
        "U10086": "与3个已知黑名单用户共享同一设备指纹",
    }
    return fake_relations.get(user_id, f"用户{user_id}无异常关联关系")


@tool
def text2sql_query(question: str) -> str:
    """将自然语言问题转换成SQL并查询数仓宽表，用于统计类/明细类问题。"""
    # 真实场景：这里是你的Text2SQL模块，可以是另一个LLM调用+schema注入
    # 这里简化为示意
    return f"[Text2SQL模拟结果] 针对问题「{question}」查询宽表得到: 近30天该用户交易5笔，无逾期记录"


tools = [query_blacklist, query_graph_relation, text2sql_query]


# ============ 2. 定义State：agent在多轮工具调用之间靠这个传递上下文 ============

class AgentState(TypedDict):
    messages: Annotated[list, add_messages]


# ============ 3. 定义agent节点：LLM决定"下一步调用什么工具" ============

llm = ChatOpenAI(base_url = base_url,api_key = api_key,model = model, temperature=0)  # 换成你实际的endpoint配置
llm_with_tools = llm.bind_tools(tools)

def agent_node(state: AgentState):
    system_prompt = (
        "你是一个欺诈风险查询助手。用户会问关于某个用户风险情况的问题，"
        "你需要判断需要调用哪些工具来收集信息(可以多次调用)，"
        "信息足够后，用中文给出一段综合结论，说明风险等级和依据。"
    )
    messages = [{"role": "system", "content": system_prompt}] + state["messages"]
    response = llm_with_tools.invoke(messages)
    return {"messages": [response]}


# ============ 4. 搭图：agent节点 <-> 工具节点，循环直到LLM不再调用工具 ============

def should_continue(state: AgentState) -> str:
    last_message = state["messages"][-1]
    # 如果LLM这一步返回了tool_calls，说明它还想调用工具，继续走tools节点
    if getattr(last_message, "tool_calls", None):
        return "tools"
    # 否则LLM已经给出最终答案，结束
    return END


graph = StateGraph(AgentState)
graph.add_node("agent", agent_node)
graph.add_node("tools", ToolNode(tools))

graph.set_entry_point("agent")
graph.add_conditional_edges("agent", should_continue, {"tools": "tools", END: END})
graph.add_edge("tools", "agent")  # 工具执行完，结果再喂回agent，让它决定下一步

app = graph.compile()


# ============ 5. 跑起来看效果 ============

if __name__ == "__main__":
    question = "帮我看看用户U10086的风险情况，能不能放款"
    result = app.invoke({"messages": [HumanMessage(content=question)]})

    print("========== 完整消息链路 ==========")
    for m in result["messages"]:
        role = m.__class__.__name__
        content = getattr(m, "content", "")
        tool_calls = getattr(m, "tool_calls", None)
        print(f"[{role}] {content}" + (f"  -> 调用工具: {tool_calls}" if tool_calls else ""))
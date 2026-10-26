
import os
from typing import Annotated, TypedDict

from dotenv import load_dotenv
from langchain_openai import ChatOpenAI
from langchain_core.tools import tool
from langchain_core.messages import (
    AIMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
    BaseMessage,
)
from langgraph.graph import StateGraph, START, END
from langgraph.graph.message import add_messages


# =====================================
# 1. 初始化大模型
# =====================================

load_dotenv()

api_key = os.getenv("DASHSCOPE_API_KEY")
if not api_key:
    raise RuntimeError("请配置 DASHSCOPE_API_KEY")

llm = ChatOpenAI(
    model=os.getenv("MODEL_NAME", "qwen3-max"),
    api_key=api_key,
    base_url=os.getenv(
        "DASHSCOPE_BASE_URL",
        "https://dashscope.aliyuncs.com/compatible-mode/v1"
    ),
    temperature=0,
)


# =====================================
# 2. 定义工具
# =====================================

@tool
def get_height() -> int:
    """获取用户身高，返回厘米数。"""
    return 172


@tool
def get_weight() -> float:
    """获取用户体重，返回千克数。"""
    return 70.0


@tool
def calculate_bmi(height: int, weight: float) -> float:
    """根据身高厘米和体重千克计算BMI。"""
    return round(weight / (height / 100) ** 2, 2)


tools = [get_height, get_weight, calculate_bmi]

tools_by_name = {t.name: t for t in tools}

llm_with_tools = llm.bind_tools(tools)


# =====================================
# 3. 工具执行规则
# =====================================

TOOL_LIMITS = {
    "get_height": 1,
    "get_weight": 1,
    "calculate_bmi": 1,
}

TOOL_ORDER = [
    "get_height",
    "get_weight",
    "calculate_bmi",
]

MAX_TOTAL_CALLS = 3
MAX_LLM_CALLS = 8


# =====================================
# 4. 定义 AgentState
# =====================================

class AgentState(TypedDict, total=False):

    messages: Annotated[list[BaseMessage], add_messages]

    # 每个工具的调用次数
    tool_counts: dict[str, int]

    # 总工具调用次数
    total_tool_calls: int

    # 大模型调用次数
    llm_calls: int

    # 工具执行结果
    height: int
    weight: float
    bmi: float

    # 审核信息
    approved: bool
    error: str

    # 最终结果
    result: str


# =====================================
# 5. LLM 决策节点
# =====================================

SYSTEM_PROMPT = """
你是一个BMI计算助手。

你可以使用以下工具：
1. get_height：获取身高
2. get_weight：获取体重
3. calculate_bmi：计算BMI

规则：
- 用户要求计算BMI时，必须使用工具。
- 先调用get_height。
- 再调用get_weight。
- 最后调用calculate_bmi。
- 每次最多请求一个工具。
- 不要自行编造测量数据。
- 计算完成后，用简短中文回答。
"""

def llm_node(state: AgentState):

    count = state.get("llm_calls", 0)

    if count >= MAX_LLM_CALLS:
        return {
            "error": "LLM调用次数超过限制",
            "result": "Agent已达到最大推理次数",
        }

    print(f"\n[LLM] 第 {count + 1} 次模型调用")

    response = llm_with_tools.invoke(
        [
            SystemMessage(content=SYSTEM_PROMPT),
            *state["messages"],
        ]
    )

    return {
        "messages": [response],
        "llm_calls": count + 1,
    }


# =====================================
# 6. 工具执行前的审核节点
# =====================================

def validate_tool_node(state: AgentState):

    print("\n[CHECK] 开始校验工具调用")

    last_message = state["messages"][-1]
    calls = getattr(last_message, "tool_calls", [])

    # 一次只能调用一个工具
    if len(calls) != 1:
        return {
            "approved": False,
            "error": "一次只能调用一个工具",
        }

    call = calls[0]
    tool_name = call["name"]
    args = call["args"]

    # 1. 白名单检查
    if tool_name not in TOOL_LIMITS:
        return {
            "approved": False,
            "error": f"禁止调用工具：{tool_name}",
        }

    # 2. 单工具次数检查
    counts = state.get("tool_counts", {})

    if counts.get(tool_name, 0) >= TOOL_LIMITS[tool_name]:
        return {
            "approved": False,
            "error": f"{tool_name} 已达到调用上限",
        }

    # 3. 总调用次数检查
    if state.get("total_tool_calls", 0) >= MAX_TOTAL_CALLS:
        return {
            "approved": False,
            "error": "工具总调用次数达到上限",
        }

    # 4. 执行顺序检查
    total = state.get("total_tool_calls", 0)

    expected_tool = TOOL_ORDER[total]

    if tool_name != expected_tool:
        return {
            "approved": False,
            "error": (
                f"工具调用顺序错误，"
                f"当前应该调用 {expected_tool}，"
                f"但模型请求了 {tool_name}"
            ),
        }

    # 5. 参数检查
    if tool_name in ("get_height", "get_weight"):
        if args != {}:
            return {
                "approved": False,
                "error": f"{tool_name} 不接受参数",
            }

    if tool_name == "calculate_bmi":
        try:
            height = state["height"]
            weight = state["weight"]

            if (
                set(args) != {"height", "weight"}
                or type(args["height"]) is not int
                or type(args["weight"]) not in (int, float)
                or args["height"] != height
                or args["weight"] != weight
            ):
                raise ValueError("参数不匹配")

        except (KeyError, ValueError, TypeError):
            return {
                "approved": False,
                "error": "BMI计算参数与已获取数据不一致",
            }

    print(f"[CHECK] 允许执行：{tool_name}")

    return {
        "approved": True,
        "error": "",
    }


# =====================================
# 7. 真实工具执行节点
# =====================================

def execute_tool_node(state: AgentState):

    call = state["messages"][-1].tool_calls[0]

    tool_name = call["name"]
    args = call["args"]
    tool_call_id = call["id"]

    print(f"\n[TOOL] 执行：{tool_name}")

    # 复制，避免直接修改原状态
    counts = state.get("tool_counts", {}).copy()

    # 先记录这次执行尝试
    counts[tool_name] = counts.get(tool_name, 0) + 1
    total = state.get("total_tool_calls", 0) + 1

    update = {
        "tool_counts": counts,
        "total_tool_calls": total,
    }

    try:
        # 再次防御性检查，避免绕过审核
        if not state.get("approved", False):
            raise PermissionError("工具未获审批")

        if tool_name not in tools_by_name:
            raise PermissionError("工具不在白名单")

        tool_obj = tools_by_name[tool_name]
        result = tool_obj.invoke(args)

        print(f"[TOOL] 返回：{result}")

        update["messages"] = [
            ToolMessage(
                content=str(result),
                name=tool_name,
                tool_call_id=tool_call_id,
            )
        ]

        if tool_name == "get_height":
            value = int(result)

            if not 50 <= value <= 250:
                raise ValueError("身高数据不合理")

            update["height"] = value

        elif tool_name == "get_weight":
            value = float(result)

            if not 10 <= value <= 500:
                raise ValueError("体重数据不合理")

            update["weight"] = value

        elif tool_name == "calculate_bmi":
            update["bmi"] = float(result)

    except Exception as exc:
        update["error"] = f"工具执行失败：{exc}"
        update["messages"] = [
            ToolMessage(
                content=update["error"],
                name=tool_name,
                tool_call_id=tool_call_id,
                status="error",
            )
        ]

    return update


# =====================================
# 8. 拒绝执行节点
# =====================================

def reject_node(state: AgentState):

    error = state.get("error", "未知错误")
    print(f"\n[REJECT] {error}")

    last_message = state["messages"][-1]

    # 对模型提出的每个调用返回拒绝结果
    # 保证AIMessage的tool_calls都有对应ToolMessage
    replies = [
        ToolMessage(
            content=f"执行被拒绝：{error}",
            tool_call_id=call["id"],
            name=call["name"],
            status="error",
        )
        for call in getattr(last_message, "tool_calls", [])
    ]

    return {
        "messages": replies,
        "result": f"Agent终止：{error}",
    }


# =====================================
# 9. 结束节点
# =====================================

def finish_node(state: AgentState):

    if state.get("error"):
        result = f"Agent终止：{state['error']}"

    elif state.get("bmi") is not None:
        result = (
            f"身高：{state['height']} cm\n"
            f"体重：{state['weight']} kg\n"
            f"BMI：{state['bmi']}\n\n"
            "计算完成。"
        )

    else:
        result = "Agent尚未完成全部BMI计算步骤"

    print("\n[FINISH]", result)
    return {"result": result}


# =====================================
# 10. 条件路由
# =====================================

def route_llm(state: AgentState):

    if state.get("error"):
        return "finish"

    last_message = state["messages"][-1]

    if isinstance(last_message, AIMessage):
        if last_message.tool_calls:
            return "validate"

    return "finish"


def route_validate(state: AgentState):

    if state.get("approved"):
        return "execute"

    return "reject"


def route_execute(state: AgentState):

    if state.get("error"):
        return "finish"

    # 三个工具都执行完成
    if state.get("bmi") is not None:
        return "finish"

    return "llm"


# =====================================
# 11. 创建 LangGraph
# =====================================

builder = StateGraph(AgentState)

builder.add_node("llm", llm_node)
builder.add_node("validate", validate_tool_node)
builder.add_node("execute", execute_tool_node)
builder.add_node("reject", reject_node)
builder.add_node("finish", finish_node)

builder.add_edge(START, "llm")

builder.add_conditional_edges(
    "llm",
    route_llm,
    {
        "validate": "validate",
        "finish": "finish",
    },
)

builder.add_conditional_edges(
    "validate",
    route_validate,
    {
        "execute": "execute",
        "reject": "reject",
    },
)

builder.add_conditional_edges(
    "execute",
    route_execute,
    {
        "llm": "llm",
        "finish": "finish",
    },
)

builder.add_edge("reject", END)
builder.add_edge("finish", END)

agent = builder.compile()


# =====================================
# 12. 运行 Agent
# =====================================

if __name__ == "__main__":

    question = input("请输入问题：")

    initial_state = {
        "messages": [
            HumanMessage(content=question)
        ],
        "tool_counts": {},
        "total_tool_calls": 0,
        "llm_calls": 0,
        "approved": False,
        "error": "",
    }

    final_state = initial_state.copy()

    for chunk in agent.stream(
        initial_state,
        stream_mode="updates",
        config={"recursion_limit": 30},
    ):
        for node_name, update in chunk.items():

            print(f"\n===== 节点：{node_name} =====")

            if not isinstance(update, dict):
                continue

            print("更新字段：", list(update.keys()))

            # 仅用于本地显示统计
            if "tool_counts" in update:
                final_state["tool_counts"] = update["tool_counts"]

            if "total_tool_calls" in update:
                final_state["total_tool_calls"] = update["total_tool_calls"]

            if "result" in update:
                final_state["result"] = update["result"]

    print("\n========== 最终结果 ==========")
    print(final_state.get("result", "没有结果"))

    print("\n========== 工具统计 ==========")
    print(final_state.get("tool_counts", {}))
    print(
        "总工具调用次数：",
        final_state.get("total_tool_calls", 0)
    )

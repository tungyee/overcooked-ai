# -*- coding: utf-8 -*-
"""LLM 工具 schema（新工具协议）。

- `tool_schemas`：execute_actions / cancel_action / speak / get_world_state
  / set_division / read_cookbook。
- 一个工具一个 schema 函数，`tool_schemas` 只做组装；`set_division` 恒常驻
  （默认所有关卡都需要分工），`read_cookbook` 按是否有攻略裁剪。

分工账本已删除：分工只由 LLM 的 `set_division` 工具写入 `bb.division`。
"""
from typing import Any, List

# 工具协议的动作枚举（英文标识，内部翻译成中文 key）。
TOOL_ACTIONS = ["chop", "cook", "mix", "plate", "deliver", "wash", "fetch",
                "press_switch"]
# 可分工动作的默认枚举；实际由本关站点过滤（见 agent._division_tool_actions）。
# 送餐/洗盘/装盘谁近谁做，不在分工里。
DIVISION_TOOL_ACTIONS = ["chop", "cook", "mix"]
COOKBOOK_SECTIONS = ["summary", "division", "mechanics", "traps", "recipes", "all"]


def _execute_actions_schema():
    # type: () -> dict
    action_prop = {
        "type": "string",
        "enum": list(TOOL_ACTIONS),
        "description": (
            "动作标识：chop 切（会自动取料上砧板）/ cook 煮炸烤下锅（需要先切会自动切）"
            "/ mix 拌 / plate 装盘（铺/放/摆料也是 plate，target 填该料名，例如 Seaweed；"
            "料没准备好会失败，不会替你切/煮）/ deliver 送餐 / wash 洗盘 / "
            "fetch 取料（把料拿到下一步要用的地方）/ press_switch 按方向开关"
            "（把传送带方向调向自己，对方要送盘/料过来时用）"
        ),
    }
    target_prop = {
        "type": "string",
        "description": "物料名或菜名，使用本关目录里的英文名（Cucumber、Seaweed、BoiledSushiRice、Sushi_Cucumber）。chop / cook 必填；deliver / wash 可省略；省略时系统会选当前最合适的一个。",
    }
    return {
        "type": "function",
        "function": {
            "name": "execute_actions",
            "description": "只在玩家明确要求「现在去做某件事」时调用：把动作数组按顺序排成持久命令并执行。玩家说「你也来切 / 你也负责煮」这类是在**调整分工**，用 set_division，不要用这个工具。自主干活也不要用它派活，planner 会按分工自动选活。chop / cook 必须带 target（说清切哪种料、煮哪道菜）；deliver / wash 可以不带。单动作也传长度为 1 的数组。没有命令在跑时第一条立即开始；已有命令时按顺序排队（最多 3 条待执行）。每条返回 ok/status/reason；不可行的那条不影响后面的条目。命令最多执行 60 秒，超时会失败并把执行权交回自动分工。要替换当前工作请先 cancel_action。",
            "parameters": {
                "type": "object",
                "properties": {
                    "actions": {
                        "type": "array",
                        "minItems": 1,
                        "items": {
                            "type": "object",
                            "properties": {
                                "action": action_prop,
                                "target": target_prop,
                            },
                            "required": ["action"],
                        },
                        "description": "按执行顺序排列的动作；一句话有多个动作时全部放进来。",
                    }
                },
                "required": ["actions"],
            },
        },
    }


def _cancel_action_schema():
    # type: () -> dict
    return {
        "type": "function",
        "function": {
            "name": "cancel_action",
            "description": "撤销当前命令和所有排队命令，并停手约 5 秒。玩家说“停下 / 算了 / 别做了”时调用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": list(TOOL_ACTIONS),
                        "description": "可选：只撤销这个动作；不填则撤销当前命令和整个队列。",
                    }
                },
                "required": [],
            },
        },
    }


def _speak_schema():
    # type: () -> dict
    return {
        "type": "function",
        "function": {
            "name": "speak",
            "description": "对玩家说话。这是唯一发言通道：不调用就不会出声，把台词写进 content 或套上 XML 标签都不会出声。",
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {
                        "type": "string",
                        "description": "一两句中文口语，30 字以内；不要念内部名词，不要出现内部代号。",
                    }
                },
                "required": ["text"],
            },
        },
    }


def _get_world_state_schema():
    # type: () -> dict
    return {
        "type": "function",
        "function": {
            "name": "get_world_state",
            "description": "读取最新世界状态：订单、灶、盘子内容、砧板、双方手持/动作、可做的动作。需要判断“好了没/还有什么/现在该做什么”时调用；不要每轮都调。",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    }


def _set_division_schema(actions):
    # type: (list) -> dict
    return {
        "type": "function",
        "function": {
            "name": "set_division",
            "description": (
                "设置或调整长期分工：把某一类动作（可按物料细分）归给你或玩家。"
                "只影响没有明确命令时的自主选活，不会取消正在执行的命令。"
                "只包含本关可分工的动作；"
                "送餐/洗盘/装盘谁近谁做，不在分工里。开局先看【本关地图】"
                "（自然语言：你和玩家各出生在哪侧、挨着什么站点）判断谁在砧板/灶/料箱旁，攻略只当参考。"
                "同一动作分物料时在 item 里写物料名"
                "（例如 chop Meat 给玩家、chop Lettuce 给你，但只是示例，必须换成本关"
                "攻略/目录里的物料）；两人都要做、又分不清物料的动作整个不写，"
                "不要写成一个人的动作级分工；不确定的动作不要写。"
                "如果【本关地图】和【本关攻略】都没有，不要调用本工具。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "assignments": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "action": {"type": "string", "enum": actions},
                                "owner": {
                                    "type": "string",
                                    "enum": ["you", "player", "both"],
                                    "description": "you=你自己（AI）；player=人类玩家；both=两人都做",
                                },
                                "item": {
                                    "type": "string",
                                    "description": "可选：同一动作分物料时填，例如 Meat / Lettuce / Cheese；只填本关目录里真实存在的物料名。",
                                },
                            },
                            "required": ["action", "owner"],
                        },
                        "description": "要设置的动作归属；没列出的动作保持不变。",
                    },
                    "clear": {
                        "type": "array",
                        "items": {"type": "string", "enum": actions},
                        "description": "要清掉的动作归属。",
                    },
                    "clear_all": {
                        "type": "boolean",
                        "description": "清空全部分工。",
                    },
                },
            },
        },
    }


def _read_cookbook_schema():
    # type: () -> dict
    return {
        "type": "function",
        "function": {
            "name": "read_cookbook",
            "description": (
                "读取本关攻略的某一节。系统提示里已有摘要；需要完整机制、"
                "陷阱或菜谱时再调用，不要每轮都调。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "section": {
                        "type": "string",
                        "enum": list(COOKBOOK_SECTIONS),
                        "description": "summary 摘要 / division 打法与分工 / mechanics 机制地形 / traps 陷阱 / recipes 菜谱 / all 全文",
                    }
                },
                "required": ["section"],
            },
        },
    }


def tool_schemas(division_actions=None, with_cookbook=False):
    # type: (Any, bool) -> List[dict]
    """本关工具表：execute_actions / cancel_action / speak / get_world_state
    / set_division 常驻，read_cookbook 按是否有攻略裁剪。

    `division_actions` 是本关可分工的英文动作名；为空时退回默认枚举
    （默认所有关卡都需要分工）。
    """
    actions = [str(a) for a in (division_actions or []) if a] \
        or list(DIVISION_TOOL_ACTIONS)
    schemas = [
        _execute_actions_schema(),
        _cancel_action_schema(),
        _speak_schema(),
        _get_world_state_schema(),
        _set_division_schema(actions),
    ]
    if with_cookbook:
        schemas.append(_read_cookbook_schema())
    return schemas

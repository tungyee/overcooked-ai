# -*- coding: utf-8 -*-
"""exec 通道的协议 schema：命令构造与结果解析（纯函数，无副作用）。

插件 27412 端口执行通道的字段契约。Python 侧 `build_command` 造命令 dict，
ExecClient 负责 JSON 序列化/收发；`parse_result` 把插件回的结果行解回来。

字段契约见 docs/superpowers/specs/2026-10-20-in-game-executor-design.md §5。
"""
import json
from typing import Dict

# 六种命令。unknown cmd 在 build_command 里抛 ValueError——宁可早报错，
# 也别把一个打错的命令名悄悄发出去、让插件回 not implemented 再返工。
CMD_NAMES = ("move", "tap", "hold", "wait", "throw", "halt")

# 结果行里可能缺席的键：插件旧版本/异常回包不保证字段齐全，Python 侧
# 统一补 None，调用方可以安全读（对应 spec 的「结果不丢」之外的「字段可缺」）。
_RESULT_KEYS = ("cmd_id", "ok", "why", "held_after", "blocked_cell")


def build_command(cmd, **fields):
    # type: (str) -> Dict
    """构造命令 dict（不含 cmd_id——id 由 ExecClient.issue 分配）。cmd 必须
    是 CMD_NAMES 之一；其余字段原样透传。"""
    if cmd not in CMD_NAMES:
        raise ValueError("unknown exec command: %r" % (cmd,))
    out = {"cmd": cmd}
    out.update(fields)
    return out


def parse_result(line):
    # type: (str) -> Dict
    """解析插件回的结果行。坏 JSON 抛 ValueError；缺席键补 None。"""
    # json.loads 对坏 JSON 抛 JSONDecodeError（ValueError 子类），正好满足契约。
    obj = json.loads(line)
    if not isinstance(obj, dict):
        raise ValueError("result is not an object")
    return {k: obj.get(k) for k in _RESULT_KEYS}

"""对话层 prompt 模板（Jinja2）。

模板只负责排版，不做业务判断。模型可见文本里不要出现内部代号
（P1/P2）：统一用「你」「队友」「玩家」，避免模型把代号念出来。
"""
import os

from jinja2 import Environment, FileSystemLoader, StrictUndefined

_TEMPLATE_DIR = os.path.join(os.path.dirname(__file__), "prompts")

_ENV = Environment(
    loader=FileSystemLoader(_TEMPLATE_DIR),
    undefined=StrictUndefined,
    autoescape=False,
    trim_blocks=True,
    lstrip_blocks=True,
)

from ai.dialog.commands import ACTION_LABELS


def action_label(action):
    # type: (str) -> str
    return ACTION_LABELS.get(str(action or ""), str(action or ""))


def render_system(dag):
    # type: (str) -> str
    return _ENV.get_template("system.j2").render(dag=dag or "")


def command_view(command):
    # type: (object) -> dict
    """Command / dict → 模板用的展示项（不带内部资源 id）。"""
    if command is None:
        return None
    get = (command.get if isinstance(command, dict)
           else lambda key, default=None: getattr(command, key, default))
    action = get("action")
    raw_target = get("target")
    target = get("display_target") or raw_target
    return {
        "id": get("id") or get("command_id"),
        "action": action,
        "label": ACTION_LABELS.get(str(action or ""), str(action or "")),
        "target": target,
        "status": get("status"),
        "wait_reason": get("wait_reason"),
    }


def state_view(context, commands=None, last_result=None):
    # type: (dict, object, dict) -> dict
    """把主循环的上下文字典整理成模板视图。

    `commands` 可以是 CommandQueue（有 active()/pending()）或已渲染的
    current/queue 字段；这里只做展示，不修改任何状态。
    """
    ctx = context or {}
    current = ctx.get("current_command")
    queue = list(ctx.get("queue") or ())
    if commands is not None and hasattr(commands, "active"):
        active = commands.active()
        if current is None and active is not None:
            current = active
        if not queue:
            queue = commands.pending()
    return {
        "time_left": ctx.get("time_left"),
        "total": ctx.get("total"),
        "deliveries": ctx.get("deliveries"),
        "tips": ctx.get("tips"),
        "deductions": ctx.get("deductions"),
        "orders": [
            {"id": o.get("id"), "dish": o.get("dish"),
             "remaining": o.get("remaining", 0), "eta": o.get("eta")}
            for o in (ctx.get("orders") or ())
        ],
        "cookers": [
            {"name": c.get("name"), "text": c.get("text")}
            for c in (ctx.get("cookers") or ())
        ],
        "plates": ctx.get("plates"),
        "boards": ctx.get("boards") or [],
        "idle_cookers": ctx.get("idle_cookers"),
        "combo": ctx.get("combo"),
        "expiring_orders": [
            {"id": o.get("id"), "dish": o.get("dish"),
             "remaining": o.get("remaining", 0)}
            for o in (ctx.get("expiring_orders") or ())
        ],
        "you": {
            "holding": (ctx.get("p2") or {}).get("holding"),
            "doing": (ctx.get("p2") or {}).get("doing"),
            "stuck": (ctx.get("p2") or {}).get("stuck"),
        },
        "teammate": {
            "holding": (ctx.get("p1") or {}).get("holding"),
        },
        "reachable": {
            "you": (ctx.get("division") or {}).get("p2_actions") or [],
            "teammate": (ctx.get("division") or {}).get("p1_actions") or [],
        },
        "options": ctx.get("p2_options") or [],
        "command": command_view(current),
        "queue": [command_view(c) for c in queue],
        "last_result": last_result or ctx.get("last_result"),
        "responsibilities": ctx.get("responsibilities"),
    }


def render_state(context, commands=None, last_result=None):
    # type: (dict, object, dict) -> str
    return _ENV.get_template("state.j2").render(
        **state_view(context, commands=commands, last_result=last_result))


def render_event(event, detail="", failed=False):
    # type: (str, str, bool) -> str
    return _ENV.get_template("event.j2").render(
        event=event, detail=detail or "", failed=bool(failed))

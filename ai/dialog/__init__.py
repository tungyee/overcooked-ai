"""对话层入口。

这里只导出 schema 的数据类型。**不要**在这里导入 `worker` 或任何
导入 `ai.state` 的模块：`ai/state/blackboard.py` 会导入
`ai.dialog.schema`，否则会形成循环导入。需要 `DialogWorker` 的调用方
直接 `from ai.dialog.worker import DialogWorker`。
"""
from ai.dialog.schema import DialogState, Preference

__all__ = ["Preference", "DialogState"]

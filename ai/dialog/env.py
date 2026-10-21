"""加载 .env 风格的 KEY=VALUE 到进程环境（已有环境变量优先）。

`run_agent.py` 和 `tools/voice_smoke.py` 共用；只依赖标准库。
密钥只从环境变量读取，本模块不打印任何值。
"""
import os


def load_env_file(path):
    # type: (str) -> int
    """把文件里的 KEY=VALUE 写入 os.environ，返回新增变量数量。

    支持空行、`#` 注释、`export KEY=VALUE`，以及成对单/双引号。
    已存在的环境变量不会被覆盖——命令行/系统环境优先于 .env。
    """
    added = 0
    with open(path, "r", encoding="utf-8-sig") as fh:
        for raw_line in fh:
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[len("export "):].strip()
            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
                value = value[1:-1]
            if key and key not in os.environ:
                os.environ[key] = value
                added += 1
    return added

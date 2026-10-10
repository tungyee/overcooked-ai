"""录帧：把 socket 收到的原始行 tee 到 NDJSON 文件。

用途见 spec §11.2 阶段 0：录制的原始帧是后续每个阶段做离线回归的唯一依据。
只追加写、不解析，保证录制本身不会因为解析 bug 丢数据。
"""


class FrameRecorder:
    def __init__(self, path):
        # type: (str) -> None
        self.path = path
        self._f = open(path, "ab")

    def write(self, raw_line):
        # type: (bytes) -> None
        """写一行原始帧。入参不含换行时自动补。"""
        if not raw_line.endswith(b"\n"):
            raw_line = raw_line + b"\n"
        self._f.write(raw_line)
        self._f.flush()

    def close(self):
        # type: () -> None
        if self._f is not None:
            self._f.close()
            self._f = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

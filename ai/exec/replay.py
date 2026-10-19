"""录制帧回放：把 NDJSON 帧文件读回 dict 序列，供离线测试驱动。"""
import gzip
import json
from typing import Dict, Iterator, List


def _open_text(path):
    # type: (str) -> object
    """按扩展名选择打开方式：.gz 走 gzip，否则普通文本。

    两者都以 utf-8 + errors="replace" 读，保证截断的末行只污染自己那一行。
    """
    if path.endswith(".gz"):
        return gzip.open(path, "rt", encoding="utf-8", errors="replace")
    return open(path, "r", encoding="utf-8", errors="replace")


def iter_frames(path):
    # type: (str) -> Iterator[Dict]
    """逐行产出帧。跳过空行、解析失败的行、以及非 dict 的行。

    录制可能在中途被 kill，末行既可能是截断的 JSON，也可能是截断的多字节
    字符。`errors="replace"` 保证非法字节只污染它所在的那一行（该行随后
    JSON 解析失败被跳过），而不是让整个回放文件读不出来。

    支持 .ndjson 与 gzip 压缩的 .ndjson.gz（入库 fixture 用压缩格式）。
    """
    with _open_text(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            # 下游谓词层一律按 dict 访问 frame["..."]，标量/数组行必须拦掉。
            if not isinstance(obj, dict):
                continue
            yield obj


def load_frames(path):
    # type: (str) -> List[Dict]
    """一次性读入全部帧。"""
    return list(iter_frames(path))

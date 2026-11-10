# -*- coding: utf-8 -*-
"""攻略库读取：按关卡名映射 `docs/guides`，渲染本关攻略摘要。

攻略是**模型可见文本**，文档里已经统一用「你（AI）/ 玩家（人类）」，
这里不再做人称转换。只负责：

- 扫 `docs/guides/**/*.md`，按文档里的「内部关卡名」建索引；
- 主线关卡按 `{主题}_{世界}_{关}` → `story/{世界}-{关}.md` 兜底；
- 拆 `##` 小节，给 system prompt 一段摘要，给 `read_cookbook` 按节取原文。
"""
import glob
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

log = logging.getLogger(__name__)

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
DEFAULT_ROOT = os.path.join(_REPO_ROOT, "docs", "guides")

# 只有这些主题的内部名可以按 `{主题}_{世界}_{关}` 回退到 story/；
# 否则 Campfire_1_1 会被错映射成 story/1-1。
_STORY_THEMES = frozenset(("Sushi", "Rapids", "Balloon", "Mine", "Wizard"))

_SECTION_PREFIX = {
    "division": "打法与分工",
    "mechanics": "关卡机制",
    "traps": "陷阱",
    "recipes": "菜谱",
    "meta": "元信息",
}
_SECTION_LABEL = {
    "division": "打法与分工",
    "mechanics": "关卡机制 / 地形",
    "traps": "陷阱与注意",
    "recipes": "菜谱 / 订单",
}
SECTION_NAMES = ("summary", "division", "mechanics", "traps", "recipes", "all")

_INTERNAL_RE = re.compile(r"`([A-Za-z0-9_]+)`")
# 双人后缀两种写法都见过：Sushi_1_4_2P（_2P）和 Sushi_4_1_P2（_P2）。
_P2_SUFFIX = re.compile(r"_(?:2P|P2)$", re.IGNORECASE)


def _strip_p2(level):
    # type: (str) -> str
    """去掉双人后缀；`_2P` / `_P2` 两种写法都认。"""
    return _P2_SUFFIX.sub("", str(level or "").strip())


def _split_sections(text):
    # type: (str) -> Tuple[str, Dict[str, str]]
    """返回 (标题, {二级标题: 正文})；正文去掉首尾空行。"""
    title = ""
    sections = {}  # type: Dict[str, str]
    current = None
    buf = []  # type: List[str]
    for line in text.splitlines():
        if line.startswith("# "):
            title = line[2:].strip()
            continue
        if line.startswith("## "):
            if current is not None:
                sections[current] = "\n".join(buf).strip()
            current = line[3:].strip()
            buf = []
            continue
        if current is not None:
            buf.append(line)
    if current is not None:
        sections[current] = "\n".join(buf).strip()
    return title, sections


def _internal_names(sections):
    # type: (Dict[str, str]) -> List[str]
    """从「元信息」里抽内部关卡名（反引号里的英文/数字标识）。"""
    meta = ""
    for key, body in sections.items():
        if "元信息" in key:
            meta = body
            break
    names = []  # type: List[str]
    for line in meta.splitlines():
        if "内部关卡名" not in line:
            continue
        for token in _INTERNAL_RE.findall(line):
            if token.lower() == "level" or token.startswith("_"):
                continue
            if not any(ch.isdigit() for ch in token):
                continue
            if token not in names:
                names.append(token)
    return names


def _story_rel(level):
    # type: (str) -> Optional[str]
    """内部关卡名 → story/{世界}-{关}；不是主线命名返回 None。"""
    base = _strip_p2(level)
    m = re.match(r"^(?P<theme>[A-Za-z]+)_(?P<world>\d+)_(?P<idx>\d+)[A-Za-z]?$",
                 base)
    if m and m.group("theme") in _STORY_THEMES:
        return "story/%s-%s" % (m.group("world"), m.group("idx"))
    # 1_6_Dynamic_Lvl_01 / 3_6_Dynamic_Lvl_02
    m = re.match(r"^(?P<world>\d+)_(?P<idx>\d+)_", base)
    if m:
        return "story/%s-%s" % (m.group("world"), m.group("idx"))
    return None


def _clip(text, max_chars):
    # type: (str, int) -> str
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    cut = text[:max_chars]
    newline = cut.rfind("\n")
    if newline > max_chars * 0.6:
        cut = cut[:newline]
    return cut.rstrip() + "\n…"


@dataclass(frozen=True)
class Guide(object):
    """一份关卡攻略。"""
    key: str                         # 相对 docs/guides 的路径（不含 .md）
    title: str
    sections: Dict[str, str] = field(default_factory=dict)
    internal_names: Tuple[str, ...] = ()

    def _find(self, prefix):
        # type: (str) -> str
        for key, body in self.sections.items():
            if key.startswith(prefix):
                return body
        return ""

    def division_text(self):
        # type: () -> str
        return self._find(_SECTION_PREFIX["division"])

    def section(self, name):
        # type: (str) -> str
        """按节取原文；`summary` 返回摘要，`all` 返回除来源/元信息外的全文。"""
        name = str(name or "summary").strip().lower()
        if name == "summary":
            return self.summary()
        if name == "all":
            parts = []  # type: List[str]
            for key, body in self.sections.items():
                if "来源" in key or "元信息" in key:
                    continue
                parts.append("## %s\n%s" % (key, body))
            return _clip("\n\n".join(parts), 4000)
        prefix = _SECTION_PREFIX.get(name)
        if prefix is None:
            return ""
        body = self._find(prefix)
        return _clip(body, 2000)

    def summary(self, max_chars=1200):
        # type: (int) -> str
        """给 system prompt 的本关摘要：机制 + 分工 + 陷阱。"""
        parts = []  # type: List[str]
        for name in ("mechanics", "division", "traps"):
            body = self.section(name) if name != "summary" else ""
            if not body:
                continue
            parts.append("【%s】\n%s" % (_SECTION_LABEL[name], body))
        text = "《%s》" % (self.title or self.key)
        if parts:
            text += "\n" + "\n\n".join(parts)
        return _clip(text, max_chars)


class Cookbook(object):
    """攻略库索引；一次加载，换关只做 resolve。"""

    def __init__(self, root=DEFAULT_ROOT):
        # type: (str) -> None
        self.root = os.path.abspath(root)
        self.guides = []                  # type: List[Guide]
        self._by_rel = {}                 # type: Dict[str, Guide]
        self._by_internal = {}            # type: Dict[str, Guide]
        self._load()

    def _load(self):
        # type: () -> None
        pattern = os.path.join(self.root, "**", "*.md")
        for path in sorted(glob.glob(pattern, recursive=True)):
            name = os.path.basename(path)
            if name in ("README.md", "_template.md"):
                continue
            try:
                with open(path, encoding="utf8") as fh:
                    text = fh.read()
            except OSError:
                log.warning("读取攻略失败：%s", path, exc_info=True)
                continue
            rel = os.path.relpath(path, self.root)[:-3].replace(os.sep, "/")
            title, sections = _split_sections(text)
            guide = Guide(key=rel, title=title, sections=sections,
                          internal_names=tuple(_internal_names(sections)))
            self.guides.append(guide)
            self._by_rel[rel] = guide
            for internal in guide.internal_names:
                self._by_internal[internal] = guide
                self._by_internal[_strip_p2(internal)] = guide

    def resolve(self, level):
        # type: (Optional[str]) -> Optional[Guide]
        """插件 `level` → 攻略；也接受 `5-5` / `story/5-5` 便于离线工具。"""
        key = str(level or "").strip()
        if not key:
            return None
        if key.endswith(".md"):
            key = key[:-3]
        if key in self._by_internal:
            return self._by_internal[key]
        if key in self._by_rel:
            return self._by_rel[key]
        base = _strip_p2(key)
        if base in self._by_internal:
            return self._by_internal[base]
        rel = _story_rel(base)
        if rel is not None and rel in self._by_rel:
            return self._by_rel[rel]
        # 5-5 / 1-4 这类人类可读编号
        m = re.match(r"^(?P<world>\d+)-(?P<idx>\d+)$", key)
        if m:
            rel = "story/%s-%s" % (m.group("world"), m.group("idx"))
            return self._by_rel.get(rel)
        return None

    def __len__(self):
        # type: () -> int
        return len(self.guides)


_BOOK = None  # type: Optional[Cookbook]


def default_cookbook():
    # type: () -> Cookbook
    global _BOOK
    if _BOOK is None:
        _BOOK = Cookbook(DEFAULT_ROOT)
    return _BOOK

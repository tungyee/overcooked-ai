"""攻略库人称约定：文档里不出现 P1/P2，统一用「你 / 玩家」。

攻略会被整段注入 LLM，模型可见文本里出现内部代号容易被 TTS 念出来；
`docs/guides` 是唯一事实来源，运行时不再做人称转换。
"""
import glob
import os
import re

GUIDES_DIR = os.path.join(os.path.dirname(__file__), "..", "docs", "guides")
_PERSON_CODE = re.compile(r"\bP[12]\b")


def _guide_files():
    paths = glob.glob(os.path.join(GUIDES_DIR, "**", "*.md"), recursive=True)
    return sorted(p for p in paths if os.path.isfile(p))


def test_guides_exist():
    assert _guide_files(), "docs/guides 下没有攻略文件"


def test_no_internal_person_codes():
    bad = []
    for path in _guide_files():
        with open(path, encoding="utf8") as fh:
            for lineno, line in enumerate(fh, 1):
                if _PERSON_CODE.search(line):
                    bad.append("%s:%d: %s" % (
                        os.path.relpath(path, GUIDES_DIR), lineno, line.strip()))
    assert not bad, "攻略里仍有 P1/P2，请统一成「你 / 玩家」：\n" + "\n".join(bad)


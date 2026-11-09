# -*- coding: utf8 -*-
"""攻略库读取：关卡映射、摘要与按节取文。"""
import os

from ai.dialog.cookbook import Cookbook, default_cookbook


def test_default_cookbook_loads_story_and_dlc():
    book = default_cookbook()
    assert len(book) > 100
    assert book.resolve("Sushi_1_4_2P") is not None
    assert book.resolve("Campfire_1_1") is not None


def test_resolve_exact_internal_name():
    book = default_cookbook()
    guide = book.resolve("Wizard_5_5_2P")
    assert guide is not None
    assert guide.key == "story/5-5"
    assert "Wizard_5_5" in guide.internal_names


def test_wizard32_asset_maps_to_ui_3_3():
    """实机 2026-11-08：UI 3-3 的关卡资产名是 Wizard_3_2（编号差 1）。"""
    book = default_cookbook()
    assert book.resolve("Wizard_3_2_2P").key == "story/3-3"
    assert book.resolve("Wizard_3_2").key == "story/3-3"
    assert book.resolve("Wizard_3_1_2P").key == "story/3-1"


def test_resolve_story_pattern_fallback():
    book = default_cookbook()
    assert book.resolve("Wizard_6_1_2P").key == "story/6-1"
    assert book.resolve("Rapids_2_1_2P").key == "story/2-1"
    assert book.resolve("1_6_Dynamic_Lvl_01_2P").key == "story/1-6"
    assert book.resolve("3_6_Dynamic_Lvl_02_2P").key == "story/3-6"


def test_resolve_human_number_and_rel_path():
    book = default_cookbook()
    assert book.resolve("5-5").key == "story/5-5"
    assert book.resolve("story/3-4").key == "story/3-4"


def test_dlc_theme_does_not_fall_back_to_story():
    book = default_cookbook()
    assert book.resolve("Campfire_9_9_2P") is None


def test_summary_and_sections_have_no_internal_codes():
    book = default_cookbook()
    guide = book.resolve("Wizard_5_5_2P")
    summary = guide.summary()
    assert "打法与分工" in summary
    assert "关卡机制" in summary
    assert "P1" not in summary and "P2" not in summary
    assert "煎肉" in summary or "煎" in summary
    assert "P1" not in guide.section("division")


def test_section_aliases():
    book = default_cookbook()
    guide = book.resolve("Wizard_5_5_2P")
    assert "玩家" in guide.section("division")
    assert "楼梯" in guide.section("mechanics")
    assert "高度差" in guide.section("traps")
    assert "汉堡" in guide.section("recipes")
    assert guide.section("summary") == guide.summary()
    assert guide.section("all")
    assert guide.section("unknown") == ""


def test_custom_root_parses_internal_names(tmp_path):
    (tmp_path / "story").mkdir()
    (tmp_path / "story" / "9-9.md").write_text(
        "# 测试关（主线 9-9）\n\n"
        "## 元信息\n\n- 内部关卡名：`Test_9_9_2P`\n\n"
        "## 打法与分工（2P）\n\n- 玩家：切。\n- 你：煎。\n",
        encoding="utf8")
    book = Cookbook(str(tmp_path))
    guide = book.resolve("Test_9_9_2P")
    assert guide is not None and guide.key == "story/9-9"
    assert "玩家" in guide.section("division")
    assert "你" in guide.summary()


def test_rapids35_guide_has_no_fixed_division():
    """3-5 木筏在巡航，固定分工会让 P2 等玩家切菜；攻略要明确不设固定分工。"""
    guide = default_cookbook().resolve("Rapids_3_5_2P")
    assert guide is not None
    text = guide.section("division")
    assert "不设固定分工" in text
    assert "一人切菜" not in text

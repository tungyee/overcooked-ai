# -*- coding: utf-8 -*-
"""历史 trace 回放工具的纯函数：快照还原与离线跑一轮（fake LLM）。"""
import json

from ai.dialog.llm import LLMReply
from tools import replay_dialog_llm as R


def test_state_for_world_expands_snapshot_map_and_belts():
    record = {
        "t": 123,
        "state": {
            "level": "Sushi_1_4_2P",
            "phase": "level",
            "orders": [],
            "map": {
                "grid": {"origin": "(0, 0, 0)", "size": "(1, 1, 1)"},
                "grids": [{"id": "GridManager"}],
                "terrain": [{"layer": 0, "boxes": []}],
                "occupancy": [{"grid": "GridManager", "cells": [[0, 0]]}],
                "hazards": [],
                "belt_cells": [[1, 2]],
                "belt_dir": {"1,2": [-1, 0]},
            },
        },
    }

    state = R._state_for_world(record)

    assert state["grid"]["origin"] == "(0, 0, 0)"
    assert state["grids"] == [{"id": "GridManager"}]
    assert state["terrain"] == [{"layer": 0, "boxes": []}]
    assert state["occupancy"][0]["cells"] == [[0, 0]]
    assert state["belts"] == [{"gx": 1, "gz": 2, "vx": -1, "vz": 0}]
    assert "map" not in state


def test_state_for_world_falls_back_to_raw_frame():
    record = {"level": "Sushi_1_4_2P", "phase": "level",
              "chefs": [], "orders": [], "stations": []}
    assert R._state_for_world(record)["level"] == "Sushi_1_4_2P"


class _ScriptedLLM(object):
    """第一段返回工具调用，第二段（tool_choice=none）返回最终那句话。"""

    def __init__(self, first, final):
        self.first, self.final = first, final

    def chat(self, messages, tools=None, tool_choice="auto", on_text=None):
        reply = self.final if tool_choice == "none" else self.first
        if on_text is not None and reply.content:
            on_text(reply.content)
        return reply


def test_run_scenario_offline_applies_ledger_and_checks(tmp_path, capsys):
    import gzip
    import os
    fixture = os.path.join(os.path.dirname(__file__), "fixtures",
                           "dialog_scenarios.ndjson.gz")
    with gzip.open(fixture, "rt") as fh:
        state = [json.loads(line) for line in fh][-1]
    frame = R.Frame(path="fixture", line=1, tick=1, state=state)
    llm = _ScriptedLLM(LLMReply("送餐不用分，谁在旁边谁送。"), LLMReply(""))
    scenario = [s for s in R.SCENARIOS if s.name == "i_deliver"][0]
    args = type("A", (), {"show_prompt": False,
                          "dry_run": False})()

    ok = R.run_scenario(scenario, frame, llm, args)

    out = capsys.readouterr().out
    assert ok, out
    assert "基线 P2 选择：DeliverPlate" in out
    assert "送餐不用分" in out

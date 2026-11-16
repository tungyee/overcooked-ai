"""tools/exec_ping.py：27412 通道存活探测。"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))

import exec_ping as E  # noqa: E402


class _FakeClient(object):
    connected = True
    reply = {"cmd_id": 0, "ok": True, "why": None}

    def __init__(self, host="127.0.0.1", port=27412):
        self.host, self.port = host, port
        self.closed = False

    def connect(self):
        pass

    def ping(self, timeout=2.0):
        return self.reply

    def close(self):
        self.closed = True


def test_main_returns_zero_on_live_channel(monkeypatch, capsys):
    monkeypatch.setattr(E, "ExecClient", _FakeClient)
    assert E.main(["--timeout", "0.1"]) == 0
    assert "正常" in capsys.readouterr().out


def test_main_returns_one_when_no_reply(monkeypatch, capsys):
    class Silent(_FakeClient):
        reply = None

    monkeypatch.setattr(E, "ExecClient", Silent)
    assert E.main(["--timeout", "0.1"]) == 1
    assert "无响应" in capsys.readouterr().out


def test_main_returns_one_when_connect_refused(monkeypatch, capsys):
    class Refused(_FakeClient):
        connected = False

    monkeypatch.setattr(E, "ExecClient", Refused)
    assert E.main(["--timeout", "0.1"]) == 1
    assert "连不上" in capsys.readouterr().out

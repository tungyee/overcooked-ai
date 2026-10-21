# -*- coding: utf-8 -*-
"""ExecClient 的非阻塞结果轮询测试。"""
import json
import socket

from ai.exec.client import ExecClient


def _connected_client():
    srv, cli = socket.socketpair()
    cli.setblocking(False)      # 客户端读必须非阻塞，poll 才不会卡主循环
    c = ExecClient()
    c._sock = cli
    c._connected = True
    return c, srv


def test_issue_returns_minus_one_when_not_connected():
    assert ExecClient().issue({"cmd": "halt"}) == -1


def test_issue_writes_json_line_with_cmd_id():
    c, srv = _connected_client()
    cid = c.issue({"cmd": "halt"})
    obj = json.loads(srv.recv(4096).decode("utf-8").strip())
    assert obj["cmd_id"] == cid
    assert obj["cmd"] == "halt"


def test_poll_returns_none_when_no_data():
    c, _ = _connected_client()
    assert c.poll() is None


def test_result_never_silently_dropped():
    c, srv = _connected_client()
    srv.sendall(b'{"cmd_id":1,"ok":true}\n{"cmd_id":2,"ok":false,"why":"stuck"}\n')
    r1 = c.poll()
    r2 = c.poll()
    assert r1 == {"cmd_id": 1, "ok": True, "why": None, "held_after": None,
                  "blocked_cell": None}
    assert r2["cmd_id"] == 2 and r2["ok"] is False and r2["why"] == "stuck"
    assert c.poll() is None


def test_bad_line_is_dropped_not_crash():
    c, srv = _connected_client()
    srv.sendall(b'garbage\n{"cmd_id":3,"ok":true}\n')
    r = c.poll()                 # 坏行丢弃，返回下一条好行
    assert r["cmd_id"] == 3 and r["ok"] is True

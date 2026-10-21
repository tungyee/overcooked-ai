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


def test_poll_idle_socket_stays_connected():
    c, srv = _connected_client()
    try:
        assert c.poll() is None
        assert c.connected
    finally:
        c.close()
        srv.close()


def test_eof_drains_complete_results_then_clears_partial_for_reconnect():
    c, srv = _connected_client()
    srv.sendall(b'{"cmd_id":1,"ok":true}\n{"cmd_id":2,"ok":true}\n{"cmd_id":')
    srv.close()
    assert c.poll()['cmd_id'] == 1
    assert c.poll()['cmd_id'] == 2
    assert c.poll() is None
    assert not c.connected and c._buf == b''


def test_invalid_utf8_does_not_discard_following_result():
    c, srv = _connected_client()
    try:
        srv.sendall(b'\xff\n{"cmd_id":3,"ok":true}\n')
        assert c.poll()['cmd_id'] == 3
        assert c._dropped == 1
    finally:
        c.close()
        srv.close()


def test_real_tcp_client_can_reconnect_after_plugin_disconnect():
    with socket.socket() as server:
        server.bind(('127.0.0.1', 0))
        server.listen(2)
        server.settimeout(1.)
        c = ExecClient(port=server.getsockname()[1])
        try:
            c.connect()
            peer, _ = server.accept()
            peer.shutdown(socket.SHUT_RDWR)
            peer.close()
            for _ in range(100):
                c.poll()
                if not c.connected:
                    break
                import time
                time.sleep(.005)
            assert not c.connected
            c.connect()
            with server.accept()[0] as peer:
                cid = c.issue({'cmd': 'halt'})
                assert json.loads(peer.recv(4096))['cmd_id'] == cid
        finally:
            c.close()

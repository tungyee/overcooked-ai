# -*- coding: utf-8 -*-
"""ExecClient：非阻塞 exec 通道客户端。

与 27411 快照端口不同，这里走独立的 27412 exec 端口：Python 发命令、
插件回 step_done 结果。主循环 20Hz 每拍 `poll()` 一次，绝不阻塞。

「结果不丢」的实现：结果行只从 27412 独立读，绝不经过 27411 的
`read_latest`（那个会丢旧帧）。坏行计数丢弃，但不中断后续好行——
宁可跳过一条脏数据，也不能让一个坏包把整条结果链卡死。
"""
import json
import socket
import threading
from typing import Dict, Optional

from ai.exec.commands import parse_result

_DEFAULT_PORT = 27412


class ExecClient(object):
    def __init__(self, host="127.0.0.1", port=_DEFAULT_PORT):
        # type: (str, int) -> None
        self.host = host
        self.port = port
        self._sock = None          # type: Optional[socket.socket]
        self._connected = False
        self._next_id = 0
        self._buf = b""            # 未取完的字节缓冲
        self._dropped = 0
        self._lock = threading.Lock()

    def connect(self):
        # type: () -> None
        """幂等连接。连不上不抛，只置 connected=False。"""
        if self._connected:
            return
        try:
            s = socket.create_connection((self.host, self.port), timeout=1.0)
            s.setblocking(False)
            self._sock = s
            self._connected = True
        except OSError:
            self._connected = False

    @property
    def connected(self):
        # type: () -> bool
        """是否已连上 exec 通道（27412）。"""
        return self._connected

    def issue(self, cmd_dict):
        # type: (Dict) -> int
        """补 cmd_id 后写一行 JSON。未连接返回 -1。

        覆盖传入 dict 的 cmd_id（id 由本客户端独占分配与关联）。
        """
        if not self._connected or self._sock is None:
            return -1
        cmd_id = self._next_id
        self._next_id += 1
        cmd_dict["cmd_id"] = cmd_id
        line = json.dumps(cmd_dict) + "\n"
        try:
            with self._lock:
                self._sock.sendall(line.encode("utf-8"))
        except OSError:
            self.close()
            return -1
        return cmd_id

    def poll(self):
        # type: () -> Optional[Dict]
        """非阻塞取一个已完成的结果；无则 None。坏行丢弃并继续读下一条。"""
        if not self._connected or self._sock is None:
            return None
        while True:
            idx = self._buf.find(b"\n")
            if idx < 0:
                try:
                    data = self._sock.recv(4096)
                except BlockingIOError:
                    return None  # Windows WSAEWOULDBLOCK 只是本拍没有新结果。
                except OSError:
                    self.close()
                    return None
                if not data:
                    self.close()  # EOF 必须触发主循环重连，不能永远保持假 connected。
                    return None
                self._buf += data
                continue
            raw_line = self._buf[:idx]
            self._buf = self._buf[idx + 1:]
            try:
                line = raw_line.decode("utf-8")
                return parse_result(line)
            except ValueError:
                self._dropped += 1

    def close(self):
        # type: () -> None
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
        self._sock = None
        self._connected = False
        self._buf = b""


class NullExec(object):
    """无连接 exec：命令即时回 ok（离线冒烟 / --dry-run 用）。

    issue 记录命令并返回递增 id；poll 总是回最近一条命令的 ok 结果，
    让委托路径的叶子原语一路走到 SUCCESS 而不真正连 27412。
    """

    def __init__(self):
        # type: () -> None
        self.issued = []      # type: list
        self._next_id = 0

    def connect(self):
        # type: () -> None
        pass

    @property
    def connected(self):
        # type: () -> bool
        return True

    def issue(self, cmd_dict):
        # type: (Dict) -> int
        self._next_id += 1
        cmd_dict = dict(cmd_dict, cmd_id=self._next_id)
        self.issued.append(cmd_dict)
        return self._next_id

    def poll(self):
        # type: () -> Optional[Dict]
        if not self.issued:
            return None
        last = self.issued[-1]
        return {"cmd_id": last["cmd_id"], "ok": True, "why": None,
                "held_after": None, "blocked_cell": None}

    def close(self):
        # type: () -> None
        pass

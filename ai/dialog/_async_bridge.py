"""把 async 生成器桥接成同步生成器 + 有界 WebSocket 建连/读取。

火山客户端基于 ``asyncio``/``websockets``，而 Task 11 的 worker 是同步
线程。这里为每次调用起一个短生命周期线程跑事件循环：后台线程执行 async
生成器并把结果放进队列，同步侧按需取出。调用方提前关闭生成器时，通过
``loop.call_soon_threadsafe`` 取消任务，连接的关闭逻辑留在 async 生成器
的 ``finally`` 里。

``connect`` / ``recv`` 把建连与每次读取都套上 ``asyncio.wait_for``：
火山服务偶发不回帧时，worker 不能无限期挂在 ``recv()`` 上（spec §7.4）。
"""
import asyncio
import contextlib
import queue
import threading

from websockets.exceptions import InvalidStatus


# 提前 stop 时最多等待后台线程这么久；网络异常时不能拖住 worker。
_JOIN_TIMEOUT = 2.0

# 建连/单次读取的默认上限（秒）。真实服务正常远低于它；测试可注入更短值。
DEFAULT_WS_TIMEOUT_S = 5.0


async def async_queue_get(items):
    # type: (queue.Queue) -> object
    """可取消地读取线程队列，避免 executor 的永久 get 阻止解释器退出。"""
    while True:
        try:
            return items.get_nowait()
        except queue.Empty:
            await asyncio.sleep(0.01)


@contextlib.asynccontextmanager
async def connect(factory, *args, timeout=DEFAULT_WS_TIMEOUT_S, label="WS",
                  **kwargs):
    # type: (object, object, float, str, object) -> object
    """有界建连：``factory(*args, **kwargs)`` 的 ``__aenter__`` 超时即报错。"""
    cm = factory(*args, **kwargs)
    try:
        ws = await asyncio.wait_for(cm.__aenter__(), timeout)
    except asyncio.TimeoutError as exc:
        raise RuntimeError(
            "%s 建连超时（%.1fs）" % (label, timeout)) from exc
    except InvalidStatus as exc:
        # 火山在握手阶段就会拒绝未开通的资源；把响应体带出来，
        # 否则调用方只能看到笼统的 403。
        raise RuntimeError(
            _invalid_status_message(label, exc.response)) from exc
    try:
        yield ws
    finally:
        with contextlib.suppress(Exception):
            await cm.__aexit__(None, None, None)


def _invalid_status_message(label, response):
    # type: (str, object) -> str
    """握手被拒时拼出「状态码 + 服务端响应体」，便于定位资源未开通等问题。"""
    status = getattr(response, "status_code", "?")
    body = getattr(response, "body", b"") or b""
    if isinstance(body, (bytes, bytearray)):
        body = bytes(body).decode("utf-8", "replace")
    detail = str(body).strip()
    return "%s 建连被拒绝：HTTP %s%s" % (
        label, status, ("：" + detail[:300]) if detail else "")


async def recv(ws, timeout=DEFAULT_WS_TIMEOUT_S, label="WS"):
    # type: (object, float, str) -> object
    """有界读取一帧；超时转成带标签的 RuntimeError。"""
    try:
        return await asyncio.wait_for(ws.recv(), timeout)
    except asyncio.TimeoutError as exc:
        raise RuntimeError("%s 读取超时（%.1fs）" % (label, timeout)) from exc


def iter_async(async_gen_factory):
    # type: (object) -> object
    items = queue.Queue()
    loop_holder = {}
    task_holder = {}

    async def pump():
        try:
            async for item in async_gen_factory():
                items.put(("item", item))
        except asyncio.CancelledError:
            items.put(("done", None))
            raise
        except BaseException as exc:  # noqa: BLE001 - 原样交给同步调用方
            items.put(("error", exc))
        else:
            items.put(("done", None))

    def run():
        loop = asyncio.new_event_loop()
        loop_holder["loop"] = loop
        try:
            asyncio.set_event_loop(loop)
            task = loop.create_task(pump())
            task_holder["task"] = task
            loop.run_until_complete(task)
        except asyncio.CancelledError:
            pass
        finally:
            loop.close()
            # 兜底哨兵：即使 pump 异常退出，同步侧也不会永远阻塞。
            items.put(("done", None))

    thread = threading.Thread(target=run, daemon=True, name="volc-ws")
    thread.start()
    try:
        while True:
            kind, payload = items.get()
            if kind == "item":
                yield payload
            elif kind == "error":
                raise payload
            else:
                return
    finally:
        loop = loop_holder.get("loop")
        task = task_holder.get("task")
        if loop is not None and task is not None and not task.done():
            try:
                loop.call_soon_threadsafe(task.cancel)
            except RuntimeError:
                # 线程恰好已关闭事件循环，任务自然也不会再运行。
                pass
        thread.join(_JOIN_TIMEOUT)

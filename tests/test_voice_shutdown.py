"""取消语音队列读取后 Python 必须能退出；不连接服务或打开音频设备。"""
import os
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.mark.parametrize('source', ['asr', 'tts'])
def test_cancelled_empty_voice_queue_does_not_prevent_process_exit(source):
    # Windows venv 的 python.exe 是包装器；直接测真实解释器，超时可结束准确的子进程。
    code = '''
import asyncio, queue, sys
from ai.dialog import asr, tts
async def main():
    if sys.argv[1] == 'asr':
        task=asyncio.create_task(asr._pack_queue(queue.Queue(),4).__anext__())
    else:
        client=tts.WarmTTSClient('fake','fake','fake')
        task=asyncio.create_task(client._serve())
    await asyncio.sleep(.08)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
asyncio.run(main())
print('EXIT_OK')
'''
    env = dict(os.environ, PYTHONPATH=os.pathsep.join(p for p in sys.path if p))
    result = subprocess.run([getattr(sys, '_base_executable', sys.executable), '-c', code, source],
        cwd=Path(__file__).resolve().parents[1], env=env,
        capture_output=True, text=True, timeout=5.)
    assert result.returncode == 0, result.stderr
    assert 'EXIT_OK' in result.stdout


def test_closed_warm_tts_cannot_restart_background_thread():
    from ai.dialog.tts import WarmTTSClient
    client = WarmTTSClient('fake','fake','fake')
    client.close()
    client.close()
    with pytest.raises(RuntimeError, match='关闭'):
        client.warmup()
    with pytest.raises(RuntimeError, match='关闭'):
        client.synthesize('不能重新启动')

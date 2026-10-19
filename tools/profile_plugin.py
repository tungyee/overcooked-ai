"""只读诊断插件采集成本与 Unity 帧时间；不会注入按键或声称因果 FPS 提升。"""
import argparse
import json
import socket
import statistics
import time
from pathlib import Path


def summarize(frames):
    # 第一帧的 previous_snapshot 可能来自上一客户端/启动阶段，剔除。
    samples = [f['diagnostics'] for f in frames[1:] if isinstance(f.get('diagnostics'), dict)]
    result = dict(frames=len(frames), samples=len(samples),
                  levels=sorted({str(f.get('level')) for f in frames}),
                  phases=sorted({str(f.get('phase')) for f in frames}))
    for key in ('snapshot_ms','scan_ms','state_ms','navigation_ms','physics_queries','components'):
        values = sorted(d[key] for d in samples if isinstance(d.get(key),(float,int)))
        if values:
            result[key] = dict(median=round(statistics.median(values),2),
                               p95=round(values[min(len(values)-1,int(len(values)*.95))],2),max=max(values))
    count = sum(d.get('render_frames',0) for d in samples)
    result['render_frames'] = count
    if count:
        result['mean_frame_ms'] = round(sum(d.get('mean_frame_ms',0)*d.get('render_frames',0) for d in samples)/count,2)
        result['max_frame_ms'] = max(d.get('max_frame_ms',0) for d in samples)
        result['frames_over_33ms'] = sum(d.get('frames_over_33ms',0) for d in samples)
    result['physics_saturated'] = sum(d.get('physics_saturated',0) for d in samples)
    sections = sorted({k for d in samples for k in (d.get('sections_ms') or {})})
    if sections:
        result['sections_ms'] = {}
        for key in sections:
            values = sorted(d['sections_ms'][key] for d in samples
                            if isinstance((d.get('sections_ms') or {}).get(key),(int,float)))
            if values:
                result['sections_ms'][key] = dict(median=round(statistics.median(values),2),
                    p95=round(values[min(len(values)-1,int(len(values)*.95))],2), max=max(values))
    result['note'] = 'Only the connected sampling window; not an A/B FPS comparison. Foreground, pause, VSync and GPU load affect frame times.'
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--seconds',type=float,default=10)
    parser.add_argument('--output',type=Path)
    args=parser.parse_args()
    if not 0<args.seconds<=600:
        parser.error('--seconds must be in (0,600]')
    frames=[];buffer=b'';end=time.monotonic()+args.seconds
    with socket.create_connection(('127.0.0.1',27411),timeout=3) as stream:
        stream.settimeout(.5)
        while time.monotonic()<end:
            try:chunk=stream.recv(262144)
            except socket.timeout:continue
            if not chunk:break
            buffer+=chunk
            while b'\n' in buffer:
                line,buffer=buffer.split(b'\n',1)
                frame=json.loads(line)
                if isinstance(frame,dict):frames.append(frame)
    result=summarize(frames)
    if args.output:
        args.output.parent.mkdir(parents=True,exist_ok=True)
        args.output.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(result,ensure_ascii=False,indent=2))
    return 0 if result['samples'] else 1


if __name__=='__main__':
    raise SystemExit(main())

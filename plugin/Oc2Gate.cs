// Oc2Gate.cs —— move 命令的平台接缝门洞跟随（Python 侧 ai/space/seams.py 发 gates 字段）。
//
// 接缝 = 两块地板贴边处两道背靠背的矮墙，各开一个门洞；篮子在动，两门洞的重叠
// 区间（缝）跟着变（Balloon_2_3 实测 0.55m/s，过一次缝 0.5~1s 门洞挪 0.3~0.5m）。
// Python 按 5 Hz 帧算的中线发出去就过期了，这里每帧用墙碰撞体现算：
//   墙带（接缝线 ±half）里非忽略碰撞体沿接缝方向的投影取补集，取离上次中线最近的
//   一段，两头各收窄 radius；两端缝口各向下打 Ground 射线确认有地板。
// 入点/出点的跨缝坐标固定（Python 给的 enter_c/exit_c），沿缝坐标取现算中线。
// 门洞此刻没开：还没进缝就在缝口外停下等（最多 GATE_WAIT），已进缝则沿用上次中线。
using System.Collections.Generic;
using UnityEngine;

namespace Oc2AI
{
    internal sealed class Gate
    {
        private const float WINDOW = 1.5f;        // 只在上次中线 ±(上次半宽+WINDOW) 内找缝
        private const float MIN_BAND = 0.1f;      // 收窄后缝至少这么宽才算开
        private const int GROUND_MASK = 1 << 9;

        public readonly bool AlongX;              // axis=="x"：接缝线 x=Line、沿 z 延伸
        public readonly float Line, Half, EnterC, ExitC, Lo, Hi, Radius;
        public readonly int Enter, Exit;          // 路点下标
        public readonly float MaxWait;            // 缝口等门上限（秒）；Python 按预测等待时间给
        public float Mid;                         // 当前中线（沿缝坐标）
        public float Width;                       // 当前可站宽度（收窄后）
        public bool Open;

        public Gate(Dictionary<string, object> g)
        {
            AlongX = Req.Str(g, "axis") == "x";
            Line = (float)(Req.Num(g, "line") ?? 0.0);
            Half = (float)(Req.Num(g, "half") ?? 0.6);
            EnterC = (float)(Req.Num(g, "enter_c") ?? 0.0);
            ExitC = (float)(Req.Num(g, "exit_c") ?? 0.0);
            Lo = (float)(Req.Num(g, "lo") ?? -1e4);
            Hi = (float)(Req.Num(g, "hi") ?? 1e4);
            Radius = (float)(Req.Num(g, "radius") ?? 0.45);
            Enter = (int)(Req.Num(g, "enter") ?? -1);
            Exit = (int)(Req.Num(g, "exit") ?? -1);
            Mid = (float)(Req.Num(g, "mid") ?? 0.0);
            MaxWait = (float)(Req.Num(g, "max_wait") ?? 4.0);
            Width = 0f;
            Open = true;
        }

        public bool Valid { get { return Enter >= 1 && Exit == Enter + 1; } }

        // 世界坐标 (x,z)：跨缝坐标 c、沿缝坐标 a。
        private Vector2 Pt(float c, float a) { return AlongX ? new Vector2(c, a) : new Vector2(a, c); }

        public Vector2 EnterPoint { get { return Pt(EnterC, Mid); } }
        public Vector2 ExitPoint { get { return Pt(ExitC, Mid); } }

        // 人沿跨缝方向已过出点（出点按跨缝坐标判到达：沿缝坐标一直在动）。
        public bool PastExit(Vector2 pos)
        {
            float c = AlongX ? pos.x : pos.y;
            return EnterC < ExitC ? c >= ExitC - 0.1f : c <= ExitC + 0.1f;
        }

        // 人沿跨缝方向已进墙带（过了入点）。
        public bool PastEnter(Vector2 pos)
        {
            float c = AlongX ? pos.x : pos.y;
            return EnterC < ExitC ? c > EnterC + 0.05f : c < EnterC - 0.05f;
        }

        /// <summary>按墙碰撞体现算门洞；返回是否开着，并更新 Mid/Width。</summary>
        // 沿缝采样步长；跨缝取入点 / 墙带两边 / 接缝线 / 出点 5 条线，都站得下才算这一点通。
        private const float STEP = 0.1f;
        // 测胶囊比厨师真身半径小这么多：贴墙站立的接触余量；收窄由 Radius-实际半径 补回
        private const float SKIN = 0.02f;
        private float _nextRefresh = -1f;
        public string Blocker;            // 最近一次判关时挡在上次中线处的碰撞体（诊断）

        /// <summary>
        /// 用厨师胶囊按真实碰撞形状逐点测门洞（与 walk_probe 同一套过滤）；返回是否开着，
        /// 并更新 Mid/Width。不用碰撞体 bounds 投影：网格碰撞体 / 整块假墙的 AABB 会盖住
        /// 实际开着的门洞（trace-20261030-123850：Python 与 walk_probe 都判通，按 AABB
        /// 投影判关，P2 在缝口等到超时、反复重发）。
        /// </summary>
        public bool Refresh(Collider body, float now)
        {
            if (body == null) return Open;
            if (now < _nextRefresh) return Open;
            _nextRefresh = now + 0.05f;      // 20 Hz 足够跟 0.55m/s 的篮子
            Bounds b = body.bounds;
            float footY = b.min.y;
            float skin = Physics.defaultContactOffset;
            float r = Mathf.Max(skin, Mathf.Min(b.extents.x, b.extents.z) - skin - SKIN);
            float y0 = footY + 0.1f + r, y1 = Mathf.Max(y0, footY + b.size.y - skin - r);
            // 门关着（等门 / 预测门洞）时放宽窗口：开门时缝不一定正好在预测的中线上
            float reach = Mathf.Max(Width * 0.5f, 0.3f) + (Open ? WINDOW : 2f * WINDOW);
            float w0 = Mathf.Max(Lo, Mid - reach), w1 = Mathf.Min(Hi, Mid + reach);
            if (w1 - w0 < 2 * Radius) { Open = false; return false; }
            float[] cs = { EnterC, Line - Half, Line, Line + Half, ExitC };
            float bestMid = 0f, bestW = -1f, bestD = float.MaxValue;
            float runStart = float.NaN;
            Collider nearMid = null;
            float nearD = float.MaxValue;
            for (float a = w0; a <= w1 + 1e-4f; a += STEP)
            {
                Collider hit = null;
                foreach (float c in cs)
                {
                    hit = BodyHit(body, Pt(c, a), y0, y1, r);
                    if (hit != null) break;
                }
                if (hit == null)
                {
                    if (float.IsNaN(runStart)) runStart = a;
                    continue;
                }
                if (Mathf.Abs(a - Mid) < nearD) { nearD = Mathf.Abs(a - Mid); nearMid = hit; }
                if (!float.IsNaN(runStart))
                {
                    Consider(runStart, a - STEP, footY, r, ref bestMid, ref bestW, ref bestD);
                    runStart = float.NaN;
                }
            }
            if (!float.IsNaN(runStart)) Consider(runStart, w1, footY, r, ref bestMid, ref bestW, ref bestD);
            if (bestW < 0f)
            {
                Open = false;
                Width = 0f;
                Blocker = nearMid != null ? nearMid.name + "/" + LayerMask.LayerToName(nearMid.gameObject.layer) : "none";
                return false;
            }
            Mid = bestMid;
            Width = bestW;
            Open = true;
            return true;
        }

        private static readonly Collider[] Overlaps = new Collider[16];

        private static Collider BodyHit(Collider body, Vector2 p, float y0, float y1, float r)
        {
            int n = Physics.OverlapCapsuleNonAlloc(new Vector3(p.x, y0, p.y), new Vector3(p.x, y1, p.y), r,
                                                   Overlaps, ~0, QueryTriggerInteraction.Ignore);
            Collider found = null;
            int bodyLayer = body.gameObject.layer;
            for (int i = 0; i < n; i++)
            {
                Collider o = Overlaps[i];
                Overlaps[i] = null;
                if (found != null || o == null || o == body) continue;
                if (o.gameObject.layer == Oc2AIPlugin.SlopedLayer()) continue;
                if (Physics.GetIgnoreLayerCollision(bodyLayer, o.gameObject.layer)) continue;
                if (Oc2AIPlugin.WalkIgnored(o)) continue;
                found = o;
            }
            return found;
        }

        // [a0,a1] 是胶囊中心（半径 r）全部站得下的一段；按 Radius 留出余量后取中点。
        private void Consider(float a0, float a1, float footY, float r, ref float bestMid, ref float bestW, ref float bestD)
        {
            float pad = Mathf.Max(0f, Radius - r);
            float b0 = a0 + pad, b1 = a1 - pad;
            if (b1 - b0 < MIN_BAND) return;
            // 走段中点（离两边墙最远）；多段时挑离上次中线最近的，保持连续
            float m = (b0 + b1) * 0.5f;
            if (!HasFloor(EnterC, m, footY) || !HasFloor(ExitC, m, footY)) return;
            float d = Mathf.Abs(m - Mid);
            if (d < bestD) { bestD = d; bestMid = m; bestW = b1 - b0; }
        }

        private bool HasFloor(float c, float a, float footY)
        {
            Vector2 p = Pt(c, a);
            return Physics.Raycast(new Vector3(p.x, footY + 1.5f, p.y), Vector3.down, 3f,
                                   GROUND_MASK, QueryTriggerInteraction.Ignore);
        }
    }
}

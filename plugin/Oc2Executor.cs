// Oc2Executor.cs —— 逐帧执行循环：ChefFrame（P2 状态读取）+ IExecCommand + MoveCommand。
//
// 每帧在主线程 tick（由 Oc2AIPlugin.Update 驱动），读 P2 的实时位置/相对位置/
// 速度，朝 waypoint 转向。到达判据是连续距离（不再离散格），stuck 有界重试。
//
// Ruling（Task 5）: move 只做位置到达，不含朝向/句柄校验——朝向 + 句柄校验
// 下沉到 tap（Task 6）按前的「重瞄 + 校验」，face/align 的职责由 tap 的重瞄承担。
// 代价：move 到位后可能背对目标，tap 会先重瞄再按（略慢一拍，但正确、不塌树）。
using System;
using System.Collections.Generic;
using System.Reflection;
using UnityEngine;

namespace Oc2AI
{
    /// <summary>缓存化反射读取：每 (类型,字段名) 只解析一次 FieldInfo。</summary>
    internal static class Reflect
    {
        private static readonly Dictionary<string, FieldInfo> _cache = new Dictionary<string, FieldInfo>();
        private static readonly Dictionary<string, MethodInfo> _methods = new Dictionary<string, MethodInfo>();

        public static object Field(object o, string name)
        {
            if (o == null) return null;
            Type t = o.GetType();
            string key = t.FullName + "." + name;
            FieldInfo f;
            if (!_cache.TryGetValue(key, out f))
            {
                for (Type tt = t; tt != null && tt != typeof(object); tt = tt.BaseType)
                {
                    f = tt.GetField(name, BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic);
                    if (f != null) break;
                }
                _cache[key] = f;   // 可为 null（字段不存在），下次直接短路
            }
            return f != null ? f.GetValue(o) : null;
        }

        public static object Call(object o, string name)
        {
            if (o == null) return null;
            Type t = o.GetType();
            string key = t.FullName + "." + name;
            MethodInfo m;
            if (!_methods.TryGetValue(key, out m))
            {
                m = t.GetMethod(name, BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic);
                _methods[key] = m;
            }
            if (m == null) return null;
            try { return m.Invoke(o, null); }
            catch { return null; }
        }

        public static float ToFloat(object o)
        {
            if (o == null) return 0f;
            if (o is float f) return f;
            if (o is int i) return (float)i;
            if (o is double d) return (float)d;
            if (o is long l) return (float)l;
            return 0f;
        }
    }

    public interface IExecCommand
    {
        bool Done { get; }
        bool Ok { get; }
        string Why { get; }
        double[] BlockedCell { get; }
        void Tick(ChefFrame f, float now);
    }

    /// <summary>P2 厨师的逐帧状态。构造时找一次厨师，之后每帧 Refresh 读轻量字段。</summary>
    public class ChefFrame
    {
        public PlayerControls Chef { get; private set; }
        public float PosX { get; private set; }
        public float PosZ { get; private set; }
        public float RelX { get; private set; }
        public float RelZ { get; private set; }
        public float VelX { get; private set; }
        public float VelZ { get; private set; }
        public bool Valid { get { return Chef != null; } }

        private float _prevX, _prevZ;
        private bool _hasPrev;
        private float _lastFindTime = -10f;

        public ChefFrame()
        {
            Chef = FindP2();
            _lastFindTime = Time.unscaledTime;
        }

        public static PlayerControls FindP2()
        {
            var all = UnityEngine.Object.FindObjectsOfType<PlayerControls>();
            foreach (var c in all)
            {
                if (c == null || c.name == null) continue;
                if (c.name.IndexOf("Player 2", StringComparison.Ordinal) >= 0) return c;
            }
            return null;
        }

        public void Refresh()
        {
            if (Chef == null)
            {
                // 节流：P2 还没出现（主菜单/加载中）时，别每帧全场景 FindObjectsOfType
                // —— 那是 60Hz 的全场景扫描，会把 CPU 打满（实机：菜单阶段 101%）。
                if (Time.unscaledTime - _lastFindTime < 1.0f) return;
                _lastFindTime = Time.unscaledTime;
                Chef = FindP2();
                if (Chef == null) return;
            }
            Vector3 p = Chef.transform.position;
            PosX = p.x;
            PosZ = p.z;

            // 相对脚下地面：站在移动平台（篮子）上时世界坐标被带着漂，stuck 判定
            // 会误报「没动」。减掉地面 collider 参考点（复用 AppendChefs 的算法）。
            RelX = PosX;
            RelZ = PosZ;
            object mov = Reflect.Field(Chef, "m_surfaceMovable");
            object gc = mov != null ? Reflect.Field(mov, "m_groundCast") : null;
            object gcol = gc != null ? Reflect.Field(gc, "m_groundCollider") : null;
            if (gcol is UnityEngine.Object uc && uc != null)
            {
                GameObject cGo = gcol as GameObject;
                if (cGo == null && gcol is Component cc) cGo = cc.gameObject;
                if (cGo != null)
                {
                    Vector3 cp = cGo.transform.position;
                    RelX = PosX - cp.x;
                    RelZ = PosZ - cp.z;
                }
            }

            // 速度用位置差推：不依赖 m_lastVelocity 字段名/类型，稳。
            float dt = Time.unscaledDeltaTime;
            if (dt > 1e-6f && _hasPrev)
            {
                VelX = (PosX - _prevX) / dt;
                VelZ = (PosZ - _prevZ) / dt;
            }
            else
            {
                VelX = 0f;
                VelZ = 0f;
            }
            _prevX = PosX;
            _prevZ = PosZ;
            _hasPrev = true;
        }

        // ---------- 交互句柄（tap/hold 命令按前校验用） ----------
        public float PickupGx { get; private set; } = -9999f;
        public float PickupGz { get; private set; } = -9999f;
        public string PickupName { get; private set; }
        public int PickupId { get; private set; }
        public float PlacementGx { get; private set; } = -9999f;
        public float PlacementGz { get; private set; } = -9999f;
        public string PlacementName { get; private set; }
        public int PlacementId { get; private set; }
        public string UseName { get; private set; }
        public int UseId { get; private set; }
        public string GridName { get; private set; }
        public float GridGx { get; private set; } = -9999f;
        public float GridGz { get; private set; } = -9999f;
        public string HeldName { get; private set; }

        /// <summary>主动重算 FindNearbyObjects 后读交互句柄 + 手持物。按需调用（tap/hold）。</summary>
        public void RefreshInteraction()
        {
            if (Chef == null) return;
            Reflect.Call(Chef, "FindNearbyObjects");   // 主动重算：被动读会与当前站位错开一帧
            object io = Reflect.Field(Chef, "m_interactionObjects");
            if (io == null) io = Reflect.Field(Chef, "m_tmpInteractionObjects");
            if (io != null)
            {
                object inter = Reflect.Field(io, "m_interactable");
                UseName = NameOf(inter);
                UseId = IdOf(inter);
                var pickup = UnwrapReferral(Reflect.Field(io, "m_iHandlePickup"), "m_pickupReferralObject");
                ReadHandle(pickup, out float pgx, out float pgz, out string pname);
                PickupGx = pgx; PickupGz = pgz; PickupName = pname; PickupId = IdOf(pickup);
                var placement = UnwrapReferral(Reflect.Field(io, "m_iHandlePlacement"), "m_placementReferralObject");
                ReadHandle(placement, out float qgx, out float qgz, out string qname);
                PlacementGx = qgx; PlacementGz = qgz; PlacementName = qname; PlacementId = IdOf(placement);
                // 通用朝向句柄（面朝的那格）：盛饭/倒料这类端盘面对锅的交互，
                // pickup/placement 句柄可能被脚边散料抢走或为 null，只有
                // m_gridLocation 稳定指向面朝的目标。
                object grid = Reflect.Field(io, "m_gridLocation");
                GridName = NameOf(grid);
                GridGx = -9999f; GridGz = -9999f;
                if (grid is UnityEngine.Object ugo && ugo != null)
                {
                    object ggi = Reflect.Field(grid, "m_gridIndex");
                    if (ggi != null)
                    {
                        object ggx = Reflect.Field(ggi, "m_x");
                        object ggz = Reflect.Field(ggi, "m_z");
                        if (ggx != null && ggz != null) { GridGx = Reflect.ToFloat(ggx); GridGz = Reflect.ToFloat(ggz); }
                    }
                }
            }
            HeldName = ReadHeld();
        }

        private static string NameOf(object o)
        {
            if (o is UnityEngine.Object uo && uo != null) return uo.name;
            return null;
        }

        private static int IdOf(object o)
        {
            if (o is UnityEngine.Object uo && uo != null) return uo.GetInstanceID();
            return 0;
        }

        private static object UnwrapReferral(object referral, string fieldName)
        {
            if (referral == null) return null;
            object target = Reflect.Field(referral, fieldName);
            if (target is UnityEngine.Object uo && uo != null) return uo;
            return referral;
        }

        private static void ReadHandle(object handler, out float gx, out float gz, out string name)
        {
            name = NameOf(handler);
            gx = -9999f; gz = -9999f;
            GameObject go = handler as GameObject;
            if (go == null) { Component c = handler as Component; if (c != null) go = c.gameObject; }
            if (go == null) return;
            foreach (Component comp in go.GetComponentsInChildren<Component>(true))
            {
                if (comp == null) continue;
                object gi = Reflect.Field(comp, "m_gridIndex");
                if (gi == null) continue;
                object x = Reflect.Field(gi, "m_x");
                object z = Reflect.Field(gi, "m_z");
                if (x != null && z != null) { gx = Reflect.ToFloat(x); gz = Reflect.ToFloat(z); return; }
            }
        }

        private string ReadHeld()
        {
            object c = Reflect.Field(Chef, "m_clientCarrier");
            if (c == null) c = Reflect.Field(Chef, "m_serverCarrier");
            if (c == null) return null;
            object carried = Reflect.Field(c, "m_carriedObjects");
            if (carried is System.Collections.IEnumerable)
            {
                foreach (object e in (System.Collections.IEnumerable)carried)
                {
                    if (e == null) continue;
                    return NameOf(e);
                }
                return null;
            }
            return NameOf(c);
        }
    }

    /// <summary>move 命令：沿 waypoint 走到 goal 的连续偏差内。位置-only 到达。</summary>
    public sealed class MoveCommand : IExecCommand
    {
        // 初值（标定留实机）：走点推进 0.15、到达 0.5（半格 0.6 内都算「在交互格内」，
        // 精确对准交给 tap 的句柄校验）、stuck 0.8s、末段刹车 0.6、最小摇杆 0.35。
        private const float WAYPOINT_EPS = 0.15f;
        private const float ARRIVE_EPS = 0.5f;
        private const float INTERACT_EPS = 1.0f;
        private const float STUCK_TIME = 0.8f;
        private const float BRAKE_DIST = 0.6f;
        private const float MIN_STICK = 0.35f;

        private readonly List<Vector2> _path = new List<Vector2>();
        private readonly Vector2 _goal;
        private readonly Dictionary<string, object> _target;
        private readonly string _kind;
        private readonly float _timeout;
        private int _idx;
        private bool _started;
        private float _startTime;
        private float _lastProgress;
        private float _lastRelX;
        private float _lastRelZ;
        private bool _hasRel;

        public bool Done { get; private set; }
        public bool Ok { get; private set; }
        public string Why { get; private set; }
        public double[] BlockedCell { get; private set; }

        public MoveCommand(Dictionary<string, object> cmd)
        {
            _goal = ToVec(Req.List(cmd, "goal"));
            _target = Req.Obj(cmd, "target");
            _kind = Req.Str(cmd, "kind");
            var path = Req.List(cmd, "path");
            if (path != null)
                foreach (var p in path)
                {
                    var l = p as List<object>;
                    if (l != null) _path.Add(ToVec(l));
                }
            if (_path.Count == 0) _path.Add(_goal);
            // path[0] 是厨师自己所在的格（Python 从 chef_cell 回溯的起点），厨师
            // 已经在这里，不必朝自己格心走——尤其当厨师恰在格边界、chef_cell 四舍
            // 五入成相邻的墙格时，朝自己格心走会撞墙（实机 1-3：(-3,-5)/(-3,-6) 边界
            // 被四舍五入成底墙 (-3,-6)，朝格心向下撞墙）。跳过它，直接走 path[1]。
            _idx = 1;
            _timeout = (float)(Req.Num(cmd, "timeout_ms") ?? 8000.0) / 1000f;
        }

        private static Vector2 ToVec(List<object> l)
        {
            if (l == null || l.Count < 2) return Vector2.zero;
            return new Vector2((float)(double)l[0], (float)(double)l[1]);
        }

        // 靠近 goal 时用游戏交互句柄判「已就位」：句柄命中目标即算到达，
        // 不必卡在精确格心（散料/障碍挡住精确位置时仍能停住）。
        private bool InteractReady(ChefFrame f)
        {
            if (_target == null) return false;
            f.RefreshInteraction();
            return PadButtons.HandleMatches(f, _target, _kind);
        }

        public void Tick(ChefFrame f, float now)
        {
            if (Done) return;
            if (!_started)
            {
                _started = true; _startTime = now; _lastProgress = now;
                var sb = new System.Text.StringBuilder();
                sb.Append("[EXEC] move start goal=(").Append(_goal.x).Append(',').Append(_goal.y)
                  .Append(") pos=(").Append(f.PosX).Append(',').Append(f.PosZ).Append(") path=[");
                for (int i = 0; i < _path.Count && i < 40; i++)
                {
                    if (i > 0) sb.Append(',');
                    sb.Append('(').Append(_path[i].x.ToString("0.#")).Append(',')
                      .Append(_path[i].y.ToString("0.#")).Append(')');
                }
                if (_path.Count > 40) sb.Append("...");
                sb.Append(']');
                UnityEngine.Debug.Log(sb.ToString());
            }
            if (now - _startTime > _timeout) { Finish(false, "timeout"); return; }
            if (!f.Valid) { Finish(false, "no chef"); return; }

            Vector2 pos = new Vector2(f.PosX, f.PosZ);

            // 到达：以游戏交互句柄为准（权威信号）——句柄命中即「到了」，
            // 不再用距离卡最后一步。goalDist 只做两件事：性能闸（离 goal 太远
            // 先不查句柄，省 FindNearbyObjects）+ 无句柄目标时的距离兜底。
            float goalDist = Vector2.Distance(pos, _goal);
            if (goalDist <= INTERACT_EPS && InteractReady(f))
            {
                PadEngagement.Pad.Stop();
                Finish(true, null);
                return;
            }
            // 兜底：句柄对不上（grid=None / 无朝向信号）时，0.5 内也判到达。
            if (goalDist <= ARRIVE_EPS)
            {
                PadEngagement.Pad.Stop();
                Finish(true, null);
                return;
            }

            Vector2 target = _idx < _path.Count ? _path[_idx] : _goal;
            float dist = Vector2.Distance(pos, target);

            // 走点推进
            if (_idx < _path.Count && dist <= WAYPOINT_EPS)
            {
                _idx++;
                target = _idx < _path.Count ? _path[_idx] : _goal;
                dist = Vector2.Distance(pos, target);
                _hasRel = false;
            }

            // stuck：无进展超时 → FAILURE 交回 Python。planner 每拍用新 field 重算，
            // 会绕开障碍重新走；不在这里「跳 waypoint」——那一跳可能越过障碍、
            // 让角色朝障碍物撞（实机：1-3 关 (-3,-5) 朝绿植 (-3,-4) 撞）。
            // 用相对脚下平台的 rel 位置判「有没有真的在动」：站在移动平台（篮子）
            // 上时世界坐标被带漂，用世界坐标会把「平台在动」误判成「我在动」。
            float relMoved = _hasRel
                ? Mathf.Sqrt((f.RelX - _lastRelX) * (f.RelX - _lastRelX)
                             + (f.RelZ - _lastRelZ) * (f.RelZ - _lastRelZ))
                : 0f;
            if (!_hasRel || relMoved > 1e-3f)
            {
                _lastRelX = f.RelX;
                _lastRelZ = f.RelZ;
                _hasRel = true;
                _lastProgress = now;
            }
            else if (now - _lastProgress > STUCK_TIME)
            {
                UnityEngine.Debug.Log("[EXEC] move stuck pos=(" + f.PosX + "," + f.PosZ + ")"
                    + " goal=(" + _goal.x + "," + _goal.y + ")"
                    + " idx=" + _idx + "/" + _path.Count
                    + " target=(" + target.x + "," + target.y + ")");
                PadEngagement.Pad.Stop();
                Finish(false, "stuck", new double[] { target.x, target.y });
                return;
            }

            // 转向 target；末段线性减速（goalDist 已在上面算过）
            Vector2 dir = (target - pos).normalized;
            float mag = goalDist < BRAKE_DIST
                ? Mathf.Clamp(goalDist / BRAKE_DIST, MIN_STICK, 1f)
                : 1f;
            PadEngagement.Pad.Move(f.Chef, dir.x * mag, dir.y * mag);
        }

        private void Finish(bool ok, string why, double[] blockedCell = null)
        {
            Done = true;
            Ok = ok;
            Why = why;
            BlockedCell = blockedCell;
        }
    }
}

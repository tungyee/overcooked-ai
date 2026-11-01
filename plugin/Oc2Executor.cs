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

    /// <summary>
    /// 生料 prefab 名 → 配方节点名的改名表（鱼：Fish → UncookedFish）。
    ///
    /// 配方按 order-node 名写（PanFriedFish 要 UncookedFish），游戏里鱼箱出的生料
    /// prefab 叫 Fish、自身没有配方节点，切好的 ChoppedFish 才挂 UncookedFish 节点。
    /// 快照（箱子 ing / held / 砧板 item / items[].n）和 exec 句柄名都过这张表，
    /// Python 看到的是一套与配方一致的名字：「UncookedFish 箱 → 切成 ChoppedFish
    /// → 下锅」。参考 feat/throw-transport 的 MapRawName（Mine_2_5 实机跑通）。
    /// 快照与句柄必须同源，否则 Python 拿 UncookedFish 去比句柄名 Fish 永远不中。
    /// 由 Oc2AIPlugin 在建配方时填充、切关时清空；只在主线程读写。
    /// </summary>
    internal static class RawNames
    {
        private static readonly Dictionary<string, string> _map = new Dictionary<string, string>();

        public static void Clear() { _map.Clear(); }

        public static void Add(string raw, string node)
        {
            if (string.IsNullOrEmpty(raw) || string.IsNullOrEmpty(node)) return;
            _map[raw.ToLowerInvariant()] = node;
        }

        public static int Count { get { return _map.Count; } }

        public static string Map(string name)
        {
            if (string.IsNullOrEmpty(name) || _map.Count == 0) return name;
            string mapped;
            return _map.TryGetValue(name.ToLowerInvariant(), out mapped) ? mapped : name;
        }
    }

    /// <summary>
    /// 快照与 exec 句柄共用的对象命名：站点名全局唯一 + 生料改名（RawNames）。
    ///
    /// 游戏里有关卡把两个站点做成同名 GameObject（Wizard_3_1 两块砧板都叫
    /// countertop_01_chopping_board_gold）。Python 侧所有站点资源 id 都是
    /// (网格名, 站点名)、物品的 on_station 也是名字，同名会让两块板合成一块、
    /// 认领互撞。这里按 GameObject 名分组，**只给重名的那组**加 "#&lt;InstanceID&gt;"
    /// 后缀，不重名的站点名字不变（旧帧 / fixture / 测试不受影响）。
    ///
    /// 站点 = 挂了下列任一组件的 GameObject；每拍快照开头 Rebuild 一次（站点
    /// 会随阶段切换出现/消失，开销只是一遍 _all）。只在主线程读写。
    /// </summary>
    internal static class ObjNames
    {
        private static readonly HashSet<string> _stationTypes = new HashSet<string>
        {
            "StaticGridLocation", "DynamicGridLocation", "ServerAttachStation",
            "ServerWorkstation", "ServerCookingStation", "ServerHeatedCookingStation",
            "ServerPlateStation", "ServerWashingStation", "ConveyorStation",
        };

        private static readonly Dictionary<int, string> _unique = new Dictionary<int, string>();

        public static void Rebuild(MonoBehaviour[] all)
        {
            _unique.Clear();
            if (all == null) return;
            var byName = new Dictionary<string, List<GameObject>>();
            var seen = new HashSet<int>();
            foreach (MonoBehaviour mb in all)
            {
                if (mb == null || !_stationTypes.Contains(mb.GetType().Name)) continue;
                GameObject go = mb.gameObject;
                if (!seen.Add(go.GetInstanceID())) continue;
                List<GameObject> list;
                if (!byName.TryGetValue(go.name, out list))
                    byName[go.name] = list = new List<GameObject>();
                list.Add(go);
            }
            foreach (var kv in byName)
            {
                if (kv.Value.Count < 2) continue;
                foreach (GameObject go in kv.Value)
                    _unique[go.GetInstanceID()] = go.name + "#" + go.GetInstanceID();
            }
        }

        /// <summary>对象（GameObject / Component）的对外名字；null 返回 null。</summary>
        public static string Of(object o)
        {
            GameObject go = o as GameObject;
            if (go == null && o is Component c && c != null) go = c.gameObject;
            if (go == null)
                return o is UnityEngine.Object uo && uo != null ? RawNames.Map(uo.name) : null;
            string n;
            if (_unique.TryGetValue(go.GetInstanceID(), out n)) return n;
            return RawNames.Map(go.name);
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

        // 句柄名/手持名与快照同一套命名（生料改成配方节点名，见 RawNames）。
        private static string NameOf(object o)
        {
            return ObjNames.Of(o);
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
        // 卡住跳点（TrySkipWaypoint）：停滞 0.3s 起试、离当前点 1 格内才跳、试探距离 0.3m。
        private const float SKIP_STALL = 0.3f;
        private const float SKIP_MAX_DIST = 1.2f;
        private const float SKIP_PROBE = 0.3f;
        // 锚定射线：从厨师头顶往下打 Ground(9) 层，找 waypoint 脚下的地板。
        private const int GROUND_LAYER_MASK = 1 << 9;
        private const float ANCHOR_RAY_UP = 5f;
        private const float ANCHOR_RAY_LEN = 20f;

        // waypoint 锚点：Python 发的是发令时刻的世界坐标，但站在移动平台（气球篮/
        // 木筏）上时，平台上的点会被平台带走。冻结的世界坐标会落进平台台面里、
        // 永远够不到（实机 trace-20261029-234145 t=1905~1925：篮子漂 0.45m 后
        // waypoint 落进灶台，P2 顶着台面推、stuck）。所以启动时把每个点挂到它
        // 脚下那块地板 collider 的本地坐标系，每帧用该 transform 还原成**当前**
        // 世界坐标——平移、旋转都跟着走；静止地板的 transform 不动，等价旧行为。
        private struct Anchor
        {
            public Transform T;      // 脚下地板；null = 没打中，按冻结世界坐标
            public Vector3 Local;
            public Vector2 World;    // 发令时刻的世界坐标（兜底）
        }

        private readonly List<Vector2> _path = new List<Vector2>();
        private readonly List<Anchor> _anchors = new List<Anchor>();
        private Anchor _goalAnchor;
        private int _anchoredCount;
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

        // 平台接缝门洞（Oc2Gate.cs）：gates 字段可选，旧 Python 不发则行为不变。
        // 门洞没开时在缝口外停下等，最多 gate.MaxWait 秒（Python 按预测开门时间给，
        // 缺省 4s）；离缝口 GATE_HOLD 内才停。
        private const float GATE_HOLD = 0.6f;
        private readonly List<Gate> _gates = new List<Gate>();
        private Collider _body;
        private float _gateWaitSince = -1f;

        // 传送门过门段（Oc2Portals.cs PortalHop）：portals 字段可选，旧 Python 不发则不变。
        // 走到推门点附近就一直朝门推，被传走后停摇杆等落地，落地后从 exit 后的路点续走。
        // 推门推了 MaxWait 秒还没被传（门在传别人 / 冷却）→ portal busy。
        // 没有过门段时被传走（误踩）→ teleported，交回 Python 按新位置重算。
        private const float PORTAL_NEAR = 1.0f;     // 离推门点多近开始算「在推门」
        private const float PORTAL_LAND_EPS = 1.5f; // 落地点与预期出口落点的容差
        private readonly List<PortalHop> _hops = new List<PortalHop>();
        private bool _teleportSeen;
        private float _portalPushSince = -1f;

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
            _goalAnchor = new Anchor { T = null, Local = Vector3.zero, World = _goal };
            _timeout =(float)(Req.Num(cmd, "timeout_ms") ?? 8000.0) / 1000f;
            var gates = Req.List(cmd, "gates");
            if (gates != null)
                foreach (var g in gates)
                {
                    var d = g as Dictionary<string, object>;
                    if (d == null) continue;
                    var gate = new Gate(d);
                    if (gate.Valid && gate.Exit < _path.Count) _gates.Add(gate);
                }
            var hops = Req.List(cmd, "portals");
            if (hops != null)
                foreach (var h in hops)
                {
                    var d = h as Dictionary<string, object>;
                    if (d == null) continue;
                    var hop = new PortalHop(d);
                    if (hop.Valid(_path.Count)) _hops.Add(hop);
                }
        }

        // 下标 i 是哪段过门的推门点 / 落地点；不是返回 null。
        private PortalHop HopAt(int i, out bool isExit)
        {
            isExit = false;
            foreach (var h in _hops)
            {
                if (h.Enter == i) return h;
                if (h.Exit == i) { isExit = true; return h; }
            }
            return null;
        }

        // 传送中 / 刚落地的处理。返回 true = 本拍已处理完（调用方直接 return）。
        private bool TickTeleport(ChefFrame f, Vector2 pos, float now)
        {
            if (Oc2AIPlugin.ChefTeleporting(f.Chef))
            {
                if (!_teleportSeen)
                {
                    _teleportSeen = true;
                    UnityEngine.Debug.Log("[EXEC] move teleporting pos=(" + f.PosX + "," + f.PosZ + ") idx=" + _idx);
                }
                PadEngagement.Pad.Stop();
                _lastProgress = now;          // 传送动画不算 stuck
                _hasRel = false;
                return true;
            }
            if (!_teleportSeen) return false;
            _teleportSeen = false;
            _portalPushSince = -1f;
            bool isExit;
            PortalHop hop = HopAt(_idx, out isExit);
            if (hop != null && !isExit && Vector2.Distance(pos, hop.Land) <= PORTAL_LAND_EPS)
            {
                // 计划内过门：跳过落地点，朝下一个路点走
                _idx = hop.Exit + 1;
                _lastProgress = now;
                UnityEngine.Debug.Log("[EXEC] move portal landed pos=(" + f.PosX + "," + f.PosZ + ") portal=" + hop.Portal);
                return false;
            }
            // 计划外被传走（误踩 / 落点不对）：路点全部作废，交回 Python 重算
            UnityEngine.Debug.Log("[EXEC] move teleported (unplanned) pos=(" + f.PosX + "," + f.PosZ + ") idx=" + _idx);
            PadEngagement.Pad.Stop();
            Finish(false, "teleported");
            return true;
        }

        // 下标 i 是哪道门洞的入点/出点；不是返回 null。
        private Gate GateAt(int i, out bool isExit)
        {
            isExit = false;
            foreach (var g in _gates)
            {
                if (g.Enter == i) return g;
                if (g.Exit == i) { isExit = true; return g; }
            }
            return null;
        }

        private static Vector2 ToVec(List<object> l)
        {
            if (l == null || l.Count < 2) return Vector2.zero;
            return new Vector2((float)(double)l[0], (float)(double)l[1]);
        }

        // 往 p 正下方打 Ground 层射线，打中就挂到那块地板的 transform 上。
        // 用物理事实判「点在哪块平台上」，不靠 Python 侧按格猜网格（篮子边缘格
        // 量化会判错：地面点误挂篮子 → 被带去悬空处）。
        private static Anchor MakeAnchor(Vector2 p, float y)
        {
            Anchor a = new Anchor { T = null, Local = Vector3.zero, World = p };
            RaycastHit hit;
            Vector3 origin = new Vector3(p.x, y + ANCHOR_RAY_UP, p.y);
            if (Physics.Raycast(origin, Vector3.down, out hit, ANCHOR_RAY_LEN,
                                GROUND_LAYER_MASK, QueryTriggerInteraction.Ignore)
                && hit.collider != null)
            {
                a.T = hit.collider.transform;
                a.Local = a.T.InverseTransformPoint(new Vector3(p.x, hit.point.y, p.y));
            }
            return a;
        }

        // 锚点的当前世界坐标；地板被销毁/没打中时退回发令时刻的世界坐标。
        private static Vector2 Now(Anchor a)
        {
            if (a.T == null) return a.World;
            Vector3 w = a.T.TransformPoint(a.Local);
            return new Vector2(w.x, w.z);
        }

        private Vector2 PointAt(int i)
        {
            bool isExit;
            PortalHop hop = HopAt(i, out isExit);
            if (hop != null) return isExit ? hop.Land : hop.Push;
            Gate g = GateAt(i, out isExit);
            if (g != null) return isExit ? g.ExitPoint : g.EnterPoint;
            return i < _anchors.Count ? Now(_anchors[i]) : Now(_goalAnchor);
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
                // 锚定在首帧做（构造在 exec 线程收包时，不能碰物理/transform）。
                float y = f.Valid ? f.Chef.transform.position.y : 0f;
                _anchors.Clear();
                _anchoredCount = 0;
                foreach (Vector2 p in _path)
                {
                    Anchor a = MakeAnchor(p, y);
                    if (a.T != null) _anchoredCount++;
                    _anchors.Add(a);
                }
                _goalAnchor = MakeAnchor(_goal, y);
                if (_gates.Count > 0 && f.Valid) _body = Oc2AIPlugin.ChefBody(f.Chef);
                var sb = new System.Text.StringBuilder();
                sb.Append("[EXEC] move start goal=(").Append(_goal.x).Append(',').Append(_goal.y)
                  .Append(") goal_floor=").Append(_goalAnchor.T != null ? _goalAnchor.T.name : "none")
                  .Append(" anchored=").Append(_anchoredCount).Append('/').Append(_path.Count)
                  .Append(" pos=(").Append(f.PosX).Append(',').Append(f.PosZ).Append(") path=[");
                for (int i = 0; i < _path.Count && i < 40; i++)
                {
                    if (i > 0) sb.Append(',');
                    sb.Append('(').Append(_path[i].x.ToString("0.#")).Append(',')
                      .Append(_path[i].y.ToString("0.#")).Append(')');
                }
                if (_path.Count > 40) sb.Append("...");
                sb.Append(']');
                foreach (var g in _gates)
                    sb.Append(" gate(").Append(g.AlongX ? "x=" : "z=").Append(g.Line.ToString("0.##"))
                      .Append(" idx=").Append(g.Enter).Append(',').Append(g.Exit)
                      .Append(" mid=").Append(g.Mid.ToString("0.##")).Append(')');
                UnityEngine.Debug.Log(sb.ToString());
            }
            if (now - _startTime > _timeout) { Finish(false, "timeout"); return; }
            if (!f.Valid) { Finish(false, "no chef"); return; }

            Vector2 pos = new Vector2(f.PosX, f.PosZ);

            // 传送门：传送中停摇杆等落地；落地后续走或（计划外）交回 Python
            if (TickTeleport(f, pos, now)) return;

            // 到达：以游戏交互句柄为准（权威信号）——句柄命中即「到了」，
            // 不再用距离卡最后一步。goalDist 只做两件事：性能闸（离 goal 太远
            // 先不查句柄，省 FindNearbyObjects）+ 无句柄目标时的距离兜底。
            // goal / waypoint 都取锚点的**当前**世界坐标（随脚下平台走）。
            Vector2 goal = Now(_goalAnchor);
            float goalDist = Vector2.Distance(pos, goal);
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

            // 过门段：当前路点是推门点 → 一直朝门推（不按距离推进），等门把人传走
            bool hopIsExit;
            PortalHop hopNow = HopAt(_idx, out hopIsExit);
            if (hopNow != null && !hopIsExit)
            {
                if (Vector2.Distance(pos, hopNow.Push) <= PORTAL_NEAR)
                {
                    if (_portalPushSince < 0f)
                    {
                        _portalPushSince = now;
                        UnityEngine.Debug.Log("[EXEC] move portal push pos=(" + f.PosX + "," + f.PosZ
                            + ") portal=" + hopNow.Portal + " max_wait=" + hopNow.MaxWait);
                    }
                    _lastProgress = now;      // 顶着门等不算 stuck
                    _hasRel = false;
                    if (now - _portalPushSince > hopNow.MaxWait)
                    {
                        UnityEngine.Debug.Log("[EXEC] move portal busy after "
                            + (now - _portalPushSince).ToString("0.0") + "s portal=" + hopNow.Portal);
                        PadEngagement.Pad.Stop();
                        Finish(false, "portal busy", new double[] { hopNow.Push.x, hopNow.Push.y });
                        return;
                    }
                }
                Vector2 pd = (hopNow.Push - pos).normalized;
                PadEngagement.Pad.Move(f.Chef, pd.x, pd.y);
                return;
            }

            // 门洞：当前或下一个路点是门洞入/出点时，按墙碰撞体现算中线
            bool atExit;
            Gate gate = GateAt(_idx, out atExit);
            if (gate == null && _idx + 1 < _path.Count)
            {
                Gate ahead = GateAt(_idx + 1, out atExit);
                if (ahead != null && !atExit) { gate = ahead; atExit = false; }
            }
            bool gateOpen = gate == null || gate.Refresh(_body, now);

            // 已到缝口等待范围：入点前一个路点只是「走到缝口」的过渡，直接算到了。
            // 等门范围按缝口（不随篮子动）量，P2 和这个路点却都在篮子上：还差 0.4m 没到
            // 它就已停下等门，等门时被篮子带出等门范围，门没开就结束等待，接着去够这个
            // 路点（trace-20261030-213754 t=1752~1780：P2 相对篮子一直 (-1.42,-1.12) 没动，
            // 朝 (14.4,-12.14) 推、盘子顶到篮子上的箱子 stuck，送餐失败）。改成以入点为
            // 目标后，被带出范围会走回缝口继续等。
            if (gate != null && !atExit && _idx + 1 == gate.Enter
                && Vector2.Distance(pos, gate.EnterPoint) <= GATE_HOLD + WAYPOINT_EPS)
            {
                _idx = gate.Enter;
                _hasRel = false;
            }

            Vector2 target = PointAt(_idx);
            float dist = Vector2.Distance(pos, target);

            // 走点推进；门洞入/出点按跨缝坐标判（沿缝坐标一直在动，按距离可能永远差一点）
            bool reached = dist <= WAYPOINT_EPS;
            if (gate != null && _idx == gate.Enter) reached = reached || (gateOpen && gate.PastEnter(pos));
            if (gate != null && _idx == gate.Exit) reached = reached || gate.PastExit(pos);
            if (_idx < _path.Count && reached)
            {
                _idx++;
                target = PointAt(_idx);
                dist = Vector2.Distance(pos, target);
                _hasRel = false;
            }

            // 门洞没开、还没进缝：走到缝口附近就停下等，不顶墙
            if (gate != null && !gateOpen && _idx <= gate.Enter
                && Vector2.Distance(pos, gate.EnterPoint) <= GATE_HOLD + WAYPOINT_EPS)
            {
                if (_gateWaitSince < 0f)
                {
                    _gateWaitSince = now;
                    UnityEngine.Debug.Log("[EXEC] move gate wait pos=(" + f.PosX + "," + f.PosZ + ") line="
                        + gate.Line + " mid=" + gate.Mid + " max_wait=" + gate.MaxWait + " blocker=" + gate.Blocker);
                }
                PadEngagement.Pad.Stop();
                _lastProgress = now;          // 等门不算 stuck
                _hasRel = false;
                if (now - _gateWaitSince > gate.MaxWait)
                {
                    // 回传门洞格（接缝线上的中线点）：Python 记 bad_cells 绕开，防止
                    // 插件判关、Python 判通时反复重发同一条 move 卡死在缝口。
                    Vector2 g = gate.AlongX ? new Vector2(gate.Line, gate.Mid) : new Vector2(gate.Mid, gate.Line);
                    UnityEngine.Debug.Log("[EXEC] move gate closed after " + (now - _gateWaitSince).ToString("0.0")
                        + "s blocker=" + gate.Blocker);
                    PadEngagement.Pad.Stop();
                    Finish(false, "gate closed", new double[] { g.x, g.y });
                }
                return;
            }
            _gateWaitSince = -1f;

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
            else if (now - _lastProgress > SKIP_STALL && TrySkipWaypoint(f, pos, dist, now))
            {
                target = PointAt(_idx);
            }
            else if (now - _lastProgress > STUCK_TIME)
            {
                UnityEngine.Debug.Log("[EXEC] move stuck pos=(" + f.PosX + "," + f.PosZ + ")"
                    + " goal=(" + goal.x + "," + goal.y + ")"
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

        // 中途路点够不到时提前转向下一个点。手里的东西挂在厨师刚体下面，碰撞体还开着
        // （ServerPhysicalAttachment.Attach 只换父节点），所以伸在身前的盘子/食材
        // 会先顶到台面。Python 和 walk_probe 只按身体胶囊判，看到的是「能走」
        // （trace-20261030-192949 t=1804~1832：端盘朝 (12,-10.8) 推，身体离燃烧器
        // 还有 0.5m，被盘子顶在 z=-11.43；开局 t=258 拿意面在同一处卡在 -11.31）。
        // 卡住时用 Rigidbody.SweepTest 按整个刚体（身体 + 手持物）试探朝下一个点走
        // 有没有挡，没挡就跳过当前点。门洞的入/出点和终点不跳。
        private bool TrySkipWaypoint(ChefFrame f, Vector2 pos, float dist, float now)
        {
            if (_idx + 1 >= _path.Count || dist > SKIP_MAX_DIST) return false;
            bool isExit;
            if (GateAt(_idx, out isExit) != null || GateAt(_idx + 1, out isExit) != null) return false;
            if (HopAt(_idx, out isExit) != null || HopAt(_idx + 1, out isExit) != null) return false;
            Rigidbody rb = f.Chef.GetComponent<Rigidbody>();
            if (rb == null) return false;
            Vector2 next = PointAt(_idx + 1);
            Vector2 d = next - pos;
            if (d.sqrMagnitude < 1e-6f) return false;
            Vector3 dir = new Vector3(d.x, 0f, d.y).normalized;
            RaycastHit hit;
            if (rb.SweepTest(dir, out hit, SKIP_PROBE, QueryTriggerInteraction.Ignore)) return false;
            UnityEngine.Debug.Log("[EXEC] move skip waypoint pos=(" + f.PosX + "," + f.PosZ + ")"
                + " idx=" + _idx + " target=(" + PointAt(_idx).x + "," + PointAt(_idx).y + ")"
                + " next=(" + next.x + "," + next.y + ")");
            _idx++;
            _lastProgress = now;
            _hasRel = false;
            return true;
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

using System;
using System.Collections;
using System.Collections.Generic;
using System.Net;
using System.Net.Sockets;
using System.Reflection;
using System.Text;
using System.Threading;
using BepInEx;
using UnityEngine;

namespace Oc2AI
{
    [BepInPlugin("com.oc2.aiplaymate", "OC2 AI Playmate", "0.19.0")]
    public partial class Oc2AIPlugin : BaseUnityPlugin
    {
        const int Port = 27411;

        MonoBehaviour[] _all;
        bool _inputDumped;
        bool _orderFieldsDumped;
        bool _discoveryDone;
        bool _terrainFilterLogged;
        string _staticJson;
        string _level;
        int _staticTries;
        int _droppedStates;
        // chop 索引的单调缓存（食材名小写 → [切成什么, 几刀]）。Dynamic 关卡里
        // 鱼/米/海苔开局后才刷出，一次性 BuildStatic 会漏掉；每帧把新扫到的
        // 食材合并进来，一旦出现新键就让 recipes 重建。只增不减：食材类型一旦
        // 见过就保留，不因实例被切掉/用掉而消失。
        Dictionary<string, string[]> _chopIndexCache = new Dictionary<string, string[]>();

        // chop 索引低频重扫的收敛计数：连续 3 次无新键就停止（见 RescanChopIndex）。
        int _chopNoNew = 0;

        // 站点名 → grid 名的缓存（关卡静态数据）。BuildStationGridMap 每帧被
        // plates/items 各调一次，每次对 _all 全组件做 GridName（内含
        // GetComponentsInChildren 递归），Balloon 关 2410 组件上拖到 290ms×2。
        // 缓存后在 RefreshLevel 切关时失效重建。
        Dictionary<string, string> _stationGridCache;

        // P2 的 WorkstationInteract(chop/投掷) 键按住时长监控：用于实测
        // 「按住是否影响投掷距离」。-1 = 未按住；>=0 = 当前按住起始的 unscaledTime。
        float _chopDownSince = -1f;
        float _chopHoldMs = 0f;

        // 网格映射（world -> gx/gz）。出餐口等功能站没有 m_gridIndex，需要
        // 用 StaticGridLocation 反解原点后，把世界坐标换算成网格坐标。
        float _cellX = 1.2f;
        float _cellZ = 1.2f;
        float _originX = 14.4f;
        float _originZ = -6.0f;
        bool _gridReady = false;
        // 主网格管理器（CalibrateGrid 里确定）。功能站用它判断该走主网格投影
        // 还是非主网格（气球篮）的本地投影。
        UnityEngine.Object _mainGrid = null;

        TcpListener _listener;
        Thread _acceptThread;
        Thread _writeThread;
        readonly object _lock = new object();
        readonly List<TcpClient> _clients = new List<TcpClient>();
        readonly Queue<string> _out = new Queue<string>();

        // exec 执行通道（27412）：入站命令 + 结果回写 + 逐帧执行循环（见 Oc2ExecServer.cs）。
        ExecServer _execServer;

        void Awake()
        {
            Logger.LogInfo("[OC2AI v0.19.0 Windows upstream] loaded");
            _discoverEnabled = Config.Bind<bool>("Diagnostics", "DiscoverComponents", false, "Expensive discovery; disabled for normal play.");
            StartSocket();
            _execServer = new ExecServer();
            _execServer.Start();
            Invoke("Snapshot", 3f);   // 之后由 Snapshot 自己按关卡内外决定下次间隔
            Invoke("RescanChopIndex", 2f);   // 延迟刷出食材的低频重扫（收敛后自动停止）
        }

        void Update()
        {
            // 帧耗时统计（LocalRuntime 拆出来的性能监控，rebase 合并回主 Update）。
            double ms = Time.unscaledDeltaTime * 1000.0;
            _frameCount++; _frameSum += ms; _frameMax = Math.Max(_frameMax, ms);
            if (ms > 33.34) _slowFrames++;
            // exec 执行通道：每帧 attach/engage（只在主菜单成功，进入关卡后 self-guard
            // 直接返回）+ tick 活跃命令（move/tap/... 逐帧状态机）。
            PadEngagement.TryEngage();
            _execServer.Tick();
        }

        void Snapshot()
        {
            // 关卡内 5 Hz；非关卡降到 1 Hz，只做「进关了吗」的探测。
            float next = 0.2f;
            lock (_lock)
                if (_clients.Count == 0) { Invoke("Snapshot", 0.5f); return; }
            var watch = System.Diagnostics.Stopwatch.StartNew();
            try
            {
                // 每拍重扫，避免新出现的灭火器/掉落物延迟 1s 才进状态
                _all = UnityEngine.Object.FindObjectsOfType<MonoBehaviour>();
                if (!_inputDumped) { _inputDumped = true; LogInput(); }
                if (IsInLevel())
                {
                    RefreshLevel();
                    if (_discoverEnabled.Value && !_discoveryDone) { _discoveryDone = true; Discover(); }
                    string state = BuildState();
                    // 不再把整帧状态写进游戏日志：这是卡顿大户，决策复盘走 Python 侧
                    // trace-*.jsonl，不依赖这里的 [STATE] 日志。
                    Push(state);
                    SavePerformance(watch);
                }
                else
                {
                    // 非关卡（菜单 / 选关 / 加载中）：不序列化、不推帧，且只低频
                    // 探测。BuildState + 全量 LogInfo 每 0.2s 会把游戏卡到选关页
                    // 掉帧（实机 150% CPU）；FindObjectsOfType + FindMB 反射也降到
                    // 1 Hz，把非关卡开销压到最小。
                    next = 1.0f;
                }
            }
            catch (Exception e)
            {
                Logger.LogWarning("[STATE] error: " + e.Message);
                next = 0.2f;   // 出错保持高频，尽快重试
            }
            Invoke("Snapshot", next);
        }

        // ---------- socket ----------
        void StartSocket()
        {
            try
            {
                _listener = new TcpListener(IPAddress.Loopback, Port);
                _listener.Start();
                _acceptThread = new Thread(AcceptLoop) { IsBackground = true };
                _acceptThread.Start();
                _writeThread = new Thread(WriteLoop) { IsBackground = true };
                _writeThread.Start();
                Logger.LogInfo("[SOCKET] listening 127.0.0.1:" + Port);
            }
            catch (Exception e) { Logger.LogWarning("[SOCKET] start fail: " + e.Message); }
        }

        void AcceptLoop()
        {
            while (!_stopping)
            {
                try { TcpClient c = _listener.AcceptTcpClient(); c.NoDelay = true; c.SendTimeout = 250; lock (_lock) _clients.Add(c); }
                catch { return; }
            }
        }

        void WriteLoop()
        {
            while (!_stopping)
            {
                string msg = null;
                TcpClient[] clients;
                lock (_lock)
                {
                    if (_out.Count > 0) msg = _out.Dequeue();
                    clients = _clients.ToArray();
                }
                if (msg == null) { Thread.Sleep(20); continue; }
                byte[] data = Encoding.UTF8.GetBytes(msg);
                foreach (TcpClient c in clients)
                {
                    try { c.GetStream().Write(data, 0, data.Length); }
                    catch
                    {
                        lock (_lock) _clients.Remove(c);
                        try { c.Close(); } catch { }
                    }
                }
            }
        }

        void Push(string json)
        {
            lock (_lock)
            {
                _out.Clear();
                _out.Enqueue(json + "\n");
            }
        }

        // ---------- 输入枚举 ----------
        void LogInput()
        {
            try
            {
                Type lib = FindType("LogicalButtonID");
                if (lib != null) Logger.LogInfo("[INPUT] actions=" + string.Join(",", Enum.GetNames(lib)));
            }
            catch (Exception e) { Logger.LogWarning("[INPUT] error: " + e.Message); }
        }

        Type FindType(string simpleName)
        {
            foreach (Assembly a in AppDomain.CurrentDomain.GetAssemblies())
            {
                Type t = a.GetType(simpleName, false);
                if (t != null) return t;
            }
            return null;
        }

        bool IsInLevel()
        {
            // 判据用 FindServerFlowController()（同时认 Campaign 与 Dynamic 两套
            // 流程控制器类名），而不是 PlayerControls.m_levelConfigBase：
            // 流程控制器是关卡场景对象，退出到选关页时随关卡销毁 → 返回 null，
            // 选关页据此正确早退、不再每 0.2s 做全量 BuildState 序列化（否则选关页
            // 掉帧卡顿，见 Snapshot() 里的注释）。m_levelConfigBase 是配置资产引用，
            // 退出关卡后不一定被清空，会误判成"仍在关卡内"。
            return FindServerFlowController() != null;
        }

        string ReadLevel()
        {
            MonoBehaviour pc = FindMB("PlayerControls");
            object lv = GetField(pc, "m_levelConfigBase");
            if (lv is UnityEngine.Object uo && uo != null) return uo.name;
            return null;
        }

        // 关卡切换时重置缓存。_level / _staticJson 一旦非空就不再重读，
        // 不检测变化会导致切关后仍报上一关的关卡名/箱子/配方（实机复现：
        // 第四关加载后切到第一关，stations 还是黄瓜海苔鱼米，订单却是虾鱼）。
        void RefreshLevel()
        {
            string cur = ReadLevel();
            if (cur == null) return;      // 菜单/加载中：读不到关卡，保持原值
            MonoBehaviour flow = FindServerFlowController();
            int flowId = flow != null ? flow.GetInstanceID() : 0;
            if (cur != _level || flowId != _flowId)
            {
                _level = cur;
                _flowId = flowId; _sceneEpoch++;
                _staticJson = null;       // 下一拍 BuildState() 会重建 stations/recipes
                // 切关重启延迟刷出食材的低频重扫（上一关可能已收敛停止）。
                _chopNoNew = 0;
                CancelInvoke("RescanChopIndex");
                Invoke("RescanChopIndex", 2f);
                // 网格校准也要重来：_originX/Z 是从上一关的 StaticGridLocation
                // 反解出来的，切关后仍沿用会让功能站（出餐口/洗碗池）的 gx/gz
                // 全错（实机：1-4 切到 1-3，出餐口被算成 -13,8 而非 -6,-4）。
                _gridReady = false;
                _originX = 0f;
                _originZ = 0f;
                _mainGrid = null;
                _stationGridCache = null;   // 站点→grid 缓存随关卡失效
            }
        }

        // ---------- 字段发现(一次性) ----------
        void Discover()
        {
            try
            {
                MonoBehaviour pc = FindMB("PlayerControls");
                if (pc != null) DumpFields("PlayerControls", pc);

                MonoBehaviour ws = FindMB("ServerWorkstation");
                if (ws == null) ws = FindMB("ClientWorkstation");
                if (ws != null)
                {
                    DumpFields("Workstation", ws);
                    object item = GetField(ws, "m_item");
                    if (item != null) DumpFields("BoardItem", item);
                    object data = GetField(ws, "m_data");
                    if (data != null) DumpFields("WorkstationData", data);
                }

                MonoBehaviour plate = FindMB("ServerPlate");
                if (plate != null)
                {
                    DumpFields("ServerPlate", plate);
                    object ingr = GetField(plate, "m_ingredientContainer");
                    if (ingr != null) DumpFields("IngredientContainer", ingr);
                }

                MonoBehaviour gns = FindMB("GridNavSpace");
                if (gns != null) DumpFields("GridNavSpace", gns);

                // 网格管理器：可能含传送带/导航边数据
                MonoBehaviour gmgr = FindMB("GridManager");
                if (gmgr != null) DumpFields("GridManager", gmgr);
                MonoBehaviour qgm = FindMB("QuadGridManager");
                if (qgm != null) DumpFields("QuadGridManager", qgm);

                DumpOccupancy();

                // 手持物(非空时)
                object held = Held(pc);
                if (held != null) DumpFields("HeldItem", held);

                // 最终菜节点(CompositeOrderNode)的组成
                object ao = GetField(GetField(GetField(FindMB("ServerCampaignFlowController"), "m_teamMonitor"), "m_ordersController"), "m_activeOrders");
                if (ao is IEnumerable)
                {
                    foreach (object o in (IEnumerable)ao)
                    {
                        object order = GetField(GetField(o, "RecipeListEntry"), "m_order");
                        if (order != null) { DumpFields("CompositeOrderNode", order); break; }
                    }
                }

                // 烹饪台(找 cook 进度字段)
                foreach (MonoBehaviour mb in _all)
                {
                    if (mb == null) continue;
                    string tn = mb.GetType().Name.ToLowerInvariant();
                    if (tn.IndexOf("cookingstation") >= 0 || tn.IndexOf("heatedcooking") >= 0 || tn.IndexOf("cooker") >= 0)
                    {
                        DumpFields("CookingStation:" + mb.GetType().Name, mb);
                        break;
                    }
                }

                // 锅对象(m_itemPot)字段 dump：确认锅是哪种类型、料挂在哪
                foreach (MonoBehaviour mb in _all)
                {
                    if (mb == null) continue;
                    string tn = mb.GetType().Name;
                    if (tn.IndexOf("CookingStation") < 0) continue;
                    object pot = GetField(mb, "m_itemPot");
                    if (pot != null)
                    {
                        DumpFields("PotItem:" + pot.GetType().Name, pot);
                        break;
                    }
                }

                // 所有 MonoBehaviour 类型(去重计数) —— 找传送带/移动平台组件
                Dictionary<string, int> typeCount = new Dictionary<string, int>();
                foreach (MonoBehaviour mb in _all)
                {
                    if (mb == null) continue;
                    string tn = mb.GetType().Name;
                    if (!typeCount.ContainsKey(tn)) typeCount[tn] = 0;
                    typeCount[tn]++;
                }
                List<string> typeNames = new List<string>(typeCount.Keys);
                typeNames.Sort();
                StringBuilder types = new StringBuilder("[TYPES] ");
                foreach (string tn in typeNames)
                    types.Append(tn).Append('*').Append(typeCount[tn]).Append(' ');
                Logger.LogInfo(types.ToString());

                // 传送带/传送门(连接 nodeMap 中不连通的区域)
                StringBuilder portals = new StringBuilder("[PORTALS] ");
                foreach (MonoBehaviour mb in _all)
                {
                    if (mb == null) continue;
                    string tn = mb.GetType().Name;
                    string tl = tn.ToLowerInvariant();
                    if (tl.IndexOf("convey") < 0 && tl.IndexOf("teleport") < 0 && tl.IndexOf("movingplatform") < 0) continue;
                    Vector3 p = mb.transform.position;
                    portals.Append(tn).Append('[').Append(mb.name).Append("](")
                           .Append(p.x.ToString("0.0")).Append(',').Append(p.z.ToString("0.0")).Append(") ");
                }
                Logger.LogInfo(portals.ToString());

                // 台面传送带：位置 + 完整字段(前3个实例)重建连接关系
                int conveyDumped = 0;
                int conveyFullDumped = 0;
                foreach (MonoBehaviour mb in _all)
                {
                    if (mb == null) continue;
                    string tn = mb.GetType().Name;
                    if (tn.IndexOf("ConveyenceReceiver") < 0) continue;
                    Vector3 p = mb.transform.position;
                    Logger.LogInfo("[CONVEY] " + tn + "[" + mb.name + "] pos=("
                        + p.x.ToString("0.0") + "," + p.z.ToString("0.0") + ")");
                    if (conveyFullDumped < 3)
                    {
                        DumpFields("CONVEYFULL:" + tn, mb);
                        conveyFullDumped++;
                    }
                    conveyDumped++;
                }

                // ConveyorStation(传送带配置:方向+速度)，含 inactive
                try
                {
                    Type convType = FindType("ConveyorStation");
                    if (convType != null)
                    {
                        UnityEngine.Object[] all = UnityEngine.Resources.FindObjectsOfTypeAll(convType);
                        StringBuilder convs = new StringBuilder("[CONVSTATION] count=" + all.Length + " ");
                        foreach (UnityEngine.Object o in all)
                        {
                            MonoBehaviour mb = o as MonoBehaviour;
                            if (mb == null) continue;
                            Vector3 p = mb.transform.position;
                            object dir = GetField(mb, "m_conveyanceDirectionXZ");
                            object spd = GetField(mb, "m_conveySpeed");
                            convs.Append(mb.name).Append(" pos=(").Append(p.x.ToString("0.0")).Append(',').Append(p.z.ToString("0.0")).Append(')')
                                 .Append(" dir=").Append(dir).Append(" spd=").Append(spd)
                                 .Append(" enabled=").Append(mb.enabled)
                                 .Append(" activeSelf=").Append(mb.gameObject.activeSelf).Append(" | ");
                        }
                        Logger.LogInfo(convs.ToString());
                    }
                }
                catch (Exception e) { Logger.LogWarning("[CONVSTATION] error: " + e.Message); }

                // 关卡碰撞体全量 dump：P2 卡住时撞到的墙通常不是工作站，
                // station collider 列表里没有，需要把所有非触发 Collider 的
                // AABB 一起报出来，才能定位「墙上还是传送带/风」。
                DumpLevelColliders();

                // 配方节点类型 dump：每种类型 dump 第一个实例的完整字段（找切菜字段）
                try
                {
                    object rl = null;
                    foreach (string nm in new[] { "ServerCampaignFlowController", "ClientCampaignFlowController", "CampaignFlowController" })
                    {
                        MonoBehaviour fc = FindMB(nm);
                        if (fc == null) continue;
                        object l = GetField(fc, "m_cachedRecipeList");
                        if (l is IEnumerable && Count(l) > 0) { rl = l; break; }
                    }
                    if (rl is IEnumerable)
                    {
                        List<string> seenTypes = new List<string>();
                        int dumpedTypes = 0;
                        foreach (object node in (IEnumerable)rl)
                        {
                            if (node == null) continue;
                            string tn = node.GetType().Name;
                            if (tn == null || seenTypes.Contains(tn)) continue;
                            seenTypes.Add(tn);
                            DumpFields("RecipeNode:" + tn, node);
                            if (++dumpedTypes >= 8) break;
                        }
                    }
                }
                catch (Exception e) { Logger.LogWarning("[RECIPENODE] error: " + e.Message); }

                // 切菜信息：dump 切菜台 m_workable 及其组件 + 反射列出所有 Chop 相关类型
                try
                {
                    MonoBehaviour ws2 = FindMB("ServerWorkstation");
                    if (ws2 != null)
                    {
                        object workable = GetField(ws2, "m_workable");
                        if (workable != null)
                        {
                            DumpFields("Workable", workable);
                            if (workable is Component wc)
                            {
                                foreach (Component c in wc.GetComponents<Component>())
                                {
                                    if (c == null) continue;
                                    DumpFields("WorkableComp:" + c.GetType().Name, c);
                                }
                            }
                        }
                    }
                    StringBuilder chopTypes = new StringBuilder("[CHOPTYPES] ");
                    foreach (Assembly a in AppDomain.CurrentDomain.GetAssemblies())
                    {
                        Type[] ts = null;
                        try { ts = a.GetTypes(); } catch { continue; }
                        foreach (Type t in ts)
                        {
                            if (t == null) continue;
                            if (t.Name.ToLowerInvariant().IndexOf("chop") < 0) continue;
                            chopTypes.Append(t.Name).Append(' ');
                        }
                    }
                    Logger.LogInfo(chopTypes.ToString());
                }
                catch (Exception e) { Logger.LogWarning("[CHOP] error: " + e.Message); }
            }
            catch (Exception e) { Logger.LogWarning("[DISCOVER] error: " + e.Message); }
        }

        void DumpOccupancy()
        {
            try
            {
                MonoBehaviour gm = FindMB("QuadGridManager");
                if (gm == null) gm = FindMB("GridManager");
                if (gm == null) return;
                object occ = GetField(gm, "m_gridOccupancy");
                if (!(occ is IEnumerable)) return;
                StringBuilder sb = new StringBuilder("[OCCUPANCY] ");
                int i = 0;
                foreach (object e in (IEnumerable)occ)
                {
                    object key = GetProp(e, "Key");
                    sb.Append(key == null ? "?" : key.ToString()).Append(' ');
                    i++;
                    if (i >= 120) break;
                }
                Logger.LogInfo(sb.ToString());
            }
            catch (Exception e) { Logger.LogWarning("[OCCUPANCY] error: " + e.Message); }
        }

        int AppendColliderLine(StringBuilder sb, List<string> seen, Component c, int n, int cap)
        {
            if (c == null || n >= cap) return n;
            object trigger = GetProp(c, "isTrigger");
            if (trigger is bool t && t) return n;   // 触发体不挡人
            object bounds = GetProp(c, "bounds");
            if (bounds == null) return n;
            object mn = GetProp(bounds, "min");
            object mx = GetProp(bounds, "max");
            if (mn == null || mx == null) return n;
            object ax = GetField(mn, "x"), az = GetField(mn, "z");
            object bx = GetField(mx, "x"), bz = GetField(mx, "z");
            if (ax == null || az == null || bx == null || bz == null) return n;
            float axf = (float)ax, azf = (float)az, bxf = (float)bx, bzf = (float)bz;
            if (bxf <= axf || bzf <= azf) return n;
            float cx = (axf + bxf) / 2f, cz = (azf + bzf) / 2f;
            if (cx < -4f || cx > 17f || cz < 0f || cz > 13f) return n;
            string key = c.name + "|" + c.GetType().Name + "|"
                + axf.ToString("0.0") + "," + azf.ToString("0.0")
                + "," + bxf.ToString("0.0") + "," + bzf.ToString("0.0");
            if (seen.Contains(key)) return n;
            seen.Add(key);
            sb.Append(c.name).Append('[').Append(c.GetType().Name).Append("](")
              .Append(axf.ToString("0.0")).Append(',').Append(azf.ToString("0.0"))
              .Append(")-(").Append(bxf.ToString("0.0")).Append(',').Append(bzf.ToString("0.0"))
              .Append(") ");
            return n + 1;
        }

        void DumpLevelColliders()
        {
            try
            {
                StringBuilder sb = new StringBuilder("[COLLIDERS] ");
                List<string> seen = new List<string>();
                int n = 0;

                // 方案A：扫场景所有 GameObject 直接挂的 collider（含纯 GameObject 墙）。
                // FindObjectsOfType(Collider) 之前返回空（类型转发到 PhysicsModule 后
                // 反射拿不到子类），改扫 GameObject 再逐个看它的 collider 组件最稳。
                Type goType = FindType("UnityEngine.GameObject");
                if (goType == null) goType = FindType("GameObject");
                MethodInfo mi = typeof(UnityEngine.Object).GetMethod(
                    "FindObjectsOfType", BindingFlags.Public | BindingFlags.Static,
                    null, new Type[] { typeof(Type) }, null);
                object[] gos = (mi != null && goType != null)
                    ? (object[])mi.Invoke(null, new object[] { goType })
                    : null;
                int goCount = gos == null ? -1 : gos.Length;
                if (gos != null && gos.Length > 0)
                {
                    foreach (object o in gos)
                    {
                        GameObject go = o as GameObject;
                        if (go == null) continue;
                        foreach (Component c in go.GetComponents<Component>())
                        {
                            if (c == null) continue;
                            if (c.GetType().Name.ToLowerInvariant().IndexOf("collider") < 0) continue;
                            n = AppendColliderLine(sb, seen, c, n, 2000);
                            if (n >= 2000) break;
                        }
                        if (n >= 2000) break;
                    }
                }

                // 方案B（兜底）：扫所有 MonoBehaviour 子物体。
                if (n == 0)
                {
                    foreach (MonoBehaviour mb in _all)
                    {
                        if (mb == null) continue;
                        foreach (Component c in mb.GetComponentsInChildren<Component>(true))
                        {
                            if (c == null) continue;
                            if (c.GetType().Name.ToLowerInvariant().IndexOf("collider") < 0) continue;
                            n = AppendColliderLine(sb, seen, c, n, 2000);
                            if (n >= 2000) break;
                        }
                        if (n >= 2000) break;
                    }
                }

                sb.Insert("[COLLIDERS] ".Length, "gos=" + goCount + " n=" + n + " ");
                Logger.LogInfo(sb.ToString());
            }
            catch (Exception e) { Logger.LogWarning("[COLLIDERS] error: " + e.Message); }
        }

        void DumpFields(string label, object o)
        {
            if (o == null) return;
            StringBuilder sb = new StringBuilder("[DISC:" + label + "] " + o.GetType().FullName + " : ");
            for (Type t = o.GetType(); t != null && t != typeof(object) && t != typeof(Component); t = t.BaseType)
            {
                foreach (FieldInfo f in t.GetFields(BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic))
                {
                    object v = null;
                    try { v = f.GetValue(o); } catch { }
                    sb.Append(f.Name).Append('=').Append(ShortVal(v)).Append(" | ");
                }
            }
            Logger.LogInfo(sb.ToString());
        }

        string ShortVal(object v)
        {
            if (v == null) return "null";
            if (v is UnityEngine.Object uo) return uo == null ? "null" : uo.name;
            Type t = v.GetType();
            if (t.IsPrimitive || v is string || v is Enum) return v.ToString();
            if (v is IEnumerable)
            {
                int c = 0; StringBuilder s = new StringBuilder("{");
                foreach (object e in (IEnumerable)v)
                {
                    if (c > 0) s.Append(',');
                    s.Append(e == null ? "null" : (e is UnityEngine.Object uo2 ? uo2.name : e.GetType().Name));
                    c++; if (c >= 8) break;
                }
                s.Append(c >= 8 ? ",..." : "").Append('}');
                return s.ToString();
            }
            return t.Name;
        }

        // ---------- 反射工具 ----------
        MonoBehaviour FindMB(string typeName)
        {
            foreach (MonoBehaviour mb in _all)
            {
                if (mb == null) continue;
                if (mb.GetType().Name == typeName) return mb;
            }
            return null;
        }

        // 服务端流程控制器：Campaign 系列是 ServerCampaignFlowController，而
        // Dynamic 系列（1_6_Dynamic_Lvl_01_2P）是它的子类 ServerDynamicFlowController。
        // FindMB 按类名精确匹配会漏掉子类，导致 Dynamic 关 Phase() 恒为 "menu"、
        // orders/score/timer 全 null（实机：1-6 角色一动不动）。这里把两者都认。
        MonoBehaviour FindServerFlowController()
        {
            MonoBehaviour fc = FindMB("ServerCampaignFlowController");
            if (fc == null) fc = FindMB("ServerDynamicFlowController");
            return fc;
        }

        MonoBehaviour FindClientFlowController()
        {
            MonoBehaviour fc = FindMB("ClientCampaignFlowController");
            if (fc == null) fc = FindMB("ClientDynamicFlowController");
            return fc;
        }

        object GetField(object o, string name)
        {
            if (o == null) return null;
            for (Type t = o.GetType(); t != null && t != typeof(object); t = t.BaseType)
            {
                FieldInfo f = t.GetField(name, BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic);
                if (f != null) return f.GetValue(o);
            }
            return null;
        }

        object GetProp(object o, string name)
        {
            if (o == null) return null;
            for (Type t = o.GetType(); t != null && t != typeof(object); t = t.BaseType)
            {
                PropertyInfo p = t.GetProperty(name, BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic);
                if (p != null) return p.GetValue(o, null);
            }
            return null;
        }

        // 反射调用无参 bool 方法，返回 "true"/"false"/"null"
        string CallBool(object o, string name)
        {
            if (o == null) return "null";
            MethodInfo mi = o.GetType().GetMethod(name, BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic);
            if (mi == null) return "null";
            try { object v = mi.Invoke(o, null); return v is bool b ? (b ? "true" : "false") : "null"; }
            catch { return "null"; }
        }

        // 反射调用无参方法，返回 object（null 表示不可用）
        object CallMethod(object o, string name)
        {
            if (o == null) return null;
            MethodInfo mi = o.GetType().GetMethod(name, BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic);
            if (mi == null) return null;
            try { return mi.Invoke(o, null); }
            catch { return null; }
        }

        // 组件所属 GridManager 的名字，找不到返回 null。
        //
        // 为什么要逐级找而不是直接读字段：网格归属挂在 StaticGridLocation /
        // DynamicGridLocation 上，而灶的状态挂在 ServerCookingStation 上
        // ——两者是同一 GameObject 的**不同组件**，所以 AppendCookers 直接
        // 读 m_gridManager / get_AccessGridManager 恒为 null，而 AppendStations
        // 枚举的就是 GridLocation 本体，读得到 "GridManager"。
        //
        // 同一口灶在两张表里 grid 一个有一个没有，是 Python 侧 (grid,name)
        // 资源 id 的致命不一致：候选 id 从 cookers[] 构造 → grid=""，
        // cooker_state 查 cookers[] 命中（null→"" 归一化相等），
        // cooker_station 查 stations[] 落空（"GridManager" != ""）。
        // 后果是 FillCooker 取到米却解析不出灶的位置，放锅那步 target=None
        // 直接 FAILURE、下一棵树的 EnsureHandsFree 把米放下 —— 表现为
        // 「拿起米又放下」，整局 1148 拍零烹饪（trace-20261016-215154）。
        object GridManagerObj(MonoBehaviour mb)
        {
            // 功能站（ServerWashingStation / ServerPlateStation 等）没有
            // GridLocation 组件，m_gridManager / get_AccessGridManager 直接
            // 读恒为 null。这里沿用 GridName 的逐级回退：本组件 → 同 GameObject
            // 其它组件 → 子物体。不往父级找：父级可能是整关容器，网格未必
            // 是这口灶自己的。
            if (mb == null) return null;
            object gm = GridManagerOf(mb);
            if (gm == null)
            {
                try
                {
                    foreach (Component c in mb.GetComponents<Component>())
                    {
                        gm = GridManagerOf(c);
                        if (gm != null) break;
                    }
                    if (gm == null)
                    {
                        foreach (Component c in mb.GetComponentsInChildren<Component>(true))
                        {
                            gm = GridManagerOf(c);
                            if (gm != null) break;
                        }
                    }
                }
                catch { return null; }
            }
            return gm is UnityEngine.Object ugm && ugm != null ? ugm : null;
        }

        string GridName(MonoBehaviour mb)
        {
            object gm = GridManagerObj(mb);
            return gm is UnityEngine.Object ugm && ugm != null ? ugm.name : null;
        }

        object GridManagerOf(object o)
        {
            if (o == null) return null;
            object gm = GetField(o, "m_gridManager");
            if (gm == null) gm = CallMethod(o, "get_AccessGridManager");
            return gm;
        }

        bool HasComponent(MonoBehaviour mb, string sub)
        {
            try
            {
                Component[] comps = mb.GetComponents<Component>();
                foreach (Component c in comps)
                    if (c != null && c.GetType().Name.ToLowerInvariant().IndexOf(sub) >= 0) return true;
            }
            catch { }
            return false;
        }

        // 可投掷 = GameObject 上挂了 ThrowableItem 组件（食材有，锅/盘没有）。
        bool IsThrowable(object o)
        {
            try
            {
                GameObject go = o as GameObject;
                if (go == null && o is Component comp) go = comp.gameObject;
                if (go == null) return false;
                Type tt = FindType("ThrowableItem");
                if (tt == null) return false;
                return go.GetComponent(tt) != null;
            }
            catch { return false; }
        }

        // ---------- JSON 工具 ----------
        string Jstr(string s)
        {
            if (s == null) return "null";
            StringBuilder sb = new StringBuilder();
            sb.Append('"');
            foreach (char c in s)
            {
                switch (c)
                {
                    case '"': sb.Append("\\\""); break;
                    case '\\': sb.Append("\\\\"); break;
                    case '\n': sb.Append("\\n"); break;
                    case '\r': sb.Append("\\r"); break;
                    case '\t': sb.Append("\\t"); break;
                    default:
                        if (c < 0x20) sb.Append("\\u").Append(((int)c).ToString("x4"));
                        else sb.Append(c);
                        break;
                }
            }
            sb.Append('"');
            return sb.ToString();
        }

        string Fmt(object o)
        {
            if (o == null) return "null";
            if (o is float f) return f.ToString("0.#");
            if (o is double d) return d.ToString("0.#");
            if (o is int i) return i.ToString();
            if (o is long l) return l.ToString();
            if (o is bool b) return b ? "true" : "false";
            if (o is UnityEngine.Object uo) return Jstr(uo == null ? "" : uo.name);
            return Jstr(o.ToString());
        }

        string V(object o)
        {
            return o == null ? "null" : o.ToString();
        }

        // ---------- 网格换算（出餐口等功能站无 m_gridIndex，靠世界坐标反推）----------
        float ToFloat(object o)
        {
            if (o == null) return 0f;
            if (o is float f) return f;
            if (o is int i) return (float)i;
            if (o is double d) return (float)d;
            if (o is long l) return (float)l;
            return 0f;
        }

        void CalibrateGrid()
        {
            // origin = pos - cell * grid，取任一 Static/DynamicGridLocation 反解。
            // 与 Python 侧 GridMapping 校准一致（本关 14.4 / -6.0 @ cell 1.2）。
            //
            // _mainGrid / _cellX / _cellZ 必须和校准原点同源（取自校准用的那条
            // Static/DynamicGridLocation 所属网格），否则 GridIndexOn 的「主/非主」
            // 判定与 GridIndex 的坐标系不一致。FindMB 只取第一个 QuadGridManager，
            // 枚举顺序可能先拿到附属网格（气球关实测 #0 Balloon Basket），于是把
            // 篮子误当主网格、功能站 gx/gz 报成主网格投影（实机 WashingPart (7,4)
            // vs 游戏句柄篮子本地 (-1,2)，interact_ready 恒 False）。
            MonoBehaviour fallback = FindMB("QuadGridManager");
            if (fallback == null) fallback = FindMB("GridManager");
            _mainGrid = fallback;   // 兜底：循环找不到站点时用
            if (fallback != null)
            {
                object size = GetField(fallback, "m_size");
                if (size is Vector3 sz && sz.x > 0f && sz.z > 0f)
                {
                    _cellX = sz.x;
                    _cellZ = sz.z;
                }
            }
            foreach (MonoBehaviour mb in _all)
            {
                if (mb == null) continue;
                string tn = mb.GetType().Name;
                if (tn != "StaticGridLocation" && tn != "DynamicGridLocation") continue;
                object gi = GetField(mb, "m_gridIndex");
                if (gi == null) continue;
                object gx = GetField(gi, "m_x");
                object gz = GetField(gi, "m_z");
                if (gx == null || gz == null) continue;
                // 校准用哪条站点，主网格和格宽就取它所属网格，保证同源。
                object gm = GridManagerOf(mb);
                if (gm is UnityEngine.Object ugm && ugm != null)
                {
                    _mainGrid = ugm;
                    object gsize = GetField(gm, "m_size");
                    if (gsize is Vector3 gsz && gsz.x > 0f && gsz.z > 0f)
                    {
                        _cellX = gsz.x;
                        _cellZ = gsz.z;
                    }
                }
                else
                {
                    continue;   // 网格归属不明，不能拿它校准（否则 _mainGrid 与原点不同源）
                }
                Vector3 p = mb.transform.position;
                // 与 Python 侧 _calibrate 一致：反解后吸到 4 位小数，
                // 抹掉 float 噪声（否则 14.4 会变成 14.3999996，半格边界出错一格）。
                _originX = (float)Math.Round((double)(p.x - _cellX * ToFloat(gx)), 4);
                _originZ = (float)Math.Round((double)(p.z - _cellZ * ToFloat(gz)), 4);
                _gridReady = true;
                return;
            }
        }

        string GridIndex(Vector3 p)
        {
            if (!_gridReady) CalibrateGrid();
            if (!_gridReady) return "null,null";
            // 出餐口中心恰在半格边界（x=15.0 → 0.5 格），float 噪声会让
            // 0.5 变成 0.5000003 而四舍五入错一格。先吸到 4 位小数再取整，
            // 与 Python to_grid 的 round-half-to-even 对齐。
            double rx = ((double)p.x - _originX) / _cellX;
            double rz = ((double)p.z - _originZ) / _cellZ;
            int gx = (int)Math.Round(Math.Round(rx, 4));
            int gz = (int)Math.Round(Math.Round(rz, 4));
            return gx + "," + gz;
        }

        string GridIndexOn(object gm, Vector3 p)
        {
            // 按**指定网格**的 origin / cell 反解本地 gx/gz，供功能站
            // （洗碗池/盘架）使用。功能站挂在 Balloon Basket 这类非主网格上，
            // 用主网格的 GridIndex(p) 会算出错误的帧（实机 WashingPart 报
            // (7,2) 而游戏句柄是篮子本地 (-1,2)，interact_ready 恒 False）。
            // gm 为 null 或就是主网格时退回主网格投影（旧行为）：主网格的
            // transform.position 不是网格原点，必须用校准过的 _originX/Z。
            if (!_gridReady) CalibrateGrid();   // 先校准，保证 _mainGrid 已就绪
            if (gm == null)
                return GridIndex(p);
            if (_mainGrid != null && gm is UnityEngine.Object uo && uo == _mainGrid)
                return GridIndex(p);
            Vector3 origin;
            if (gm is Component comp)
                origin = comp.transform.position;
            else if (gm is GameObject go)
                origin = go.transform.position;
            else
                return GridIndex(p);
            float cellX = 1.2f, cellZ = 1.2f;
            object size = GetField(gm, "m_size");
            if (size is Vector3 sz && sz.x > 0f && sz.z > 0f)
            {
                cellX = sz.x;
                cellZ = sz.z;
            }
            double rx = ((double)p.x - origin.x) / cellX;
            double rz = ((double)p.z - origin.z) / cellZ;
            int gx = (int)Math.Round(Math.Round(rx, 4));
            int gz = (int)Math.Round(Math.Round(rz, 4));
            return gx + "," + gz;
        }

        // ---------- 状态 ----------
        string BuildState()
        {
            if (_staticJson == null && IsInLevel()) BuildStatic();

            // 分段计时：定位序列化大头（实机 snapshot_ms 665ms，结果见帧里 segments 字段）。
            var segW = System.Diagnostics.Stopwatch.StartNew();
            var seg = new StringBuilder();

            StringBuilder sb = new StringBuilder("{");
            sb.Append("\"plugin_version\":\"0.19.0\",\"protocol_version\":4,\"session_id\":").Append(Jstr(_sessionId))
              .Append(",\"scene_epoch\":").Append(_sceneEpoch)
              .Append(",\"phase\":").Append(Jstr(Phase()))
              .Append(",\"level\":").Append(Jstr(_level))
              .Append(",\"paused\":").Append(IsPaused() ? "true" : "false");
            seg.Append("meta=").Append(segW.Elapsed.TotalMilliseconds.ToString("0.0")).Append(';'); segW.Reset(); segW.Start();

            sb.Append(",\"score\":");
            AppendScore(sb);
            seg.Append("score=").Append(segW.Elapsed.TotalMilliseconds.ToString("0.0")).Append(';'); segW.Reset(); segW.Start();

            sb.Append(",\"timer\":{");
            AppendTimer(sb, FindClientFlowController(), "client");
            sb.Append(',');
            AppendTimer(sb, FindServerFlowController(), "server");
            sb.Append("}");
            seg.Append("timer=").Append(segW.Elapsed.TotalMilliseconds.ToString("0.0")).Append(';'); segW.Reset(); segW.Start();

            sb.Append(",\"orders\":[");
            AppendOrders(sb);
            sb.Append("]");
            seg.Append("orders=").Append(segW.Elapsed.TotalMilliseconds.ToString("0.0")).Append(';'); segW.Reset(); segW.Start();

            sb.Append(",\"chefs\":[");
            int chefCount = AppendChefs(sb);
            sb.Append("],\"chefCount\":").Append(chefCount);
            AppendChopMonitor(sb);
            seg.Append("chefs=").Append(segW.Elapsed.TotalMilliseconds.ToString("0.0")).Append(';'); segW.Reset(); segW.Start();

            // 手持物 -> 持有者名，本帧只算一次，plates/items/extinguishers 共用。
            Dictionary<GameObject, string> heldBy = BuildHeldByMap();
            seg.Append("heldBy=").Append(segW.Elapsed.TotalMilliseconds.ToString("0.0")).Append(';'); segW.Reset(); segW.Start();

            sb.Append(",\"plates\":[");
            AppendPlates(sb, heldBy);
            sb.Append("]");
            seg.Append("plates=").Append(segW.Elapsed.TotalMilliseconds.ToString("0.0")).Append(';'); segW.Reset(); segW.Start();

            sb.Append(",\"workstations\":[");
            AppendWorkstations(sb);
            sb.Append("]");
            seg.Append("workstations=").Append(segW.Elapsed.TotalMilliseconds.ToString("0.0")).Append(';'); segW.Reset(); segW.Start();

            sb.Append(",\"cookers\":[");
            AppendCookers(sb);
            sb.Append("]");
            seg.Append("cookers=").Append(segW.Elapsed.TotalMilliseconds.ToString("0.0")).Append(';'); segW.Reset(); segW.Start();

            sb.Append(",\"extinguishers\":[");
            AppendExtinguishers(sb, heldBy);
            sb.Append("]");
            seg.Append("extinguishers=").Append(segW.Elapsed.TotalMilliseconds.ToString("0.0")).Append(';'); segW.Reset(); segW.Start();

            sb.Append(",\"items\":[");
            AppendItems(sb, heldBy);
            sb.Append("]");
            seg.Append("items=").Append(segW.Elapsed.TotalMilliseconds.ToString("0.0")).Append(';'); segW.Reset(); segW.Start();

            sb.Append(",\"grid\":");
            AppendGrid(sb);
            seg.Append("grid=").Append(segW.Elapsed.TotalMilliseconds.ToString("0.0")).Append(';'); segW.Reset(); segW.Start();

            sb.Append(",\"grids\":");
            AppendGrids(sb);
            seg.Append("grids=").Append(segW.Elapsed.TotalMilliseconds.ToString("0.0")).Append(';'); segW.Reset(); segW.Start();

            sb.Append(",\"terrain\":");
            AppendTerrain(sb);
            seg.Append("terrain=").Append(segW.Elapsed.TotalMilliseconds.ToString("0.0")).Append(';'); segW.Reset(); segW.Start();

            sb.Append(",\"level_bounds\":");
            AppendLevelBounds(sb);
            seg.Append("level_bounds=").Append(segW.Elapsed.TotalMilliseconds.ToString("0.0")).Append(';'); segW.Reset(); segW.Start();

            sb.Append(",\"occupancy\":");
            AppendOccupancy(sb);
            seg.Append("occupancy=").Append(segW.Elapsed.TotalMilliseconds.ToString("0.0")).Append(';'); segW.Reset(); segW.Start();

            sb.Append(",\"belts\":");
            AppendBelts(sb);
            seg.Append("belts=").Append(segW.Elapsed.TotalMilliseconds.ToString("0.0")).Append(';'); segW.Reset(); segW.Start();

            sb.Append(",\"hazards\":");
            AppendHazards(sb);
            seg.Append("hazards=").Append(segW.Elapsed.TotalMilliseconds.ToString("0.0")).Append(';'); segW.Reset(); segW.Start();

            sb.Append(",\"wind_zones\":");
            AppendWindZones(sb);
            seg.Append("wind_zones=").Append(segW.Elapsed.TotalMilliseconds.ToString("0.0")).Append(';'); segW.Reset(); segW.Start();

            sb.Append(",\"stations\":[");
            AppendStations(sb);
            sb.Append(']');
            seg.Append("stations=").Append(segW.Elapsed.TotalMilliseconds.ToString("0.0")).Append(';'); segW.Reset(); segW.Start();

            if (_staticJson != null) sb.Append(',').Append(_staticJson);
            seg.Append("staticJson=").Append(segW.Elapsed.TotalMilliseconds.ToString("0.0")).Append(';'); segW.Reset(); segW.Start();

            sb.Append(",\"segments\":\"").Append(seg.ToString()).Append('"');
            sb.Append(",\"diagnostics\":").Append(_performanceJson);
            sb.Append('}');
            return sb.ToString();
        }

        // 监控 P2 的 chop/投掷键(WorkstationInteract)按住时长。IsUseDown() 是
        // 纯状态查询、无副作用，可每拍调用；按键上/下沿靠 unscaledTime 差值计时。
        void AppendChopMonitor(StringBuilder sb)
        {
            float now = Time.unscaledTime;
            bool down = false;
            foreach (MonoBehaviour mb in _all)
            {
                if (mb == null) continue;
                if (mb.GetType().Name == "PlayerControls" && mb.name == "Player 2")
                {
                    down = CallBool(GetField(mb, "m_controlScheme"), "IsUseDown") == "true";
                    break;
                }
            }
            if (down)
            {
                if (_chopDownSince < 0f) _chopDownSince = now;
                _chopHoldMs = (now - _chopDownSince) * 1000f;
            }
            else if (_chopDownSince >= 0f)
            {
                _chopHoldMs = (now - _chopDownSince) * 1000f;
                _chopDownSince = -1f;
            }
            sb.Append(",\"chop_held\":").Append(down ? "true" : "false")
              .Append(",\"chop_hold_ms\":").Append(_chopHoldMs.ToString("0"))
              .Append(",\"chop_down_since\":")
              .Append(_chopDownSince < 0f ? "null" : _chopDownSince.ToString("0.000"));
        }

        string Phase()
        {
            if (FindServerFlowController() == null) return "menu";
            return CountChefs() >= 1 ? "level" : "lobby";
        }

        bool IsPaused()
        {
            MonoBehaviour f = FindMB("T17InGameFlow");
            if (f == null) return false;
            MethodInfo mi = f.GetType().GetMethod("IsPauseMenuOpen", BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic);
            if (mi != null)
            {
                try { object v = mi.Invoke(f, null); if (v is bool b) return b; }
                catch { }
            }
            object p = GetField(f, "m_wasPauseMenuOpen");
            return p is bool pb && pb;
        }

        void AppendScore(StringBuilder sb)
        {
            object score = GetField(GetField(FindServerFlowController(), "m_teamMonitor"), "m_score");
            sb.Append('{');
            AppendK(sb, "deliveries", GetField(score, "TotalSuccessfulDeliveries"), false);
            AppendK(sb, "base", GetField(score, "TotalBaseScore"), false);
            AppendK(sb, "tips", GetField(score, "TotalTipsScore"), false);
            AppendK(sb, "combo", GetField(score, "TotalCombo"), false);
            AppendK(sb, "multiplier", GetField(score, "TotalMultiplier"), true);
            sb.Append('}');
        }

        void AppendK(StringBuilder sb, string key, object v, bool last)
        {
            sb.Append(Jstr(key)).Append(':').Append(Fmt(v));
            if (!last) sb.Append(',');
        }

        void AppendTimer(StringBuilder sb, MonoBehaviour fc, string side)
        {
            sb.Append(Jstr(side)).Append(":{");
            object rt = GetField(fc, "m_roundTimer");
            sb.Append("\"left\":").Append(Fmt(GetField(rt, "m_timeLeft")))
              .Append(",\"limit\":").Append(Fmt(GetField(rt, "m_timeLimit")));
            sb.Append('}');
        }

        void AppendOrders(StringBuilder sb)
        {
            object ao = GetField(GetField(GetField(FindServerFlowController(), "m_teamMonitor"), "m_ordersController"), "m_activeOrders");
            if (!(ao is IEnumerable)) { sb.Append("null"); return; }

            int i = 0;
            foreach (object o in (IEnumerable)ao)
            {
                if (i > 0) sb.Append(',');
                object mid = GetField(GetField(o, "ID"), "m_id");
                object rle = GetField(o, "RecipeListEntry");
                object order = GetField(rle, "m_order");
                object score = GetField(rle, "m_scoreForMeal");
                object remaining = GetField(o, "Remaining");
                if (remaining == null) remaining = GetProp(o, "Remaining");
                object lifetime = GetField(o, "Lifetime");
                if (lifetime == null) lifetime = GetProp(o, "Lifetime");
                string dish = order is UnityEngine.Object uo && uo != null ? uo.name : "?";

                sb.Append("{\"id\":").Append(Fmt(mid))
                  .Append(",\"dish\":").Append(Jstr(dish))
                  .Append(",\"score\":").Append(Fmt(score))
                  .Append(",\"remaining\":").Append(Fmt(remaining))
                  .Append(",\"lifetime\":").Append(Fmt(lifetime))
                  .Append(",\"composition\":").Append(ArrJson(GetField(order, "m_composition")))
                  .Append(",\"optional\":").Append(ArrJson(GetField(order, "m_optional")))
                  .Append('}');

                if (!_orderFieldsDumped)
                {
                    _orderFieldsDumped = true;
                    DumpFields("OrderData", o);
                }
                i++;
                if (i >= 12) break;
            }
        }

        int CountChefs()
        {
            int n = 0;
            foreach (MonoBehaviour mb in _all)
                if (mb != null && mb.GetType().Name == "PlayerControls") n++;
            return n;
        }

        int AppendChefs(StringBuilder sb)
        {
            int i = 0;
            foreach (MonoBehaviour mb in _all)
            {
                if (mb == null) continue;
                if (mb.GetType().Name != "PlayerControls") continue;
                if (i > 0) sb.Append(',');

                Vector3 p = mb.transform.position;
                object resp = GetField(mb, "m_bRespawning");
                object held = Held(mb);

                // 相对脚下地面的位置：站在移动平台（篮子）上时，世界坐标会被
                // 平台带着漂，settle 判定会误判「没停稳」。这里减掉地面 collider
                // 的参考点，得到相对位置；静态地面上 collider 固定，等价于世界
                // 坐标（旧行为）。无 collider 时退回世界坐标。
                float relX = p.x, relZ = p.z;
                try
                {
                    object mov = GetField(mb, "m_surfaceMovable");
                    object gc = mov != null ? GetField(mov, "m_groundCast") : null;
                    object gcol = gc != null ? GetField(gc, "m_groundCollider") : null;
                    if (gcol is UnityEngine.Object uc && uc != null)
                    {
                        GameObject cGo = gcol as GameObject;
                        if (cGo == null && gcol is Component cc) cGo = cc.gameObject;
                        if (cGo != null)
                        {
                            Vector3 cp = cGo.transform.position;
                            relX = p.x - cp.x;
                            relZ = p.z - cp.z;
                        }
                    }
                }
                catch { }

                sb.Append("{\"name\":").Append(Jstr(mb.name))
                  .Append(",\"x\":").Append(p.x.ToString("0.00"))
                  .Append(",\"z\":").Append(p.z.ToString("0.00"))
                  .Append(",\"rel_x\":").Append(relX.ToString("0.00"))
                  .Append(",\"rel_z\":").Append(relZ.ToString("0.00"))
                  .Append(",\"yaw\":").Append(mb.transform.eulerAngles.y.ToString("0"))
                  .Append(",\"respawning\":").Append(resp is bool b ? (b ? "true" : "false") : "null")
                  .Append(",\"held\":").Append(Fmt(held))
                  .Append(",\"held_throwable\":").Append(IsThrowable(held) ? "true" : "false")
                  .Append(",\"interactions\":").Append(Interactions(mb))
                  .Append('}');
                i++;
            }
            return i;
        }

        object Held(MonoBehaviour pc)
        {
            object c = GetField(pc, "m_clientCarrier");
            if (c == null) c = GetField(pc, "m_serverCarrier");
            if (c == null) return null;
            object carried = GetField(c, "m_carriedObjects");
            if (carried is IEnumerable)
            {
                foreach (object e in (IEnumerable)carried)
                {
                    if (e == null) continue;
                    return e;
                }
                return null;
            }
            return c;
        }

        string GridXY(object handler)
        {
            // 从拾取/放置句柄所在 GameObject（含子物体）上找带 m_gridIndex 的组件，返回 "x,z"
            if (handler == null) return "null,null";
            GameObject go = handler as GameObject;
            if (go == null)
            {
                Component c = handler as Component;
                if (c != null) go = c.gameObject;
            }
            if (go == null) return "null,null";
            foreach (Component comp in go.GetComponentsInChildren<Component>(true))
            {
                if (comp == null) continue;
                object gi = GetField(comp, "m_gridIndex");
                if (gi != null)
                {
                    object x = GetField(gi, "m_x");
                    object z = GetField(gi, "m_z");
                    return Fmt(x) + "," + Fmt(z);
                }
            }
            // 出餐口等功能站没有 m_gridIndex：退而从世界坐标反推网格。
            return GridIndex(go.transform.position);
        }

        string CookInfo(MonoBehaviour mb)
        {
            // 从可携带物(锅等)上找 HasContents 与 GetCookedOrderState，返回 "has,cooked"
            string has = "null";
            string cooked = "null";
            foreach (Component c in mb.GetComponentsInChildren<Component>(true))
            {
                if (c == null) continue;
                if (has == "null")
                {
                    string hc = CallBool(c, "HasContents");
                    if (hc != "null") has = hc;
                }
                if (cooked == "null")
                {
                    object cs = CallMethod(c, "GetCookedOrderState");
                    if (cs != null) cooked = cs.ToString();
                }
                if (has != "null" && cooked != "null") break;
            }
            return has + "," + cooked;
        }

        object UnwrapReferral(object referral, string fieldName)
        {
            // HandlePickupReferral / HandlePlacementReferral 只是转介，内部
            // m_*ReferralObject 才指向真正的拾取/放置目标。这里用目标本身，
            // 不用转介物的名字/格子——两者可能不一致（实机 placement 信号不准）。
            if (referral == null) return null;
            object target = GetField(referral, fieldName);
            if (target is UnityEngine.Object uo && uo != null) return uo;
            return referral;  // 没解开（旧结构/异常）→ 退回转介本身
        }

        string Interactions(MonoBehaviour pc)
        {
            object io = GetField(pc, "m_interactionObjects");
            if (io == null) io = GetField(pc, "m_tmpInteractionObjects");
            if (io == null) return "null";
            object inter = GetField(io, "m_interactable");
            object pickup = UnwrapReferral(GetField(io, "m_iHandlePickup"), "m_pickupReferralObject");
            object placement = UnwrapReferral(GetField(io, "m_iHandlePlacement"), "m_placementReferralObject");
            object grid = GetField(io, "m_gridLocation");
            string iname = inter is UnityEngine.Object uo && uo != null ? uo.name : null;
            string pname = pickup is UnityEngine.Object up && up != null ? up.name : null;
            string qname = placement is UnityEngine.Object uq && uq != null ? uq.name : null;
            string gname = grid is UnityEngine.Object ug && ug != null ? ug.name : null;
            object gi = grid is UnityEngine.Object ug2 && ug2 != null ? GetField(grid, "m_gridIndex") : null;
            object gx = gi != null ? GetField(gi, "m_x") : null;
            object gz = gi != null ? GetField(gi, "m_z") : null;
            if (gx == null && grid is UnityEngine.Object ug3 && ug3 != null) gx = GetField(grid, "m_x");
            if (gz == null && grid is UnityEngine.Object ug4 && ug4 != null) gz = GetField(grid, "m_z");
            string[] pg = GridXY(pickup).Split(',');
            string[] qg = GridXY(placement).Split(',');
            // 出餐口等功能站不报 m_gridLocation，gx/gz 为 null。退而从
            // pickup/placement 句柄反推（面朝它时句柄才非空，正好是朝向信号）。
            if (gx == null || gz == null)
            {
                string fx = qg[0], fz = qg[1];
                if (fx == "null") { fx = pg[0]; fz = pg[1]; }
                int gxv, gzv;
                if (fx != "null" && int.TryParse(fx, out gxv) && int.TryParse(fz, out gzv))
                {
                    gx = (object)gxv;
                    gz = (object)gzv;
                }
            }
            return "{\"interactable\":" + Jstr(iname)
                 + ",\"pickup\":" + Jstr(pname)
                 + ",\"placement\":" + Jstr(qname)
                 + ",\"grid\":" + Jstr(gname)
                 + ",\"gx\":" + Fmt(gx) + ",\"gz\":" + Fmt(gz)
                 + ",\"pickup_gx\":" + pg[0] + ",\"pickup_gz\":" + pg[1]
                 + ",\"placement_gx\":" + qg[0] + ",\"placement_gz\":" + qg[1]
                 + "}";
        }

        void AppendPlates(StringBuilder sb, Dictionary<GameObject, string> heldBy)
        {
            int i = 0;
            Dictionary<GameObject, string> stationMap = BuildStationMap();
            Dictionary<string, string> stationGrid = BuildStationGridMap();
            foreach (MonoBehaviour mb in _all)
            {
                if (mb == null) continue;
                if (mb.GetType().Name != "ServerPlate") continue;
                if (i > 0) sb.Append(',');
                object ingr = GetField(mb, "m_ingredientContainer");
                Vector3 p = mb.transform.position;
                sb.Append("{\"n\":").Append(Jstr(mb.GetInstanceID().ToString()))
                  .Append(",\"name\":").Append(Jstr(mb.name))
                  .Append(",\"x\":").Append(p.x.ToString("0.0"))
                  .Append(",\"z\":").Append(p.z.ToString("0.0"))
                  .Append(",\"count\":").Append(ContentsCount(ingr))
                  .Append(",\"contents\":").Append(ListContents(ingr))
                  .Append(",\"detail\":").Append(ListContentsDetail(ingr))
                  .Append(",\"grid\":").Append(Jstr(GridOfItem(mb.gameObject, stationMap, stationGrid)))
                  .Append(",\"held_by\":").Append(
                      Jstr(heldBy.TryGetValue(mb.gameObject, out string hb) ? hb : null))
                  .Append('}');
                i++;
                if (i >= 8) break;
            }
        }

        int ContentsCount(object ingr)
        {
            if (ingr == null) return -1;
            MethodInfo mi = ingr.GetType().GetMethod("GetContentsCount", BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic);
            if (mi == null) return -1;
            try { object v = mi.Invoke(ingr, null); if (v is int n) return n; }
            catch { }
            return -1;
        }

        string ListContents(object ingr)
        {
            if (ingr == null) return "[]";
            MethodInfo mi = ingr.GetType().GetMethod("GetContents", BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic);
            if (mi != null)
            {
                try { object r = mi.Invoke(ingr, null); if (r is IEnumerable) return ArrIngredientNames(r); }
                catch { }
            }
            foreach (string fn in new[] { "m_contents", "m_ingredientContents" })
            {
                object r = GetField(ingr, fn);
                if (r is IEnumerable) return ArrIngredientNames(r);
            }
            return "[]";
        }

        string ArrIngredientNames(object arr)
        {
            if (!(arr is IEnumerable)) return "[]";
            StringBuilder sb = new StringBuilder("[");
            int i = 0;
            foreach (object e in (IEnumerable)arr)
            {
                if (i > 0) sb.Append(',');
                sb.Append(Jstr(IngredientName(e)));
                i++;
                if (i >= 12) break;
            }
            sb.Append(']');
            return sb.ToString();
        }

        string IngredientName(object e)
        {
            if (e == null) return "null";
            if (e is UnityEngine.Object uo) return uo == null ? "null" : uo.name;
            object node = GetField(e, "m_ingriedientOrderNode");
            if (node is UnityEngine.Object uo2 && uo2 != null) return uo2.name;
            return e.GetType().Name;
        }

        // 递归展开一个 AssembledDefinitionNode，读出"具体是什么料"。
        // 熟料/复合节点（CookedCompositeAssembledNode 等）只有不透明的类型名，
        // 真正的料在其继承的 m_composition 里（IngredientAssembledNode /
        // ItemAssembledNode），递归下去才能拿到 SushiRice / Pasta 这类名字。
        string DescribeNode(object e)
        {
            if (e == null) return "null";
            if (e is UnityEngine.Object uo) return uo == null ? "null" : uo.name;
            object ing = GetField(e, "m_ingriedientOrderNode");
            if (ing is UnityEngine.Object uo2 && uo2 != null) return "ing:" + uo2.name;
            object item = GetField(e, "m_itemOrderNode");
            if (item is UnityEngine.Object uo3 && uo3 != null) return "item:" + uo3.name;
            object comp = GetField(e, "m_composition");
            if (comp is IEnumerable)
            {
                string cook = "";
                object step = GetField(e, "m_cookingStep");
                if (step is UnityEngine.Object uo4 && uo4 != null) cook = uo4.name;
                List<string> parts = new List<string>();
                foreach (object c in (IEnumerable)comp) parts.Add(DescribeNode(c));
                return e.GetType().Name + (cook.Length > 0 ? "(" + cook + ")" : "")
                    + "{ " + string.Join(", ", parts.ToArray()) + " }";
            }
            return e.GetType().Name;
        }

        string ListContentsDetail(object ingr)
        {
            if (ingr == null) return "[]";
            MethodInfo mi = ingr.GetType().GetMethod("GetContents", BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic);
            if (mi != null)
            {
                try { object r = mi.Invoke(ingr, null); if (r is IEnumerable) return ArrIngredientDetails(r); }
                catch { }
            }
            foreach (string fn in new[] { "m_contents", "m_ingredientContents" })
            {
                object r = GetField(ingr, fn);
                if (r is IEnumerable) return ArrIngredientDetails(r);
            }
            return "[]";
        }

        string ArrIngredientDetails(object arr)
        {
            if (!(arr is IEnumerable)) return "[]";
            StringBuilder sb = new StringBuilder("[");
            int i = 0;
            foreach (object e in (IEnumerable)arr)
            {
                if (i > 0) sb.Append(',');
                sb.Append(Jstr(DescribeNode(e)));
                i++;
                if (i >= 12) break;
            }
            sb.Append(']');
            return sb.ToString();
        }

        // 读锅里是什么料：锅对象的某个子组件就是 IngredientContainer，
        // 用和盘完全相同的方式（GetContents / m_contents + DescribeNode）读。
        string CookwareName(object pot)
        {
            // 灶上炊具的 GameObject 名（如 utensil_pot_01 / utensil_frying_pan_01）。
            // m_itemPot 是 ICookable（Component 或 GameObject），名字能区分锅/煎锅，
            // 而 get_AccessCookingType 的返回值未离线验证，先不依赖。
            if (pot == null) return null;
            if (pot is Component pc) return pc.gameObject.name;
            if (pot is GameObject go) return go.name;
            return null;
        }

        string PotContents(object pot)
        {
            if (pot == null) return "[]";
            GameObject go = pot as GameObject;
            if (go == null)
            {
                Component pc = pot as Component;
                if (pc != null) go = pc.gameObject;
            }
            if (go == null) return "[]";

            // 先尝试读取 IngredientContainer 的 detail
            string detail = "[]";
            foreach (Component c in go.GetComponentsInChildren<Component>(true))
            {
                if (c == null) continue;
                string det = ListContentsDetail(c);
                if (det != "[]") { detail = det; break; }
            }

            // 修复：锅被端离灶台后，detail 可能只有 ing:* 而缺少 CookedCompositeAssembledNode 包装。
            // 如果 detail 不为空但不含熟料标记，检查 cooked 状态，手动包装成熟料格式。
            if (detail != "[]" && detail.IndexOf("CookedCompositeAssembledNode") < 0)
            {
                // 检查锅的 cooked 状态（在传入的 pot 对象上调用）
                object cs = CallMethod(pot, "GetCookedOrderState");
                string cooked = (cs != null) ? cs.ToString() : "null";

                // 如果是熟的，包装成 CookedCompositeAssembledNode 格式
                if (cooked == "Cooked")
                {
                    // 推断锅具类型
                    string cookware = "Pot";  // 默认
                    if (pot != null)
                    {
                        string potType = pot.GetType().Name.ToLowerInvariant();
                        if (potType.IndexOf("frying") >= 0 || potType.IndexOf("pan") >= 0)
                            cookware = "Pan";
                        else if (potType.IndexOf("steamer") >= 0)
                            cookware = "Steamer";
                        else if (potType.IndexOf("oven") >= 0)
                            cookware = "Oven";
                    }

                    // detail 格式: ["ing:SushiRice"] 或 ["ing:A","ing:B"]
                    // 需要包装成: ["CookedCompositeAssembledNode(Pot){ ing:SushiRice }"]
                    // 去掉首尾的 [ ]，然后包装
                    string inner = detail.Substring(1, detail.Length - 2);  // 去掉 []
                    detail = "[\"CookedCompositeAssembledNode(" + cookware + "){ " +
                             inner.Replace("\"", "") + " }\"]";
                }
            }

            return detail;
        }

        void AppendWorkstations(StringBuilder sb)
        {
            int i = 0;
            foreach (MonoBehaviour mb in _all)
            {
                if (mb == null) continue;
                if (mb.GetType().Name != "ServerWorkstation") continue;
                if (i > 0) sb.Append(',');
                // 板上实际物品（生/熟都在 ServerAttachStation.m_item，用 InspectItem() 取）
                object attach = GetField(mb, "m_attachStation");
                object item = CallMethod(attach, "InspectItem");
                object interacters = GetField(mb, "m_interacters");
                object data = GetField(mb, "m_data");
                object interacting = GetField(data, "m_interacting");
                sb.Append("{\"n\":").Append(Jstr(mb.name))
                  .Append(",\"item\":").Append(Fmt(item))
                  .Append(",\"interacting\":").Append(interacting is bool b ? (b ? "true" : "false") : "null")
                  .Append(",\"interacters\":").Append(ArrJson(interacters))
                  .Append('}');
                i++;
                if (i >= 12) break;
            }
        }

        void AppendCookers(StringBuilder sb)
        {
            int i = 0;
            foreach (MonoBehaviour mb in _all)
            {
                if (mb == null) continue;
                string ln = mb.GetType().Name.ToLowerInvariant();
                if (ln.IndexOf("cookingstation") < 0 && ln.IndexOf("heatedcooking") < 0) continue;
                if (i > 0) sb.Append(',');
                object isCooking = GetField(mb, "m_isCooking");
                object isTurnedOn = GetField(mb, "m_isTurnedOn");
                object pot = GetField(mb, "m_itemPot");
                object flammable = GetField(mb, "m_flammable");
                string onFire = "null";
                if (flammable != null)
                {
                    object f = GetField(flammable, "m_onFire");
                    onFire = f is bool fb ? (fb ? "true" : "false") : "null";
                }
                Vector3 p = mb.transform.position;
                // 烹饪进度/时长：给 Python 侧 _deadline_forecast 预判「快糊了」用。
                // m_itemPot 是 ICookable（运行时 ServerCookingHandler），有
                // GetCookingProgress() 与 get_AccessCookingTime，同一单位 float。
                object cookingProgress = CallMethod(pot, "GetCookingProgress");
                object cookingTime = CallMethod(pot, "get_AccessCookingTime");
                // grid 必须与 AppendStations 对同一口灶报的值一致（见 GridName
                // 注释）：Python 侧的 (grid,name) 资源 id 两张表都要查得到。
                sb.Append("{\"n\":").Append(Jstr(mb.name))
                  .Append(",\"grid\":").Append(Jstr(GridName(mb)))
                  .Append(",\"x\":").Append(p.x.ToString("0.0"))
                  .Append(",\"z\":").Append(p.z.ToString("0.0"))
                  .Append(",\"isCooking\":").Append(isCooking is bool b ? (b ? "true" : "false") : "null")
                  .Append(",\"isTurnedOn\":").Append(isTurnedOn is bool b2 ? (b2 ? "true" : "false") : "null")
                  .Append(",\"cooked\":").Append(CallBool(pot, "IsCooked"))
                  .Append(",\"burning\":").Append(CallBool(pot, "IsBurning"))
                  .Append(",\"onFire\":").Append(onFire)
                  .Append(",\"cooking_progress\":").Append(Fmt(cookingProgress))
                  .Append(",\"cooking_time\":").Append(Fmt(cookingTime))
                  .Append(",\"cookware\":").Append(Jstr(CookwareName(pot)))
                  .Append(",\"cook_type\":").Append(Fmt(CallMethod(pot, "get_AccessCookingType")))
                  .Append(",\"pot_contents\":").Append(PotContents(pot))
                  .Append('}');
                i++;
                if (i >= 8) break;
            }
        }

        void AppendExtinguishers(StringBuilder sb, Dictionary<GameObject, string> heldBy)
        {
            int i = 0;
            foreach (MonoBehaviour mb in _all)
            {
                if (mb == null) continue;
                string ln = mb.GetType().Name.ToLowerInvariant();
                string lname = mb.name.ToLowerInvariant();
                if (ln.IndexOf("extinguish") < 0 && lname.IndexOf("extinguish") < 0) continue;
                Vector3 p = mb.transform.position;
                if (i > 0) sb.Append(',');
                sb.Append("{\"n\":").Append(Jstr(mb.name))
                  .Append(",\"x\":").Append(p.x.ToString("0.0"))
                  .Append(",\"z\":").Append(p.z.ToString("0.0"))
                  .Append(",\"held_by\":").Append(
                      Jstr(heldBy.TryGetValue(mb.gameObject, out string hb) ? hb : null))
                  .Append('}');
                i++;
                if (i >= 8) break;
            }
        }

        // 每帧一次：反查「可携带物挂在哪个站点上」（字段判据，替代 Python 侧按
        // 坐标排除灶上那口被重复报进 items 的锅）。返回 GameObject -> 描述：
        //   "cooking:<灶名>" —— 锅在灶上（读到 m_itemPot）
        //   "attach:<站点名>" —— 挂在普通挂载站（读到 AttachStation.m_item）
        // 不在表里 = 地上 / 手持 / 没匹配到。灶的映射后填、覆盖 attach（锅在灶上
        // 时优先算 cooking）。整个 O(_all) 一遍，不能每个 item 都扫两遍——那样
        // 5Hz 每帧几十万次 ToLowerInvariant 分配，游戏会一卡一卡（实机复现）。
        Dictionary<GameObject, string> BuildStationMap()
        {
            var map = new Dictionary<GameObject, string>();
            try
            {
                // ① 通用挂载站（先填）
                foreach (MonoBehaviour att in _all)
                {
                    if (att == null) continue;
                    if (att.GetType().Name.IndexOf("AttachStation") < 0) continue;
                    object item = GetField(att, "m_item");
                    if (item == null) continue;
                    GameObject go = item is Component c ? c.gameObject : (item as GameObject);
                    if (go == null) continue;
                    object station = GetField(att, "m_station");
                    string stName = station == null ? "null"
                        : (station is UnityEngine.Object su && su != null ? su.name : station.GetType().Name);
                    map[go] = "attach:" + stName;
                }
                // ② 灶（后填，覆盖 attach → 灶优先）
                foreach (MonoBehaviour mb in _all)
                {
                    if (mb == null) continue;
                    string tn = mb.GetType().Name;
                    if (tn.IndexOf("CookingStation") < 0 && tn.IndexOf("HeatedCooking") < 0) continue;
                    object pot = GetField(mb, "m_itemPot");
                    if (pot == null) continue;
                    GameObject go = pot is Component c2 ? c2.gameObject : (pot as GameObject);
                    if (go == null) continue;
                    map[go] = "cooking:" + mb.name;
                }
            }
            catch { }
            return map;
        }

        // 站点名 -> grid 名。散料/盘没有 grid 引用，靠 on_station 的站点反查。
        Dictionary<string, string> BuildStationGridMap()
        {
            if (_stationGridCache != null) return _stationGridCache;
            var map = new Dictionary<string, string>();
            foreach (MonoBehaviour mb in _all)
            {
                if (mb == null) continue;
                string g = GridName(mb);
                if (string.IsNullOrEmpty(g)) continue;
                if (!map.ContainsKey(mb.name)) map[mb.name] = g;
            }
            _stationGridCache = map;
            return map;
        }

        // 物件 -> grid 名：先查它所在的站点（on_station），再查该站点的 grid。
        string GridOfItem(GameObject go, Dictionary<GameObject, string> stationMap,
                          Dictionary<string, string> stationGrid)
        {
            if (go == null || stationMap == null || stationGrid == null) return null;
            if (!stationMap.TryGetValue(go, out string os) || string.IsNullOrEmpty(os)) return null;
            string stName = os;
            if (stName.StartsWith("attach:")) stName = stName.Substring(7);
            else if (stName.StartsWith("cooking:")) stName = stName.Substring(8);
            return stationGrid.TryGetValue(stName, out string g) ? g : null;
        }

        // 每帧一次：手持物 -> 持有者名（"Player 1" / "Player 2"）。AppendItems
        // 用它给 items[] 打 held_by 标记——Python 侧据此把手持物与散落物分开。
        // 手持物坐标跟着厨师走，散落语义的取料谓词不能把它当散落料（否则
        // 会出现 P2 追着拿料的 P1 跑，trace-20261018-150454）。
        Dictionary<GameObject, string> BuildHeldByMap()
        {
            var map = new Dictionary<GameObject, string>();
            try
            {
                foreach (MonoBehaviour mb in _all)
                {
                    if (mb == null) continue;
                    if (mb.GetType().Name != "PlayerControls") continue;
                    object held = Held(mb);
                    GameObject go = null;
                    if (held is GameObject g) go = g;
                    else if (held is Component c) go = c.gameObject;
                    if (go == null) continue;
                    map[go] = mb.name;
                }
            }
            catch { }
            return map;
        }

        void AppendItems(StringBuilder sb, Dictionary<GameObject, string> heldBy)
        {
            // 可携带物（地面/柜台/传送带上的原料、鱼片、盘子等；手持物也在此，
            // 靠 held_by 与散落物区分——手持锅的内容状态要在这里读）。
            // 按 GameObject 去重：同一个物件上挂着多个 Carryable/Teleportable 组件，
            // 按组件枚举会重复报 N 次；按物件报一次即可，Python 侧 dedupe_items
            // 仍按 (名字,位置) 兜底。
            int i = 0;
            HashSet<GameObject> seen = new HashSet<GameObject>();
            Dictionary<GameObject, string> stationMap = BuildStationMap();
            Dictionary<string, string> stationGrid = BuildStationGridMap();
            foreach (MonoBehaviour mb in _all)
            {
                if (mb == null) continue;
                string tn = mb.GetType().Name;
                if (tn.IndexOf("CarryableItem") < 0 && tn.IndexOf("TeleportableItem") < 0) continue;
                if (!seen.Add(mb.gameObject)) continue;
                Vector3 p = mb.transform.position;
                if (i > 0) sb.Append(',');
                string ci = CookInfo(mb);
                string[] cip = ci.Split(',');
                string has = cip[0];
                string cooked = cip[1];
                sb.Append("{\"n\":").Append(Jstr(mb.name))
                  .Append(",\"x\":").Append(p.x.ToString("0.0"))
                  .Append(",\"z\":").Append(p.z.ToString("0.0"))
                  .Append(",\"has_contents\":").Append(has == "true" ? "true" : has == "false" ? "false" : "null")
                  .Append(",\"cooked\":").Append(cooked == "null" ? "null" : Jstr(cooked))
                  .Append(",\"detail\":").Append(PotContents(mb))
                  .Append(",\"throwable\":").Append(IsThrowable(mb) ? "true" : "false")
                  .Append(",\"grid\":").Append(Jstr(GridOfItem(mb.gameObject, stationMap, stationGrid)))
                  .Append(",\"on_station\":").Append(
                      Jstr(stationMap.TryGetValue(mb.gameObject, out string os) ? os : null))
                  .Append(",\"held_by\":").Append(
                      Jstr(heldBy.TryGetValue(mb.gameObject, out string hb) ? hb : null))
                  .Append('}');
                i++;
            }
        }

        int Count(object e)
        {
            if (!(e is IEnumerable)) return -1;
            int n = 0;
            foreach (object _ in (IEnumerable)e) n++;
            return n;
        }

        void AppendGrid(StringBuilder sb)
        {
            object origin = GetField(FindMB("QuadGridManager"), "m_origin");
            object size = GetField(FindMB("QuadGridManager"), "m_size");
            object nodeMap = GetField(FindMB("GridNavSpace"), "m_nodeMap");
            object mo = GetField(FindMB("GridNavSpace"), "m_mapOffset");

            sb.Append("{\"origin\":\"").Append(V(origin))
              .Append("\",\"size\":\"").Append(V(size))
              .Append("\",\"mapOffset\":\"")
              .Append(GetField(mo, "X") == null ? "?" : GetField(mo, "X").ToString()).Append(",")
              .Append(GetField(mo, "Y") == null ? "?" : GetField(mo, "Y").ToString())
              .Append("\",\"nodeMap\":");
            if (nodeMap is Array arr)
            {
                int d0 = arr.GetLength(0), d1 = arr.GetLength(1);
                StringBuilder m = new StringBuilder();
                m.Append('"').Append(d0).Append('x').Append(d1).Append('=');
                for (int a = 0; a < d0; a++)
                    for (int b = 0; b < d1; b++)
                        m.Append(arr.GetValue(a, b) is bool bb && bb ? '1' : '0');
                m.Append('"');
                sb.Append(m);
            }
            else sb.Append("null");
            sb.Append('}');
        }

        // 替代 CalibrateGrid/GridIndex 的单网格假设（§4.1 根因：取第一个
        // QuadGridManager、第一个站点就 return，多网格下必然只拿到一个）。
        // 直接枚举全部 QuadGridManager 实例。
        //
        // 注意：主网格的 transform.position **不是**网格原点（有父级偏移，
        // 实机 Sushi_1_4 transform=12 而 m_gridIndex 实际原点 14.4，差 2.4），
        // 必须用 CalibrateGrid 反解出的 _originX/Z；非主网格（气球篮）的
        // transform 才是原点，与 GridIndexOn 的判定保持一致。
        void AppendGrids(StringBuilder sb)
        {
            if (!_gridReady) CalibrateGrid();
            sb.Append('[');
            int i = 0;
            foreach (MonoBehaviour mb in _all)
            {
                if (mb == null) continue;
                if (mb.GetType().Name != "QuadGridManager") continue;
                object size = GetField(mb, "m_size");
                float cell = 1.2f;
                if (size is Vector3 sz && sz.x > 0f) cell = sz.x;
                Vector3 p = mb.transform.position;
                float ox = p.x, oz = p.z;
                if (_gridReady && _mainGrid != null && _mainGrid.name == mb.name)
                {
                    ox = _originX;
                    oz = _originZ;
                }
                if (i > 0) sb.Append(',');
                sb.Append("{\"id\":").Append(Jstr(mb.name))
                  .Append(",\"ox\":").Append(ox.ToString("0.####"))
                  .Append(",\"oz\":").Append(oz.ToString("0.####"))
                  .Append(",\"cell\":").Append(cell.ToString("0.####"))
                  .Append('}');
                i++;
            }
            sb.Append(']');
        }

        // 取代 Col_Floor/Col_Walls/Col_Objects 名字前缀匹配（§2 根因 2：Sushi_1_4
        // 命中 41 个、Balloon_1_5 命中 0 个）。按层号分组输出，层名是 Unity 工程
        // 配置，跨关稳定（§4.4）。只收这几层：Ground(9)/Walls(10)/
        // PlateStationBlock(20)/CookingStationBlock(22)/TableBlock(26)/KillPlane(30)。
        static readonly int[] TERRAIN_LAYERS = { 9, 10, 20, 22, 26, 30 };

        void AppendTerrain(StringBuilder sb)
        {
            sb.Append('[');
            Dictionary<int, List<float[]>> byLayer = new Dictionary<int, List<float[]>>();
            foreach (int l in TERRAIN_LAYERS) byLayer[l] = new List<float[]>();

            Type goType = FindType("UnityEngine.GameObject");
            if (goType == null) goType = FindType("GameObject");
            MethodInfo mi = typeof(UnityEngine.Object).GetMethod(
                "FindObjectsOfType", BindingFlags.Public | BindingFlags.Static,
                null, new Type[] { typeof(Type) }, null);
            object[] gos = (mi != null && goType != null)
                ? (object[])mi.Invoke(null, new object[] { goType }) : null;
            if (gos != null)
            {
                foreach (object o in gos)
                {
                    GameObject go = o as GameObject;
                    if (go == null || !byLayer.ContainsKey(go.layer)) continue;
                    foreach (Component c in go.GetComponents<Component>())
                    {
                        if (c == null) continue;
                        if (c.GetType().Name.ToLowerInvariant().IndexOf("collider") < 0) continue;
                        object trig = GetProp(c, "isTrigger");
                        if (trig is bool tb && tb) continue;
                        object bd = GetProp(c, "bounds");
                        if (bd == null) continue;
                        object mn = GetProp(bd, "min"), mx = GetProp(bd, "max");
                        if (mn == null || mx == null) continue;
                        object ax = GetField(mn, "x"), az = GetField(mn, "z");
                        object bx = GetField(mx, "x"), bz = GetField(mx, "z");
                        if (ax == null || az == null || bx == null || bz == null) continue;
                        float axf = (float)ax, azf = (float)az, bxf = (float)bx, bzf = (float)bz;
                        if (bxf <= axf || bzf <= azf) continue;
                        byLayer[go.layer].Add(new float[] { axf, bxf, azf, bzf });
                    }
                }
            }
            // 过滤：阻挡层（10/20/22/26）里与 Ground(9) 几乎重合的 box 不是墙，
            // 是地板自己的 collider / KillPlane（反编译 LevelBounds：KillPlane 挂
            // RespawnCollider，其 collider 覆盖整块地板）。不滤掉的话
            // make_walkable_from_terrain 会把整块地板判成墙，角色一步都走不动
            // （实测 Balloon_1_5_2P：主地板/篮子地板各有一块这样的假墙，导致
            // distance_field 只有 P2 脚下 1 格可走）。
            List<float[]> groundBoxes = byLayer[9];
            for (int li = 0; li < TERRAIN_LAYERS.Length; li++)
            {
                int l = TERRAIN_LAYERS[li];
                if (l == 9 || l == 30) continue;   // Ground 保留；KillPlane(30) 保留
                List<float[]> src = byLayer[l];
                if (src == null || src.Count == 0) continue;
                List<float[]> kept = new List<float[]>(src.Count);
                List<string> dropped = null;
                foreach (float[] b in src)
                {
                    if (_NearlyMatchesAnyGround(b, groundBoxes))
                    {
                        if (!_terrainFilterLogged)
                        {
                            if (dropped == null) dropped = new List<string>();
                            dropped.Add(string.Format("[{0},{1},{2},{3}]",
                                b[0], b[1], b[2], b[3]));
                        }
                        continue;
                    }
                    kept.Add(b);
                }
                if (dropped != null)
                {
                    Logger.LogInfo("[TERRAIN] 过滤掉与 Ground 重合的假墙 layer=" + l
                        + " : " + string.Join(" ", dropped.ToArray()));
                }
                byLayer[l] = kept;
            }
            _terrainFilterLogged = true;

            int i = 0;
            foreach (int l in TERRAIN_LAYERS)
            {
                List<float[]> boxes = byLayer[l];
                if (boxes.Count == 0) continue;
                if (i > 0) sb.Append(',');
                sb.Append("{\"layer\":").Append(l).Append(",\"boxes\":[");
                for (int k = 0; k < boxes.Count; k++)
                {
                    if (k > 0) sb.Append(',');
                    float[] b = boxes[k];
                    sb.Append('[').Append(b[0].ToString("0.##")).Append(',')
                      .Append(b[1].ToString("0.##")).Append(',')
                      .Append(b[2].ToString("0.##")).Append(',')
                      .Append(b[3].ToString("0.##")).Append(']');
                }
                sb.Append("]}");
                i++;
            }
            sb.Append(']');
        }

        static bool _NearlyMatchesAnyGround(float[] box, List<float[]> grounds)
        {
            const float eps = 0.05f;
            if (grounds == null) return false;
            foreach (float[] g in grounds)
            {
                if (Math.Abs(box[0] - g[0]) <= eps &&
                    Math.Abs(box[1] - g[1]) <= eps &&
                    Math.Abs(box[2] - g[2]) <= eps &&
                    Math.Abs(box[3] - g[3]) <= eps)
                {
                    return true;
                }
            }
            return false;
        }

        // 可玩区域 AABB（LevelBounds.m_bounds）。反编译确认 LevelBounds 是关卡
        // 边界组件：SetupLevelBounds() 从 4 个侧壁 collider + Ceiling + KillPlane
        // 的 bounds 求中心平均得到 m_bounds。它不依赖 Unity 层号，跨关稳定。
        //
        // 用途：1-6（Dynamic）掉出范围的根因是 AppendTerrain 的 Ground/KillPlane
        // 层号扫描在该关拿不到东西 → terrain 为空 → Python 侧「不在 Ground 内即
        // 不可走」这层约束整体失效，角色被寻路引到地板外坠落。这里把 LevelBounds
        // 的 AABB 单独报出去，让 Python 在 Ground 缺失时用它兜底裁剪可走范围。
        void AppendLevelBounds(StringBuilder sb)
        {
            sb.Append('[');
            int i = 0;
            foreach (MonoBehaviour mb in _all)
            {
                if (mb == null || mb.GetType().Name != "LevelBounds") continue;
                object bd = GetField(mb, "m_bounds");
                if (bd == null) continue;
                object mn = GetProp(bd, "min");
                object mx = GetProp(bd, "max");
                if (mn == null || mx == null) continue;
                object ax = GetField(mn, "x"), az = GetField(mn, "z");
                object bx = GetField(mx, "x"), bz = GetField(mx, "z");
                if (ax == null || az == null || bx == null || bz == null) continue;
                float axf = (float)ax, azf = (float)az, bxf = (float)bx, bzf = (float)bz;
                // 未初始化（SetupLevelBounds 尚未跑）的 Bounds 是退化 AABB，跳过
                if (bxf <= axf || bzf <= azf) continue;
                if (i > 0) sb.Append(',');
                sb.Append("{\"minx\":").Append(axf.ToString("0.##"))
                  .Append(",\"maxx\":").Append(bxf.ToString("0.##"))
                  .Append(",\"minz\":").Append(azf.ToString("0.##"))
                  .Append(",\"maxz\":").Append(bzf.ToString("0.##"))
                  .Append('}');
                i++;
            }
            sb.Append(']');
        }

        // 运行时占用表（§5.3 第 5 步），比 nodeMap 权威——nodeMap 11×11 覆盖不全
        // 且关内从不更新（§4.3），这里直接读 GridManager/QuadGridManager 的
        // m_gridOccupancy，每个网格单独一组 cells。
        void AppendOccupancy(StringBuilder sb)
        {
            sb.Append('[');
            int i = 0;
            foreach (MonoBehaviour mb in _all)
            {
                if (mb == null) continue;
                string tn = mb.GetType().Name;
                if (tn != "QuadGridManager" && tn != "GridManager") continue;
                object occ = GetField(mb, "m_gridOccupancy");
                if (!(occ is IEnumerable)) continue;
                StringBuilder cells = new StringBuilder();
                int n = 0;
                foreach (object e in (IEnumerable)occ)
                {
                    object key = GetProp(e, "Key");
                    if (key == null) continue;
                    object gx = GetField(key, "m_x");
                    object gz = GetField(key, "m_z");
                    if (gx == null || gz == null) continue;
                    if (n > 0) cells.Append(',');
                    cells.Append('[').Append(gx).Append(',').Append(gz).Append(']');
                    n++;
                }
                if (n == 0) continue;
                if (i > 0) sb.Append(',');
                sb.Append("{\"grid\":").Append(Jstr(mb.name))
                  .Append(",\"cells\":[").Append(cells).Append("]}");
                i++;
            }
            sb.Append(']');
        }

        // 取代 occupancy.BELTS 关卡硬编码表（§4.5：与手标表 18/18 格零差异，
        // 可证的等价替换）。方向必须用 GetSurfaceVelocity()，不能用
        // m_directionXZ 枚举——后者是局部朝向，两排传送带都报 Rightwards，
        // 真实方向需乘 transform.rotation（§4.5 实测对比表）。
        void AppendBelts(StringBuilder sb)
        {
            // 只扫 Travelator（Sushi_1_4 那种地板齐平的、角色能走上去的传送带）。
            // 1-6 的 ConveyorStation 是台面高度、物料在上面的传送带，角色走不上去，
            // 语义上是「站点/柜台」不是「可走的地板传送带」，不能混进 belt_cells
            // （混进来会让 occupancy 豁免把不可走的台面当成可走）。
            sb.Append('[');
            int i = 0;
            foreach (MonoBehaviour mb in _all)
            {
                if (mb == null || mb.GetType().Name != "Travelator") continue;
                object gi = GetField(mb, "m_gridIndex");
                object gm = GetField(mb, "m_gridManager");
                object vel = CallMethod(mb, "GetSurfaceVelocity");
                if (gi == null) continue;
                object gx = GetField(gi, "m_x"), gz = GetField(gi, "m_z");
                if (gx == null || gz == null) continue;
                float vx = 0f, vz = 0f;
                if (vel is Vector3 v) { vx = v.x; vz = v.z; }
                if (i > 0) sb.Append(',');
                sb.Append("{\"grid\":").Append(gm is UnityEngine.Object ugm && ugm != null
                                               ? Jstr(ugm.name) : "null")
                  .Append(",\"gx\":").Append(gx).Append(",\"gz\":").Append(gz)
                  .Append(",\"vx\":").Append(vx.ToString("0.##"))
                  .Append(",\"vz\":").Append(vz.ToString("0.##"))
                  .Append('}');
                i++;
            }
            sb.Append(']');
        }

        // 1-6 的落火（§4.7）：TTL 16s、批量下落。实时扫 FireHazard（取基类本体，
        // 不是 ServerFireHazard/ClientFireHazard 子类——子类上该字段读不到，
        // §4.7 诊断已验证）。ttl 用 m_lifetime 减去已过去的时间——FireHazard
        // 没有直接暴露"剩余时间"的字段，用 m_destroyTime（诊断里恒为 1，可能是
        // 别的含义）之外没有更好的信号；这里退而用「本对象存在即未到期」的事实：
        // hazards 列表只要包含它、ttl 填 m_lifetime 的名义值即可——Python 侧
        // 只用 hazards 的"存在性"来挡格子（§5.3 第 3 步），不消费精确剩余秒数，
        // ttl 字段留给未来做"等 hazard 消失"类功能时再精确化（spec §10 待办已
        // 排除本次做等待语义）。
        void AppendHazards(StringBuilder sb)
        {
            sb.Append('[');
            int i = 0;
            foreach (MonoBehaviour mb in _all)
            {
                if (mb == null || mb.GetType().Name != "FireHazard") continue;
                object life = GetField(mb, "m_lifetime");
                Vector3 p = mb.transform.position;
                if (i > 0) sb.Append(',');
                sb.Append("{\"t\":\"fire\",\"x\":").Append(p.x.ToString("0.##"))
                  .Append(",\"z\":").Append(p.z.ToString("0.##"))
                  .Append(",\"ttl\":").Append(life == null ? "1.0" : life.ToString())
                  .Append('}');
                i++;
            }
            sb.Append(']');
        }

        // 风区（WindVolume）：反编译确认 WindVolume.GetVelocity() =
        // m_windSpeed * transform.right（enabled 时），m_volumeBounds 是风区
        // AABB。角色走进风区会被持续推着动不了，Python 侧据此在「被风吹得
        // 动不了」时豁免 motion 失败的资源拉黑。
        void AppendWindZones(StringBuilder sb)
        {
            sb.Append('[');
            int i = 0;
            foreach (MonoBehaviour mb in _all)
            {
                if (mb == null || mb.GetType().Name != "WindVolume") continue;
                if (!mb.enabled) continue;   // 关闭的风区不起风，不报
                object spd = GetField(mb, "m_windSpeed");
                float speed = spd is float f ? f : 0f;
                if (speed <= 0f) continue;   // 风速 0 = 无风，不报
                object bd = GetField(mb, "m_volumeBounds");
                if (bd == null) continue;
                object mn = GetProp(bd, "min");
                object mx = GetProp(bd, "max");
                if (mn == null || mx == null) continue;
                object ax = GetField(mn, "x"), az = GetField(mn, "z");
                object bx = GetField(mx, "x"), bz = GetField(mx, "z");
                if (ax == null || az == null || bx == null || bz == null) continue;
                float axf = (float)ax, azf = (float)az, bxf = (float)bx, bzf = (float)bz;
                if (bxf <= axf || bzf <= azf) continue;
                Vector3 right = mb.transform.right;
                if (i > 0) sb.Append(',');
                sb.Append("{\"minx\":").Append(axf.ToString("0.##"))
                  .Append(",\"maxx\":").Append(bxf.ToString("0.##"))
                  .Append(",\"minz\":").Append(azf.ToString("0.##"))
                  .Append(",\"maxz\":").Append(bzf.ToString("0.##"))
                  .Append(",\"vx\":").Append((speed * right.x).ToString("0.##"))
                  .Append(",\"vz\":").Append((speed * right.z).ToString("0.##"))
                  .Append('}');
                i++;
            }
            sb.Append(']');
        }

        // 原来在 BuildStatic() 里的四段 stations 循环，现在每帧单独调用——
        // stations 不是真正的静态数据（§5.5：x/z/gx/gz/AABB 全部要跟着移动平台
        // 刷新），和 cookers/plates/items 一样每帧重新扫 _all 构建，不进
        // _staticJson 缓存。副作用：关内阶段切换后新厨房的站点下一帧自动出现，
        // 不需要额外的 staleness/结构跳变检测（§5.6/§4.9）。
        void AppendStations(StringBuilder sb)
        {
            int si = 0;
            foreach (MonoBehaviour mb in _all)
            {
                if (mb == null) continue;
                string tn = mb.GetType().Name;
                if (tn != "StaticGridLocation" && tn != "DynamicGridLocation") continue;
                string type = ClassifyStation(mb, mb.name);
                if (type == null) continue;
                object gi = GetField(mb, "m_gridIndex");
                Vector3 p = mb.transform.position;
                object stGm = GetField(mb, "m_gridManager");
                // StaticGridLocation 没有 m_gridManager 直接字段时，退而找
                // get_AccessGridManager（IGridLocation 接口，§2 根因 1）。
                if (stGm == null) stGm = CallMethod(mb, "get_AccessGridManager");
                if (si > 0) sb.Append(',');
                sb.Append("{\"t\":").Append(Jstr(type))
                    .Append(",\"n\":").Append(Jstr(mb.name))
                    .Append(",\"grid\":").Append(
                        stGm is UnityEngine.Object ugm && ugm != null ? Jstr(ugm.name) : "null")
                    .Append(",\"gx\":").Append(Fmt(GetField(gi, "m_x")))
                    .Append(",\"gz\":").Append(Fmt(GetField(gi, "m_z")))
                    .Append(",\"x\":").Append(p.x.ToString("0.0"))
                    .Append(",\"z\":").Append(p.z.ToString("0.0"));
                AppendColliderBounds(sb, mb);
                if (type == "crate")
                {
                    sb.Append(",\"ing\":").Append(Jstr(CrateIngredient(mb)));
                    sb.Append(ChopFields(mb));
                }
                sb.Append('}');
                si++;
            }

            // 功能站点：盘架/上菜口、洗碗池（按组件类型找，不依赖 StaticGridLocation）
            foreach (MonoBehaviour mb in _all)
            {
                if (mb == null) continue;
                string tn = mb.GetType().Name;
                string type = null;
                if (tn == "ServerPlateStation") type = "plate_station";
                else if (tn == "ServerWashingStation") type = "washing";
                if (type == null) continue;
                Vector3 p = mb.transform.position;
                // 功能站没有 GridLocation 组件：grid 归属改用 GridManagerObj
                // 逐级回退去找（本组件→同对象其它组件→子物体），gx/gz 用该
                // 网格自己的 origin/cell 反解（GridIndexOn）。找不到网格时
                // 退回主网格投影（旧行为），避免报 null 导致站都看不见。
                object stGm = GridManagerObj(mb);
                string[] gidx = GridIndexOn(stGm, p).Split(',');
                if (si > 0) sb.Append(',');
                sb.Append("{\"t\":").Append(Jstr(type))
                    .Append(",\"n\":").Append(Jstr(mb.name))
                    .Append(",\"grid\":").Append(
                        stGm is UnityEngine.Object ugm2 && ugm2 != null ? Jstr(ugm2.name) : "null")
                    .Append(",\"gx\":").Append(gidx[0])
                    .Append(",\"gz\":").Append(gidx[1])
                    .Append(",\"x\":").Append(p.x.ToString("0.0"))
                    .Append(",\"z\":").Append(p.z.ToString("0.0"));
                AppendColliderBounds(sb, mb);
                if (type == "washing")
                {
                    // 洗碗进度：m_plateCount（待洗盘数）、m_cleaningTimer（当前块进度）
                    sb.Append(",\"plate_count\":").Append(Fmt(GetField(mb, "m_plateCount")))
                        .Append(",\"cleaning_timer\":").Append(Fmt(GetField(mb, "m_cleaningTimer")));
                }
                sb.Append('}');
                si++;
            }

            // 台面传送带（ConveyorStation）：物料在上面的传送带，角色走不上去。
            // 报段位置(gx/gz) + 方向速度(vx/vz)，Python 侧据此去静态段旁拦截，
            // 而不是追移动物料（1-6 的米/鱼/海苔只走这条带）。
            foreach (MonoBehaviour mb in _all)
            {
                if (mb == null || mb.GetType().Name != "ConveyorStation") continue;
                Vector3 p = mb.transform.position;
                object stGm = GridManagerObj(mb);
                string[] gidx = GridIndexOn(stGm, p).Split(',');
                object dir = GetField(mb, "m_conveyanceDirectionXZ");
                object spd = GetField(mb, "m_conveySpeed");
                float speed = spd is float f ? f : 0f;
                float dx = 0f, dz = 0f;
                if (dir is Vector2 v2) { dx = v2.x; dz = v2.y; }
                else if (dir is Vector3 v3) { dx = v3.x; dz = v3.z; }
                if (si > 0) sb.Append(',');
                sb.Append("{\"t\":\"conveyor\",\"n\":").Append(Jstr(mb.name))
                    .Append(",\"grid\":").Append(
                        stGm is UnityEngine.Object ugm && ugm != null ? Jstr(ugm.name) : "null")
                    .Append(",\"gx\":").Append(gidx[0])
                    .Append(",\"gz\":").Append(gidx[1])
                    .Append(",\"x\":").Append(p.x.ToString("0.0"))
                    .Append(",\"z\":").Append(p.z.ToString("0.0"))
                    .Append(",\"vx\":").Append((dx * speed).ToString("0.##"))
                    .Append(",\"vz\":").Append((dz * speed).ToString("0.##"))
                    .Append('}');
                si++;
            }

            // 干净盘子堆：CleanPlateUtensilRespawnBehaviour（每堆一个）
            foreach (MonoBehaviour mb in _all)
            {
                if (mb == null) continue;
                string tn = mb.GetType().Name;
                if (tn != "ServerCleanPlateUtensilRespawnBehaviour" && tn != "CleanPlateUtensilRespawnBehaviour") continue;
                Vector3 p = mb.transform.position;
                if (si > 0) sb.Append(',');
                sb.Append("{\"t\":\"plate_stack\",\"n\":").Append(Jstr(mb.name))
                    .Append(",\"x\":").Append(p.x.ToString("0.0"))
                    .Append(",\"z\":").Append(p.z.ToString("0.0"));
                AppendColliderBounds(sb, mb);
                sb.Append('}');
                si++;
            }

            // 墙体/障碍碰撞体：nodeMap 外围一圈误标可走，需按真实 collider 挡掉
            AppendWallStations(sb, ref si);
        }

        // ---------- 静态: 配方 ----------
        void BuildStatic()
        {
            try
            {
                StringBuilder recipes = new StringBuilder();

                // 原实现硬编码 ServerCampaignFlowController / ClientCampaignFlowController /
                // CampaignFlowController 三个具体类名，只覆盖 Campaign 系列关卡。
                // Dynamic 系列关卡（如 1_6_Dynamic_Lvl_01_2P）走 ServerDynamicFlowController，
                // 不在这三个名字里，于是 recipes 永远为空（实机复现：BuildStatic
                // 整局 1851 次重试一次未成功）。改按接口 IRecipeListCache 查找，
                // 不管具体是哪个子类——元数据反查确认 m_cachedRecipeList 声明在
                // 共享基类 ClientKitchenFlowControllerBase 上，GetCachedRecipeList()
                // 是该接口方法，Campaign/Dynamic 两套框架都实现了它。
                object list = null;
                Type recipeIface = FindType("IRecipeListCache");
                if (recipeIface != null)
                {
                    foreach (MonoBehaviour mb in _all)
                    {
                        if (mb == null || !recipeIface.IsInstanceOfType(mb)) continue;
                        object l = CallMethod(mb, "GetCachedRecipeList");
                        if (l is IEnumerable && Count(l) > 0) { list = l; break; }
                    }
                }
                // 「要不要切」跟着食材走，不跟着箱子走（1-6 鱼只走传送带、无箱子）。
                // 扫一遍全场景的食材 prefab，建 食材名(小写) → [切成什么, 几刀]，
                // 导出配方时把 chop/chop_stages 写进叶子节点。索引用单调缓存：
                // Dynamic 关卡食材开局后才刷出，每帧合并新料，这里读的是累计结果。
                MergeChopIndex(_chopIndexCache, BuildChopIndex());
                Dictionary<string, string[]> chopIndex = _chopIndexCache;
                int ri = 0;
                if (list is IEnumerable)
                {
                    foreach (object node in (IEnumerable)list)
                    {
                        if (ri > 0) recipes.Append(',');
                        string nm = ShortVal(node);
                        string chop = "null";
                        string stages = "null";
                        if (nm != null)
                        {
                            string[] ci;
                            if (chopIndex.TryGetValue(nm.ToLowerInvariant(), out ci))
                            {
                                chop = ci[0];
                                stages = ci[1];
                            }
                        }
                        recipes.Append("{\"name\":").Append(Jstr(nm))
                            .Append(",\"t\":").Append(Jstr(node.GetType().Name))
                            .Append(",\"step\":").Append(Jstr(ShortVal(GetField(node, "m_platingStep"))))
                            .Append(",\"progress\":").Append(Jstr(ShortVal(GetField(node, "m_progress"))))
                            .Append(",\"comp\":").Append(ArrJson(GetField(node, "m_composition")))
                            .Append(",\"cook\":").Append(Jstr(ShortVal(GetField(node, "m_cookingStep"))))
                            .Append(",\"plate\":").Append(Jstr(ShortVal(GetField(node, "m_platingPrefab"))))
                            .Append(",\"ing\":").Append(Jstr(ShortVal(GetField(node, "m_ingriedientOrderNode"))))
                            .Append(",\"chop\":").Append(Jstr(chop))
                            .Append(",\"chop_stages\":").Append(Jstr(stages))
                            .Append('}');
                        ri++;
                        if (ri >= 200) break;
                    }
                }

                if (ri == 0)
                {
                    _staticTries++;
                    // 配方还没就绪（m_cachedRecipeList 尚未填充），持续重试；日志节流避免 5Hz 刷屏
                    if (_staticTries % 25 == 1)
                        Logger.LogInfo("[STATIC] recipes empty, retry " + _staticTries);
                    return;
                }

                _staticJson = "\"recipes\":[" + recipes + "]";
                Logger.LogInfo("[STATIC] built recipes=" + ri);
            }
            catch (Exception e)
            {
                _staticJson = null;
                Logger.LogWarning("[STATIC] error: " + e.Message);
            }
        }

        // 食材名(小写) → [切成什么, 几刀]。
        //
        // 「要不要切」是食材 prefab 上 WorkableItem 的属性，跟箱子无关。旧实现只在
        // 箱子(DispenserCrate)上读它，于是「只走传送带、没有箱子」的食材（1-6 的鱼）
        // 会被当成不用切的料，planner 直接发 FetchToPlate。这里改成从三个来源收集，
        // 与食材从哪来解耦：
        //   ① PickupItemSpawner.m_itemPrefab —— 通用刷出器，传送带供料点也是它；
        //   ② ServerIngredientContainer/IngredientContainer.m_contents —— 容器里的料；
        //   ③ CarryableItem/TeleportableItem —— 场上散落/手持/传送带上的料。
        // ①② 的 prefab 名干净，先扫；③ 的实例名可能带场景后缀，留作兜底补漏。
        // 低频重扫 chop 索引：覆盖 Dynamic 关卡开局后延迟刷出的食材（鱼/米/海苔）。
        // Invoke 自调度（约每 2 秒一次），连续 3 次无新键即收敛停止——稳定后零开销，
        // 不再像旧实现那样在每帧 BuildState 里全场景反射（2543 组件拖到 665ms）。
        // 首次调度在 Awake，切关时在 RefreshLevel 重启。
        void RescanChopIndex()
        {
            if (!IsInLevel())
            {
                Invoke("RescanChopIndex", 2f);   // 菜单/加载中：等进关再扫
                return;
            }
            if (_staticJson == null)
            {
                Invoke("RescanChopIndex", 1f);   // recipes 还没建好：下一拍 BuildStatic 后再扫
                return;
            }
            int before = _chopIndexCache.Count;
            MergeChopIndex(_chopIndexCache, BuildChopIndex());
            if (_chopIndexCache.Count > before)
            {
                _chopNoNew = 0;                  // 有新食材，重置收敛计数
                Logger.LogInfo("[CHOPIDX] new ingredients, rebuild recipes (cache="
                    + _chopIndexCache.Count + ")");
                _staticJson = null;              // 下一拍 BuildState 重建 recipes
                Invoke("RescanChopIndex", 1f);   // 可能还有更多在刷出，快一点再扫
                return;
            }
            _chopNoNew++;
            if (_chopNoNew < 3) Invoke("RescanChopIndex", 2f);
            // 收敛（连续 3 次无新键）：不再调度，零开销。
        }

        Dictionary<string, string[]> BuildChopIndex()
        {
            var index = new Dictionary<string, string[]>();
            try
            {
                Type wt = FindType("WorkableItem");
                if (wt == null) return index;
                foreach (MonoBehaviour mb in _all)
                {
                    if (mb == null) continue;
                    string tn = mb.GetType().Name;
                    if (tn == "PickupItemSpawner")
                    {
                        AddChopFrom(index, wt, GetField(mb, "m_itemPrefab"));
                    }
                    else if (tn == "ServerIngredientContainer" || tn == "IngredientContainer")
                    {
                        object contents = GetField(mb, "m_contents");
                        if (contents is IEnumerable)
                        {
                            foreach (object e in (IEnumerable)contents)
                                AddChopFrom(index, wt, e);
                        }
                    }
                }
                foreach (MonoBehaviour mb in _all)
                {
                    if (mb == null) continue;
                    string tn = mb.GetType().Name;
                    if (tn.IndexOf("CarryableItem") >= 0 || tn.IndexOf("TeleportableItem") >= 0)
                        AddChopFrom(index, wt, mb.gameObject);
                }
            }
            catch (Exception e)
            {
                Logger.LogWarning("[CHOPIDX] build error: " + e.Message);
            }
            return index;
        }

        // 把 fresh 里 cache 没有的键合并进去（只增不减）。见 _chopIndexCache 字段注释。
        void MergeChopIndex(Dictionary<string, string[]> cache, Dictionary<string, string[]> fresh)
        {
            foreach (var kv in fresh)
                if (!cache.ContainsKey(kv.Key)) cache[kv.Key] = kv.Value;
        }

        // 从一个食材 prefab/实例读 WorkableItem，把 name → [chop, stages] 记进 index。
        // 已存在的键跳过（第一遍刷出器/容器的干净名优先）。chop 与 stages 都为 null
        // 表示不可切，不记——查表 miss 走 null，效果相同。
        void AddChopFrom(Dictionary<string, string[]> index, Type wt, object obj)
        {
            try
            {
                if (obj == null) return;
                GameObject go = null;
                if (obj is GameObject g) go = g;
                else if (obj is Component c) go = c.gameObject;
                if (go == null) return;
                string name = go.name;
                if (string.IsNullOrEmpty(name)) return;
                string key = name.ToLowerInvariant();
                if (index.ContainsKey(key)) return;
                Component w = go.GetComponent(wt);
                if (w == null)
                {
                    // 散落/传送带上的食材实例可能只挂 ServerWorkableItem（运行时切配状态），
                    // 真正的切配数据 WorkableItem 由其 m_workable 字段引用。箱子那条路读
                    // 的是 prefab(m_itemPrefab)/容器内容，直接有 WorkableItem，走不到这里。
                    Type swt = FindType("ServerWorkableItem");
                    if (swt == null) return;
                    Component sw = go.GetComponent(swt);
                    if (sw == null) return;
                    w = GetField(sw, "m_workable") as Component;
                    if (w == null) return;
                }
                object next = GetField(w, "m_nextPrefab");
                object stages = GetField(w, "m_stages");
                string chop = (next is UnityEngine.Object uo && uo != null) ? uo.name : "null";
                string st = (stages == null ? "null" : stages.ToString());
                if (chop == "null" && st == "null") return;
                index[key] = new string[] { chop, st };
            }
            catch (Exception e)
            {
                Logger.LogWarning("[CHOPIDX] error on " + (obj != null ? obj.GetType().Name : "null") + ": " + e.Message);
            }
        }

        // 站点物理占用：取 GameObject（含子物体）上所有非触发 Collider 的世界 AABB 并集。
        // 出餐口这类功能站不注册进 GridManager 占用表、nodeMap 又标可走，只能靠 collider 兜底。
        void AppendColliderBounds(StringBuilder sb, MonoBehaviour mb)
        {
            try
            {
                Component[] comps = mb.GetComponentsInChildren<Component>(true);
                float minX = float.MaxValue, minZ = float.MaxValue;
                float maxX = float.MinValue, maxZ = float.MinValue;
                bool found = false;
                foreach (Component c in comps)
                {
                    if (c == null) continue;
                    if (c.GetType().Name.ToLowerInvariant().IndexOf("collider") < 0) continue;
                    object trigger = GetProp(c, "isTrigger");
                    if (trigger is bool t && t) continue;   // 触发体不挡人
                    object bounds = GetProp(c, "bounds");
                    if (bounds == null) continue;
                    object mn = GetProp(bounds, "min");
                    object mx = GetProp(bounds, "max");
                    if (mn == null || mx == null) continue;
                    object ax = GetField(mn, "x"), az = GetField(mn, "z");
                    object bx = GetField(mx, "x"), bz = GetField(mx, "z");
                    if (ax == null || az == null || bx == null || bz == null) continue;
                    float axf = (float)ax, azf = (float)az, bxf = (float)bx, bzf = (float)bz;
                    if (bxf <= axf || bzf <= azf) continue; // 空/退化 bounds（如禁用 collider）
                    if (axf < minX) minX = axf;
                    if (azf < minZ) minZ = azf;
                    if (bxf > maxX) maxX = bxf;
                    if (bzf > maxZ) maxZ = bzf;
                    found = true;
                }
                if (!found) return;
                sb.Append(",\"minx\":").Append(minX.ToString("0.##"))
                  .Append(",\"minz\":").Append(minZ.ToString("0.##"))
                  .Append(",\"maxx\":").Append(maxX.ToString("0.##"))
                  .Append(",\"maxz\":").Append(maxZ.ToString("0.##"));
            }
            catch (Exception e)
            {
                Logger.LogWarning("[STATIC] collider bounds error: " + e.Message);
            }
        }

        // 关卡墙体/障碍/地板碰撞体（Col_Walls / Col_Objects / Col_Floor）。
        // 这些是纯 GameObject 上的 collider，不进 GridManager 占用表、nodeMap
        // 又只覆盖 gz=-4..4（GridNavSpace 半尺寸 7x4），报成站点交给 Python：
        //   - Col_Walls/Col_Objects → "wall"，station_blocked_cells 按 AABB 标不可走
        //   - Col_Floor → "floor"，world.py 用它的 minz 把 bounds 扩到底部地板，
        //     make_walkable 再把 nodeMap 外的底部两行当作可走（连接左右半边的通道）
        // 跳过角色胶囊（CapsuleCollider）。
        void AppendWallStations(StringBuilder sb, ref int si)
        {
            try
            {
                Type goType = FindType("UnityEngine.GameObject");
                if (goType == null) goType = FindType("GameObject");
                MethodInfo mi = typeof(UnityEngine.Object).GetMethod(
                    "FindObjectsOfType", BindingFlags.Public | BindingFlags.Static,
                    null, new Type[] { typeof(Type) }, null);
                object[] gos = (mi != null && goType != null)
                    ? (object[])mi.Invoke(null, new object[] { goType })
                    : null;
                if (gos == null) return;
                List<string> seen = new List<string>();
                foreach (object o in gos)
                {
                    GameObject go = o as GameObject;
                    if (go == null) continue;
                    foreach (Component c in go.GetComponents<Component>())
                    {
                        if (c == null) continue;
                        if (c.GetType().Name.ToLowerInvariant().IndexOf("collider") < 0) continue;
                        string nm = c.name;
                        if (nm == null) continue;
                        string stype;
                        if (nm.StartsWith("Col_Floor"))
                            stype = "floor";       // 地板：可走，用于扩展 bounds
                        else if (nm.StartsWith("Col_Walls") || nm.StartsWith("Col_Objects"))
                            stype = "wall";        // 墙/障碍：不可走
                        else
                            continue;
                        object trigger = GetProp(c, "isTrigger");
                        if (trigger is bool t && t) continue;
                        object bounds = GetProp(c, "bounds");
                        if (bounds == null) continue;
                        object mn = GetProp(bounds, "min");
                        object mx = GetProp(bounds, "max");
                        if (mn == null || mx == null) continue;
                        object ax = GetField(mn, "x"), az = GetField(mn, "z");
                        object bx = GetField(mx, "x"), bz = GetField(mx, "z");
                        if (ax == null || az == null || bx == null || bz == null) continue;
                        float axf = (float)ax, azf = (float)az, bxf = (float)bx, bzf = (float)bz;
                        if (bxf <= axf || bzf <= azf) continue;
                        string key = nm + "|" + axf.ToString("0.0") + "," + azf.ToString("0.0")
                            + "," + bxf.ToString("0.0") + "," + bzf.ToString("0.0");
                        if (seen.Contains(key)) continue;
                        seen.Add(key);
                        if (si > 0) sb.Append(',');
                        sb.Append("{\"t\":").Append(Jstr(stype)).Append(",\"n\":").Append(Jstr(nm))
                          .Append(",\"minx\":").Append(axf.ToString("0.##"))
                          .Append(",\"minz\":").Append(azf.ToString("0.##"))
                          .Append(",\"maxx\":").Append(bxf.ToString("0.##"))
                          .Append(",\"maxz\":").Append(bzf.ToString("0.##"))
                          .Append('}');
                        si++;
                    }
                }
            }
            catch (Exception e) { Logger.LogWarning("[WALL] error: " + e.Message); }
        }

        string ClassifyStation(MonoBehaviour mb, string go)
        {
            string g = go.ToLowerInvariant();
            if (g.Contains("dispensercrate")) return "crate";
            if (HasComponent(mb, "platereturnstation")) return "plate_return";
            if (HasComponent(mb, "platestation")) return "plate_station";
            if (HasComponent(mb, "rubbishbin") || g.Contains("bin")) return "bin";
            if (HasComponent(mb, "choppingstation") || g.Contains("chopping")) return "chopping";
            if (HasComponent(mb, "heatedstation") || HasComponent(mb, "cookingstation")) return "cooking";
            if (HasComponent(mb, "workstation")) return "workstation";
            if (g.Contains("plate")) return "plate_station";
            if (g.Contains("countertop") || g.Contains("standard")) return "counter";
            return null;
        }

        string CrateIngredient(MonoBehaviour mb)
        {
            try
            {
                foreach (Component c in mb.GetComponents<Component>())
                {
                    if (c == null) continue;
                    string tn = c.GetType().Name;
                    if (tn == "ServerIngredientContainer" || tn == "IngredientContainer")
                    {
                        object contents = GetField(c, "m_contents");
                        if (contents is IEnumerable)
                        {
                            foreach (object e in (IEnumerable)contents)
                                if (e != null) return ShortVal(e);
                        }
                    }
                    else if (tn == "PickupItemSpawner")
                    {
                        object prefab = GetField(c, "m_itemPrefab");
                        if (prefab is UnityEngine.Object uo && uo != null) return uo.name;
                    }
                }
            }
            catch { }
            return null;
        }

        string ChopFields(MonoBehaviour mb)
        {
            // crate 食材的 WorkableItem：m_nextPrefab=切成什么，m_stages=几刀
            try
            {
                Type wt = FindType("WorkableItem");
                if (wt == null) return ",\"chop\":null,\"chop_stages\":null";
                foreach (Component c in mb.GetComponents<Component>())
                {
                    if (c == null) continue;
                    string tn = c.GetType().Name;
                    object item = null;
                    if (tn == "ServerIngredientContainer" || tn == "IngredientContainer")
                    {
                        object contents = GetField(c, "m_contents");
                        if (contents is IEnumerable)
                        {
                            foreach (object e in (IEnumerable)contents)
                            {
                                if (e != null) { item = e; break; }
                            }
                        }
                    }
                    else if (tn == "PickupItemSpawner")
                    {
                        item = GetField(c, "m_itemPrefab");
                    }
                    if (item == null) continue;
                    GameObject go = null;
                    if (item is GameObject g) go = g;
                    else if (item is Component comp) go = comp.gameObject;
                    if (go == null) continue;
                    Component w = go.GetComponent(wt);
                    if (w == null) return ",\"chop\":null,\"chop_stages\":null";
                    object next = GetField(w, "m_nextPrefab");
                    object stages = GetField(w, "m_stages");
                    string chop = (next is UnityEngine.Object uo && uo != null) ? uo.name : "null";
                    return ",\"chop\":" + Jstr(chop) + ",\"chop_stages\":" + (stages == null ? "null" : stages.ToString());
                }
            }
            catch { }
            return ",\"chop\":null,\"chop_stages\":null";
        }

        string ArrJson(object arr)
        {
            if (!(arr is IEnumerable)) return "[]";
            StringBuilder sb = new StringBuilder("[");
            int i = 0;
            foreach (object e in (IEnumerable)arr)
            {
                if (i > 0) sb.Append(',');
                sb.Append(Jstr(ShortVal(e)));
                i++;
                if (i >= 12) break;
            }
            sb.Append(']');
            return sb.ToString();
        }
    }
}
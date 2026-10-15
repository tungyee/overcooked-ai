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
    [BepInPlugin("com.oc2.aiplaymate", "OC2 AI Playmate", "0.13.0")]
    public class Oc2AIPlugin : BaseUnityPlugin
    {
        const int Port = 27411;

        MonoBehaviour[] _all;
        bool _inputDumped;
        bool _orderFieldsDumped;
        bool _discoveryDone;
        string _staticJson;
        string _level;
        int _staticTries;
        int _droppedStates;

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

        TcpListener _listener;
        Thread _acceptThread;
        Thread _writeThread;
        readonly object _lock = new object();
        readonly List<TcpClient> _clients = new List<TcpClient>();
        readonly Queue<string> _out = new Queue<string>();

        void Awake()
        {
            Logger.LogInfo("[OC2AI v0.12] loaded");
            StartSocket();
            InvokeRepeating("Snapshot", 3f, 0.2f);
        }

        void Snapshot()
        {
            try
            {
                // 每拍重扫，避免新出现的灭火器/掉落物延迟 1s 才进状态
                _all = UnityEngine.Object.FindObjectsOfType<MonoBehaviour>();
                if (!_inputDumped) { _inputDumped = true; LogInput(); }
                RefreshLevel();
                // 不在关卡内（菜单 / 选关 / 加载中）就不序列化、不推帧：
                // 这些帧 phase != "level"，agent 侧本来也跳过决策，纯属白跑；
                // 而且 BuildState + 全量 LogInfo 每 0.2s 跑一次会把游戏卡到选关页掉帧
                // （实机：游戏进程 150% CPU、agent 只有 0.6%）。推给 agent 也没用。
                if (!IsInLevel()) return;
                if (!_discoveryDone) { _discoveryDone = true; Discover(); }
                string state = BuildState();
                // 不再把整帧状态写进游戏日志：这是卡顿大户，决策复盘走 Python 侧
                // trace-*.jsonl，不依赖这里的 [STATE] 日志。
                Push(state);
            }
            catch (Exception e)
            {
                Logger.LogWarning("[STATE] error: " + e.Message);
            }
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
            while (true)
            {
                try { TcpClient c = _listener.AcceptTcpClient(); lock (_lock) _clients.Add(c); }
                catch { return; }
            }
        }

        void WriteLoop()
        {
            while (true)
            {
                string msg = null;
                lock (_lock) { if (_out.Count > 0) msg = _out.Dequeue(); }
                if (msg == null) { Thread.Sleep(20); continue; }
                byte[] data = Encoding.UTF8.GetBytes(msg);
                List<TcpClient> dead = null;
                lock (_lock)
                {
                    foreach (TcpClient c in _clients)
                    {
                        try { NetworkStream ns = c.GetStream(); if (ns.CanWrite) ns.Write(data, 0, data.Length); }
                        catch { if (dead == null) dead = new List<TcpClient>(); dead.Add(c); }
                    }
                    if (dead != null)
                        foreach (TcpClient d in dead) { _clients.Remove(d); try { d.Close(); } catch { } }
                }
            }
        }

        void Push(string json)
        {
            lock (_lock)
            {
                if (_out.Count < 20)
                {
                    _out.Enqueue(json + "\n");
                }
                else if (_droppedStates++ % 50 == 0)
                {
                    Logger.LogWarning("[SOCKET] out queue full, dropping state #" + _droppedStates);
                }
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
            return FindMB("ServerCampaignFlowController") != null;
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
            if (_level == null)
            {
                _level = cur;
                return;
            }
            if (cur != _level)
            {
                _level = cur;
                _staticJson = null;       // 下一拍 BuildState() 会重建 stations/recipes
                // 网格校准也要重来：_originX/Z 是从上一关的 StaticGridLocation
                // 反解出来的，切关后仍沿用会让功能站（出餐口/洗碗池）的 gx/gz
                // 全错（实机：1-4 切到 1-3，出餐口被算成 -13,8 而非 -6,-4）。
                _gridReady = false;
                _originX = 0f;
                _originZ = 0f;
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
            MonoBehaviour qgm = FindMB("QuadGridManager");
            if (qgm == null) qgm = FindMB("GridManager");
            if (qgm != null)
            {
                object size = GetField(qgm, "m_size");
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

        // ---------- 状态 ----------
        string BuildState()
        {
            if (_staticJson == null && IsInLevel()) BuildStatic();

            StringBuilder sb = new StringBuilder("{");
            sb.Append("\"phase\":").Append(Jstr(Phase()))
              .Append(",\"level\":").Append(Jstr(_level))
              .Append(",\"paused\":").Append(IsPaused() ? "true" : "false");

            sb.Append(",\"score\":");
            AppendScore(sb);

            sb.Append(",\"timer\":{");
            AppendTimer(sb, FindMB("ClientCampaignFlowController"), "client");
            sb.Append(',');
            AppendTimer(sb, FindMB("ServerCampaignFlowController"), "server");
            sb.Append("}");

            sb.Append(",\"orders\":[");
            AppendOrders(sb);
            sb.Append("]");

            sb.Append(",\"chefs\":[");
            int chefCount = AppendChefs(sb);
            sb.Append("],\"chefCount\":").Append(chefCount);
            AppendChopMonitor(sb);

            sb.Append(",\"plates\":[");
            AppendPlates(sb);
            sb.Append("]");

            sb.Append(",\"workstations\":[");
            AppendWorkstations(sb);
            sb.Append("]");

            sb.Append(",\"cookers\":[");
            AppendCookers(sb);
            sb.Append("]");

            sb.Append(",\"extinguishers\":[");
            AppendExtinguishers(sb);
            sb.Append("]");

            sb.Append(",\"items\":[");
            AppendItems(sb);
            sb.Append("]");

            sb.Append(",\"grid\":");
            AppendGrid(sb);

            if (_staticJson != null) sb.Append(',').Append(_staticJson);

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
            if (FindMB("ServerCampaignFlowController") == null) return "menu";
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
            object score = GetField(GetField(FindMB("ServerCampaignFlowController"), "m_teamMonitor"), "m_score");
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
            object ao = GetField(GetField(GetField(FindMB("ServerCampaignFlowController"), "m_teamMonitor"), "m_ordersController"), "m_activeOrders");
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

                sb.Append("{\"name\":").Append(Jstr(mb.name))
                  .Append(",\"x\":").Append(p.x.ToString("0.00"))
                  .Append(",\"z\":").Append(p.z.ToString("0.00"))
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

        void AppendPlates(StringBuilder sb)
        {
            int i = 0;
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
                sb.Append("{\"n\":").Append(Jstr(mb.name))
                  .Append(",\"x\":").Append(p.x.ToString("0.0"))
                  .Append(",\"z\":").Append(p.z.ToString("0.0"))
                  .Append(",\"isCooking\":").Append(isCooking is bool b ? (b ? "true" : "false") : "null")
                  .Append(",\"isTurnedOn\":").Append(isTurnedOn is bool b2 ? (b2 ? "true" : "false") : "null")
                  .Append(",\"cooked\":").Append(CallBool(pot, "IsCooked"))
                  .Append(",\"burning\":").Append(CallBool(pot, "IsBurning"))
                  .Append(",\"onFire\":").Append(onFire)
                  .Append(",\"cooking_progress\":").Append(Fmt(cookingProgress))
                  .Append(",\"cooking_time\":").Append(Fmt(cookingTime))
                  .Append(",\"pot_contents\":").Append(PotContents(pot))
                  .Append('}');
                i++;
                if (i >= 8) break;
            }
        }

        void AppendExtinguishers(StringBuilder sb)
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
                  .Append('}');
                i++;
                if (i >= 8) break;
            }
        }

        void AppendItems(StringBuilder sb)
        {
            // 散落物品（可携带物：地面/柜台/传送带上的原料、鱼片、盘子等）
            int i = 0;
            foreach (MonoBehaviour mb in _all)
            {
                if (mb == null) continue;
                string tn = mb.GetType().Name;
                if (tn.IndexOf("CarryableItem") < 0 && tn.IndexOf("TeleportableItem") < 0) continue;
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
                  .Append('}');
                i++;
                if (i >= 32) break;
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

        // ---------- 静态: 站点 + 配方 ----------
        void BuildStatic()
        {
            try
            {
                StringBuilder stations = new StringBuilder();
                StringBuilder recipes = new StringBuilder();

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
                    if (si > 0) stations.Append(',');
                    stations.Append("{\"t\":").Append(Jstr(type))
                        .Append(",\"n\":").Append(Jstr(mb.name))
                        .Append(",\"gx\":").Append(Fmt(GetField(gi, "m_x")))
                        .Append(",\"gz\":").Append(Fmt(GetField(gi, "m_z")))
                        .Append(",\"x\":").Append(p.x.ToString("0.0"))
                        .Append(",\"z\":").Append(p.z.ToString("0.0"));
                    AppendColliderBounds(stations, mb);
                    if (type == "crate")
                    {
                        stations.Append(",\"ing\":").Append(Jstr(CrateIngredient(mb)));
                        stations.Append(ChopFields(mb));
                    }
                    stations.Append('}');
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
                    string[] gidx = GridIndex(p).Split(',');
                    if (si > 0) stations.Append(',');
                    stations.Append("{\"t\":").Append(Jstr(type))
                        .Append(",\"n\":").Append(Jstr(mb.name))
                        .Append(",\"gx\":").Append(gidx[0])
                        .Append(",\"gz\":").Append(gidx[1])
                        .Append(",\"x\":").Append(p.x.ToString("0.0"))
                        .Append(",\"z\":").Append(p.z.ToString("0.0"));
                    AppendColliderBounds(stations, mb);
                    if (type == "washing")
                    {
                        // 洗碗进度：m_plateCount（待洗盘数）、m_cleaningTimer（当前块进度）
                        stations.Append(",\"plate_count\":").Append(Fmt(GetField(mb, "m_plateCount")))
                            .Append(",\"cleaning_timer\":").Append(Fmt(GetField(mb, "m_cleaningTimer")));
                    }
                    stations.Append('}');
                    si++;
                }

                // 干净盘子堆：CleanPlateUtensilRespawnBehaviour（每堆一个）
                foreach (MonoBehaviour mb in _all)
                {
                    if (mb == null) continue;
                    string tn = mb.GetType().Name;
                    if (tn != "ServerCleanPlateUtensilRespawnBehaviour" && tn != "CleanPlateUtensilRespawnBehaviour") continue;
                    Vector3 p = mb.transform.position;
                    if (si > 0) stations.Append(',');
                    stations.Append("{\"t\":\"plate_stack\",\"n\":").Append(Jstr(mb.name))
                        .Append(",\"x\":").Append(p.x.ToString("0.0"))
                        .Append(",\"z\":").Append(p.z.ToString("0.0"));
                    AppendColliderBounds(stations, mb);
                    stations.Append('}');
                    si++;
                }

                // 墙体/障碍碰撞体：nodeMap 外围一圈误标可走，需按真实 collider 挡掉
                AppendWallStations(stations, ref si);

                object list = null;
                foreach (string nm in new[] { "ServerCampaignFlowController", "ClientCampaignFlowController", "CampaignFlowController" })
                {
                    MonoBehaviour fc = FindMB(nm);
                    if (fc == null) continue;
                    object l = GetField(fc, "m_cachedRecipeList");
                    if (l is IEnumerable && Count(l) > 0) { list = l; break; }
                }
                int ri = 0;
                if (list is IEnumerable)
                {
                    foreach (object node in (IEnumerable)list)
                    {
                        if (ri > 0) recipes.Append(',');
                        recipes.Append("{\"name\":").Append(Jstr(ShortVal(node)))
                            .Append(",\"t\":").Append(Jstr(node.GetType().Name))
                            .Append(",\"step\":").Append(Jstr(ShortVal(GetField(node, "m_platingStep"))))
                            .Append(",\"progress\":").Append(Jstr(ShortVal(GetField(node, "m_progress"))))
                            .Append(",\"comp\":").Append(ArrJson(GetField(node, "m_composition")))
                            .Append(",\"cook\":").Append(Jstr(ShortVal(GetField(node, "m_cookingStep"))))
                            .Append(",\"plate\":").Append(Jstr(ShortVal(GetField(node, "m_platingPrefab"))))
                            .Append(",\"ing\":").Append(Jstr(ShortVal(GetField(node, "m_ingriedientOrderNode"))))
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

                _staticJson = "\"stations\":[" + stations + "],\"recipes\":[" + recipes + "]";
                Logger.LogInfo("[STATIC] built stations=" + si + " recipes=" + ri);
            }
            catch (Exception e)
            {
                _staticJson = null;
                Logger.LogWarning("[STATIC] error: " + e.Message);
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
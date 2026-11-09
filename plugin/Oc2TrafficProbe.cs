// 4-1「红绿灯寿司街」车辆/信号灯诊断：只写日志，不改行为。
//
// 目标：先用一局真机日志确认游戏里车辆、红绿灯分别是什么组件，再决定
// NDJSON 协议里暴露哪些字段（见 docs/agent/status-backlog.md 的 4-1 待办）。
//
// 判据不依赖具体类名：
//   - 类型/名字含 traffic/car/vehicle/cross/road/signal/wheel 的对象 → 打字段；
//   - 马路条带内移动的 MonoBehaviour / Rigidbody → 打速度与轨迹；
//   - 条带内的 Collider/Animator/Light → 打几何与状态，便于反推车/灯。
//
// 输出前缀统一 [TRAFFIC-*]，只在关卡名含 "4_1" 时启用，1 Hz。
using System;
using System.Collections.Generic;
using System.Reflection;
using System.Text;
using UnityEngine;

static class Oc2TrafficProbe
{
    static float _next;
    static string _lastLevel;
    static bool _typesDumped;

    // Sushi_4_1 的马路条带（世界坐标）。主网格 gx -3..6 ≈ x 21.2..32，
    // z 覆盖整条街；向外各放 2~3 格，避免车体中心刚好落在边界外。
    const float MinX = 18.5f, MaxX = 34.5f, MinZ = -6.0f, MaxZ = 16.5f;
    // 灯/触发器等可能在路沿外侧，用更宽的搜索区。
    const float NearMinX = 10.0f, NearMaxX = 40.0f;
    const float NearMinZ = -10.0f, NearMaxZ = 20.0f;

    static readonly Dictionary<int, Vector3> _lastPos = new Dictionary<int, Vector3>();
    static readonly Dictionary<int, float> _lastTime = new Dictionary<int, float>();
    static readonly Dictionary<int, Vector3> _rootPos = new Dictionary<int, Vector3>();
    static readonly Dictionary<int, float> _rootTime = new Dictionary<int, float>();
    static readonly Dictionary<int, float> _lastMoveLog = new Dictionary<int, float>();
    static readonly HashSet<int> _collLogged = new HashSet<int>();
    static readonly HashSet<int> _keyLogged = new HashSet<int>();
    static readonly HashSet<int> _lightLogged = new HashSet<int>();
    static readonly HashSet<int> _allLogged = new HashSet<int>();
    static readonly HashSet<int> _rbLogged = new HashSet<int>();
    static readonly HashSet<int> _animLogged = new HashSet<int>();
    static int _objLoggedCount;
    const int ObjLogCap = 120;

    static bool InRoad(Vector3 p)
    {
        return p.x >= MinX && p.x <= MaxX && p.z >= MinZ && p.z <= MaxZ;
    }

    static bool Near(Vector3 p)
    {
        return p.x >= NearMinX && p.x <= NearMaxX && p.z >= NearMinZ && p.z <= NearMaxZ;
    }

    static string Path(Transform t)
    {
        if (t == null) return "";
        StringBuilder sb = new StringBuilder(t.name);
        Transform cur = t;
        int guard = 0;
        while (cur.parent != null && guard++ < 32)
        {
            cur = cur.parent;
            sb.Insert(0, cur.name + "/");
        }
        return sb.ToString();
    }

    static string Types(GameObject go)
    {
        if (go == null) return "";
        StringBuilder sb = new StringBuilder();
        foreach (Component c in go.GetComponents<Component>())
        {
            if (c == null) continue;
            if (sb.Length > 0) sb.Append(',');
            sb.Append(c.GetType().Name);
        }
        return sb.ToString();
    }

    static string V3(Vector3 v)
    {
        return "(" + v.x.ToString("0.00") + "," + v.y.ToString("0.00")
            + "," + v.z.ToString("0.00") + ")";
    }

    static string V2(float x, float z)
    {
        return "(" + x.ToString("0.00") + "," + z.ToString("0.00") + ")";
    }

    static bool Keyword(string s)
    {
        if (string.IsNullOrEmpty(s)) return false;
        string l = s.ToLowerInvariant();
        return l.IndexOf("traffic") >= 0 || l.IndexOf("vehicle") >= 0
            || l.IndexOf("crosswalk") >= 0 || l.IndexOf("crossing") >= 0
            || l.IndexOf("signal") >= 0 || l.IndexOf("wheel") >= 0
            || l.IndexOf("road") >= 0 || l.IndexOf("street") >= 0
            || l.IndexOf("pedestrian") >= 0 || l.IndexOf("taxi") >= 0
            || l.IndexOf("truck") >= 0 || l.IndexOf("bus") >= 0
            || l.IndexOf(" car") >= 0 || l.IndexOf("_car") >= 0
            || l.IndexOf("car_") >= 0;
    }

    static string Short(object v)
    {
        if (v == null) return null;
        if (v is string) return (string)v;
        if (v is bool) return ((bool)v) ? "true" : "false";
        if (v is Vector3) return V3((Vector3)v);
        if (v is Vector2) { Vector2 q = (Vector2)v; return V2(q.x, q.y); }
        if (v is Color) { Color c = (Color)v; return "rgba(" + c.r.ToString("0.00") + "," + c.g.ToString("0.00") + "," + c.b.ToString("0.00") + "," + c.a.ToString("0.00") + ")"; }
        if (v is Enum) return v.ToString();
        if (v is float || v is double || v is int || v is long || v is short || v is byte)
            return Convert.ToString(v, System.Globalization.CultureInfo.InvariantCulture);
        return null; // 复杂对象跳过，避免日志爆炸
    }

    static void DumpFields(object obj, string prefix, Action<string> log)
    {
        if (obj == null) return;
        FieldInfo[] fs = obj.GetType().GetFields(
            BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic);
        int n = 0;
        foreach (FieldInfo f in fs)
        {
            object v;
            try { v = f.GetValue(obj); } catch { continue; }
            string s = Short(v);
            if (s == null) continue;
            log("[TRAFFIC-FIELD] " + prefix + "." + f.Name + "=" + s);
            if (++n >= 40) break;
        }
    }

    static void Track(int id, Transform tr, string desc, float now, Action<string> log)
    {
        if (tr == null) return;
        Vector3 p = tr.position;
        Vector3 q;
        float pt;
        if (_lastPos.TryGetValue(id, out q) && _lastTime.TryGetValue(id, out pt))
        {
            float dt = now - pt;
            if (dt > 0.05f)
            {
                Vector3 v = (p - q) / dt;
                float sp = Mathf.Sqrt(v.x * v.x + v.z * v.z);
                if (sp > 0.4f)
                {
                    float last;
                    if (!_lastMoveLog.TryGetValue(id, out last) || now - last > 2f)
                    {
                        _lastMoveLog[id] = now;
                        log("[TRAFFIC-MOVE] " + desc + " pos=" + V2(p.x, p.z)
                            + " vel=" + V2(v.x, v.z) + " speed=" + sp.ToString("0.00"));
                    }
                }
            }
        }
        _lastPos[id] = p;
        _lastTime[id] = now;
    }

    static string AnimParams(Animator a)
    {
        if (a == null || a.parameters == null) return "";
        StringBuilder sb = new StringBuilder();
        foreach (AnimatorControllerParameter p in a.parameters)
        {
            if (sb.Length > 0) sb.Append(',');
            switch (p.type)
            {
                case AnimatorControllerParameterType.Bool:
                    sb.Append(p.name).Append('=').Append(a.GetBool(p.name));
                    break;
                case AnimatorControllerParameterType.Float:
                    sb.Append(p.name).Append('=').Append(a.GetFloat(p.name).ToString("0.00"));
                    break;
                case AnimatorControllerParameterType.Int:
                    sb.Append(p.name).Append('=').Append(a.GetInteger(p.name));
                    break;
                default:
                    sb.Append(p.name).Append("=trigger");
                    break;
            }
        }
        return sb.ToString();
    }

    static void LogRoots(MonoBehaviour[] all, float now, Action<string> log)
    {
        // 4-1 的两辆车和两盏灯：名字固定，按根对象每秒打一条干净时间线，
        // 不用在几千个组件里靠速度筛。
        foreach (MonoBehaviour mb in all)
        {
            if (mb == null) continue;
            string nm = mb.name;
            string path = Path(mb.transform);
            bool isCar = (nm == "Blue_Car" || nm == "Green_Car")
                && path.IndexOf("AnimatedObjects") >= 0;
            bool isLight = nm.StartsWith("traffic_light_animated");
            if (!isCar && !isLight) continue;

            Vector3 p = mb.transform.position;
            int id = mb.GetInstanceID();
            float vx = 0f, vz = 0f, speed = 0f;
            Vector3 q;
            float pt;
            if (_rootPos.TryGetValue(id, out q) && _rootTime.TryGetValue(id, out pt))
            {
                float dt = now - pt;
                if (dt > 0.05f)
                {
                    vx = (p.x - q.x) / dt;
                    vz = (p.z - q.z) / dt;
                    speed = Mathf.Sqrt(vx * vx + vz * vz);
                }
            }
            _rootPos[id] = p;
            _rootTime[id] = now;

            Animator anim = mb.GetComponent<Animator>();
            string clip = "";
            float norm = 0f;
            if (anim != null)
            {
                AnimatorClipInfo[] infos = anim.GetCurrentAnimatorClipInfo(0);
                if (infos != null && infos.Length > 0 && infos[0].clip != null)
                    clip = infos[0].clip.name;
                norm = anim.GetCurrentAnimatorStateInfo(0).normalizedTime;
            }

            string extra = "";
            if (isLight)
            {
                // 灯可能是「换材质」而不是换灯光：把根和子物体的材质都打出来。
                foreach (MeshRenderer mr in mb.GetComponentsInChildren<MeshRenderer>())
                {
                    if (mr == null || mr.sharedMaterial == null) continue;
                    extra += " mat=" + mr.name + ":" + mr.sharedMaterial.name;
                    if (mr.sharedMaterial.HasProperty("_Color"))
                        extra += ":color=" + Short(mr.sharedMaterial.color);
                    if (mr.sharedMaterial.HasProperty("_EmissionColor"))
                        extra += ":emis=" + Short(mr.sharedMaterial.GetColor("_EmissionColor"));
                }
                extra += " params=[" + AnimParams(anim) + "]";
            }
            if (isCar)
            {
                foreach (Collider c in mb.GetComponentsInChildren<Collider>())
                {
                    if (c == null) continue;
                    if (c.gameObject.name.IndexOf("KillPlane") < 0) continue;
                    extra += " kill=" + V2(c.bounds.center.x, c.bounds.center.z)
                        + " size=" + V3(c.bounds.size);
                    break;
                }
            }
            log("[TRAFFIC-ROOT] name=" + nm + " pos=" + V2(p.x, p.z)
                + " vel=" + V2(vx, vz) + " speed=" + speed.ToString("0.00")
                + " clip=" + clip + " norm=" + norm.ToString("0.00") + extra);
        }
    }

    public static void Tick(MonoBehaviour[] all, string level, Action<string> log)
    {
        if (string.IsNullOrEmpty(level)
            || level.IndexOf("4_1", StringComparison.Ordinal) < 0)
            return;

        float now = Time.unscaledTime;
        if (now < _next) return;
        _next = now + 1f;

        if (_lastLevel != level)
        {
            _lastLevel = level;
            _typesDumped = false;
            _collLogged.Clear();
            _keyLogged.Clear();
            _lightLogged.Clear();
            _allLogged.Clear();
            _rbLogged.Clear();
            _animLogged.Clear();
            _objLoggedCount = 0;
            _lastPos.Clear();
            _lastTime.Clear();
            _rootPos.Clear();
            _rootTime.Clear();
            _lastMoveLog.Clear();
            log("[TRAFFIC] probe start level=" + level
                + " road=x[" + MinX + "," + MaxX + "] z[" + MinZ + "," + MaxZ + "]");
        }

        // 一次性类型清单：先看 4-1 里到底有哪些候选组件。
        if (!_typesDumped)
        {
            _typesDumped = true;
            Dictionary<string, int> counts = new Dictionary<string, int>();
            foreach (MonoBehaviour mb in all)
            {
                if (mb == null) continue;
                string tn = mb.GetType().Name;
                if (!Keyword(tn) && !Keyword(mb.name)) continue;
                if (!counts.ContainsKey(tn)) counts[tn] = 0;
                counts[tn]++;
            }
            StringBuilder sb = new StringBuilder("[TRAFFIC-TYPES]");
            foreach (KeyValuePair<string, int> kv in counts)
                sb.Append(' ').Append(kv.Key).Append('*').Append(kv.Value);
            log(sb.ToString());
        }

        // 关键字对象（红绿灯 / 车 / 路沿触发器）：打位置、组件和一层字段。
        foreach (MonoBehaviour mb in all)
        {
            if (mb == null) continue;
            string tn = mb.GetType().Name;
            if (!Keyword(tn) && !Keyword(mb.name)) continue;
            Vector3 p = mb.transform.position;
            if (!Near(p)) continue;
            int id = mb.GetInstanceID();
            if (!_keyLogged.Add(id)) continue;
            log("[TRAFFIC-KEY] type=" + tn + " name=" + mb.name
                + " path=" + Path(mb.transform) + " pos=" + V2(p.x, p.z)
                + " types=" + Types(mb.gameObject));
            DumpFields(mb, tn + "[" + mb.name + "]", log);
        }

        // 车/灯根对象每秒一条干净时间线（含动画 clip、信号灯颜色/参数）。
        LogRoots(all, now, log);

        // 马路条带里所有 MonoBehaviour 的移动轨迹（车一定会在这里露出来）。
        foreach (MonoBehaviour mb in all)
        {
            if (mb == null) continue;
            Vector3 p = mb.transform.position;
            if (!InRoad(p)) continue;
            int id = mb.GetInstanceID();
            if (_allLogged.Add(id) && _objLoggedCount < ObjLogCap)
            {
                _objLoggedCount++;
                log("[TRAFFIC-OBJ] type=" + mb.GetType().Name + " name=" + mb.name
                    + " path=" + Path(mb.transform) + " pos=" + V2(p.x, p.z)
                    + " types=" + Types(mb.gameObject));
            }
            Track(id, mb.transform, "type=" + mb.GetType().Name + " name=" + mb.name
                + " path=" + Path(mb.transform), now, log);
        }

        // Rigidbody：车辆如果是物理驱动，速度/质量会直接给出答案。
        try
        {
            Rigidbody[] rbs = UnityEngine.Object.FindObjectsOfType<Rigidbody>();
            foreach (Rigidbody rb in rbs)
            {
                if (rb == null) continue;
                Vector3 p = rb.transform.position;
                if (!InRoad(p)) continue;
                if (_rbLogged.Add(rb.GetInstanceID()))
                {
                    log("[TRAFFIC-RB] name=" + rb.name + " path=" + Path(rb.transform)
                        + " pos=" + V2(p.x, p.z) + " vel=" + V2(rb.velocity.x, rb.velocity.z)
                        + " mass=" + rb.mass.ToString("0.0")
                        + " types=" + Types(rb.gameObject));
                }
                Track(-rb.GetInstanceID(), rb.transform,
                      "rb name=" + rb.name + " path=" + Path(rb.transform), now, log);
            }
        }
        catch (Exception e) { log("[TRAFFIC] rb scan error: " + e.Message); }

        // Collider：车体/红绿灯触发区的几何与层号；每个对象只打一次。
        try
        {
            Collider[] cols = UnityEngine.Object.FindObjectsOfType<Collider>();
            foreach (Collider c in cols)
            {
                if (c == null) continue;
                Vector3 p = c.bounds.center;
                if (!InRoad(p)) continue;
                if (!_collLogged.Add(c.GetInstanceID())) continue;
                log("[TRAFFIC-COLL] name=" + c.name + " path=" + Path(c.transform)
                    + " pos=" + V2(p.x, p.z) + " size=" + V3(c.bounds.size)
                    + " layer=" + c.gameObject.layer
                    + " types=" + Types(c.gameObject));
            }
        }
        catch (Exception e) { log("[TRAFFIC] collider scan error: " + e.Message); }

        // Animator：有的车/灯靠动画状态机驱动。
        try
        {
            Animator[] anims = UnityEngine.Object.FindObjectsOfType<Animator>();
            foreach (Animator a in anims)
            {
                if (a == null) continue;
                Vector3 p = a.transform.position;
                if (!InRoad(p)) continue;
                if (!_animLogged.Add(a.GetInstanceID())) continue;
                AnimatorStateInfo st = a.GetCurrentAnimatorStateInfo(0);
                log("[TRAFFIC-ANIM] name=" + a.name + " path=" + Path(a.transform)
                    + " pos=" + V2(p.x, p.z) + " speed=" + a.speed.ToString("0.0")
                    + " state=" + st.fullPathHash + " norm=" + st.normalizedTime.ToString("0.00"));
            }
        }
        catch (Exception e) { log("[TRAFFIC] animator scan error: " + e.Message); }

        // Unity Light：如果红绿灯是真实光源，颜色/强度会直接反映灯色。
        try
        {
            Light[] lights = UnityEngine.Object.FindObjectsOfType<Light>();
            foreach (Light l in lights)
            {
                if (l == null) continue;
                Vector3 p = l.transform.position;
                if (!Near(p)) continue;
                if (!_lightLogged.Add(l.GetInstanceID())) continue;
                log("[TRAFFIC-LIGHT] name=" + l.name + " path=" + Path(l.transform)
                    + " pos=" + V2(p.x, p.z) + " type=" + l.type
                    + " color=" + Short(l.color) + " intensity=" + l.intensity.ToString("0.00")
                    + " range=" + l.range.ToString("0.00") + " enabled=" + l.enabled);
            }
        }
        catch (Exception e) { log("[TRAFFIC] light scan error: " + e.Message); }
    }
}

// 临时诊断：1 Hz 打一次中央区域的移动对象状态。
// 目标：确认 3-3 的桌子是 Animator、旧版 Animation、PilotMovement，
// 还是单纯被父节点/脚本搬运的 DynamicGridLocation。只写日志，不改行为。
using System;
using System.Reflection;
using System.Text;
using UnityEngine;

static class Oc2PilotProbe
{
    static float _next;

    static object Field(object obj, string name)
    {
        if (obj == null) return null;
        Type t = obj.GetType();
        while (t != null)
        {
            FieldInfo f = t.GetField(name, BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic);
            if (f != null) return f.GetValue(obj);
            t = t.BaseType;
        }
        return null;
    }

    static object Call(object obj, string name)
    {
        if (obj == null) return null;
        MethodInfo m = obj.GetType().GetMethod(name, Type.EmptyTypes);
        return m != null ? m.Invoke(obj, null) : null;
    }

    static string Path(Transform t)
    {
        if (t == null) return "";
        string s = t.name;
        while (t.parent != null)
        {
            t = t.parent;
            s = t.name + "/" + s;
        }
        return s;
    }

    static string Arr(object value)
    {
        if (value == null) return "";
        System.Array a = value as System.Array;
        if (a == null) return value.ToString();
        StringBuilder sb = new StringBuilder();
        foreach (object item in a)
        {
            if (sb.Length > 0) sb.Append('|');
            sb.Append(item);
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

    static bool Central(Vector3 p)
    {
        return p.x >= 8f && p.x <= 23f && p.z >= -11f && p.z <= -0.5f;
    }

    public static void Tick(MonoBehaviour[] all, Action<string> log)
    {
        if (Time.unscaledTime < _next) return;
        _next = Time.unscaledTime + 1f;

        foreach (MonoBehaviour mb in all)
        {
            if (mb == null) continue;
            string tn = mb.GetType().Name;
            if (tn != "PilotMovement" && tn != "PilotRotation"
                && tn != "DynamicGridLocation" && tn != "RigidbodyMotion")
                continue;
            Vector3 p = mb.transform.position;
            if (!Central(p)) continue;
            string extra = "";
            if (tn == "PilotRotation")
            {
                Transform tr = Field(mb, "m_transformToRotate") as Transform;
                if (tr != null) extra += " angle=" + tr.eulerAngles.y.ToString("0.0");
                extra += " min=" + Field(mb, "m_minLimitDegrees")
                    + " max=" + Field(mb, "m_maxLimitDegrees");
            }
            if (tn == "DynamicGridLocation")
            {
                extra += " parent=" + Path(mb.transform.parent)
                    + " parentTypes=" + Types(mb.transform.parent != null
                        ? mb.transform.parent.gameObject : null);
                Animator pa = mb.GetComponentInParent<Animator>();
                if (pa != null)
                {
                    AnimatorStateInfo pst = pa.GetCurrentAnimatorStateInfo(0);
                    extra += " parentAnim=" + pa.gameObject.name
                        + " norm=" + pst.normalizedTime.ToString("0.00")
                        + " len=" + pst.length.ToString("0.0")
                        + " loop=" + pst.loop;
                }
            }
            extra += " moved=" + Call(mb, "HasMoved")
                + " vel=" + Call(mb, "EstimateAverageVelocity");
            log("[PILOT] " + tn + " name=" + mb.name
                + " path=" + Path(mb.transform)
                + " pos=(" + p.x.ToString("0.0") + "," + p.z.ToString("0.0") + ")"
                + extra);
        }

        // 定时触发器：ChickenPepperoniCrates 这类对象的周期来源
        foreach (MonoBehaviour mb in all)
        {
            if (mb == null || mb.GetType().Name != "TriggerTimer") continue;
            Vector3 p = mb.transform.position;
            Component srv = mb.GetComponent("ServerTriggerTimer");
            log("[PILOT-TIMER] name=" + mb.name
                + " path=" + Path(mb.transform)
                + " pos=(" + p.x.ToString("0.0") + "," + p.z.ToString("0.0") + ")"
                + " time=" + Field(mb, "m_time")
                + " remaining=" + Field(srv, "m_timer")
                + " start=" + Field(mb, "m_startTrigger")
                + " complete=" + Field(mb, "m_completeTrigger")
                + " startTiming=" + Field(mb, "m_startTiming")
                + " atStart=" + Field(mb, "m_triggerAtStart"));

            Component tq = mb.GetComponent("TriggerQueue");
            if (tq != null)
            {
                object queue = Field(tq, "m_queue");
                log("[PILOT-QUEUE] name=" + mb.name
                    + " waitFinished=" + Field(tq, "m_waitForFinished")
                    + " loop=" + Field(tq, "m_loopWhenFinished")
                    + " loopDelay=" + Field(tq, "m_loopDelay")
                    + " startOnAwake=" + Field(tq, "m_startOnAwake")
                    + " start=" + Field(tq, "m_startTrigger")
                    + " end=" + Field(tq, "m_endTrigger")
                    + " triggers=" + Arr(Field(queue, "m_triggers"))
                    + " delays=" + Arr(Field(queue, "m_delays")));
            }
            Component sq = mb.GetComponent("ServerTriggerQueue");
            if (sq != null)
            {
                log("[PILOT-QUEUE] server name=" + mb.name
                    + " delay=" + Field(sq, "m_delayTimer")
                    + " index=" + Field(sq, "m_nextIndex"));
            }
        }

        // layer 26 = TableBlock：桌子本体的碰撞体（按 bounds 中心，父节点在(0,0)也能抓到）
        foreach (Collider c in UnityEngine.Object.FindObjectsOfType<Collider>())
        {
            if (c == null || c.gameObject.layer != 26) continue;
            Vector3 p = c.bounds.center;
            if (!Central(p)) continue;
            log("[PILOT-COLL] name=" + c.gameObject.name
                + " path=" + Path(c.transform)
                + " pos=(" + p.x.ToString("0.0") + "," + p.z.ToString("0.0") + ")"
                + " types=" + Types(c.gameObject)
                + " parent=" + Path(c.transform.parent)
                + " parentTypes=" + Types(c.transform.parent != null
                    ? c.transform.parent.gameObject : null));
        }

        // 旧版 Animation（不是 Animator）
        foreach (Animation an in UnityEngine.Object.FindObjectsOfType<Animation>())
        {
            if (an == null) continue;
            Vector3 p = an.transform.position;
            if (!Central(p)) continue;
            string clips = "";
            foreach (AnimationState st in an)
            {
                clips += "[" + st.name + " t=" + st.time.ToString("0.0")
                    + " len=" + st.length.ToString("0.0")
                    + " speed=" + st.speed.ToString("0.0")
                    + " wrap=" + st.wrapMode + "]";
            }
            log("[PILOT-LEGACY-ANIM] name=" + an.gameObject.name
                + " path=" + Path(an.transform)
                + " pos=(" + p.x.ToString("0.0") + "," + p.z.ToString("0.0") + ")"
                + " playing=" + an.isPlaying + " clips=" + clips);
        }

        // Animator：保留，但按对象/父链过滤，避免父节点在 (0,0) 漏掉
        foreach (Animator a in UnityEngine.Object.FindObjectsOfType<Animator>())
        {
            if (a == null) continue;
            Vector3 p = a.transform.position;
            bool near = Central(p);
            if (!near)
            {
                foreach (Transform child in a.transform)
                {
                    if (Central(child.position)) { near = true; break; }
                }
            }
            if (!near) continue;
            AnimatorStateInfo st = a.GetCurrentAnimatorStateInfo(0);
            log("[PILOT-ANIM] name=" + a.gameObject.name
                + " path=" + Path(a.transform)
                + " pos=(" + p.x.ToString("0.0") + "," + p.z.ToString("0.0") + ")"
                + " state=" + st.fullPathHash
                + " norm=" + st.normalizedTime.ToString("0.00")
                + " len=" + st.length.ToString("0.0")
                + " speed=" + a.speed.ToString("0.0")
                + " loop=" + st.loop);
        }
    }
}

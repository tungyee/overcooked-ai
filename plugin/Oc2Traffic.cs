// 4-1「红绿灯寿司街」的过街状态：信号灯 + 两辆车 + 马路条带。
//
// Python 侧用它做两件事：
//   1. 路沿等待：两个灯都 False 且持续一小段时间后才起步；
//   2. 车体避让：车下 KillPlane 的 AABB 覆盖到路径格时继续等。
//
// 只在关卡名含 "4_1" 时输出，其它关卡 traffic=null（Python 不门控）。
using System;
using System.Collections.Generic;
using System.Globalization;
using System.Text;
using UnityEngine;

namespace Oc2AI
{
    public partial class Oc2AIPlugin
    {
        void AppendTraffic(StringBuilder sb)
        {
            if (string.IsNullOrEmpty(_level)
                || _level.IndexOf("4_1", StringComparison.Ordinal) < 0
                || !_gridReady)
            {
                sb.Append("null");
                return;
            }

            // 马路条带：主网格 gx -3..6 的格心范围；z 覆盖整关（4-1 马路纵贯）。
            float minx = _originX + (-3f) * _cellX;
            float maxx = _originX + 6f * _cellX;
            sb.Append('{')
              .Append("\"road\":{\"minx\":").Append(F(minx))
              .Append(",\"maxx\":").Append(F(maxx))
              .Append(",\"minz\":-4,\"maxz\":15}");

            sb.Append(",\"lights\":[");
            HashSet<int> seen = new HashSet<int>();
            int i = 0;
            foreach (MonoBehaviour mb in _all)
            {
                if (mb == null) continue;
                string nm = mb.name;
                if (!nm.StartsWith("traffic_light_animated")) continue;
                if (!seen.Add(mb.gameObject.GetInstanceID())) continue;
                Animator a = mb.GetComponent<Animator>();
                if (a == null) continue;
                bool on = false, onB = false;
                try { on = a.GetBool("LightsOn"); onB = a.GetBool("LightsOn_CarB"); }
                catch (Exception) { }
                // 灯 prefab 的子物体 red_glow/yellow_glow/green_glow 由 Animator
                // 直接切 m_IsActive；这是玩家看到的灯色，不是车灯 bool。只读诊断：
                // 原样暴露三个灯的 active 状态，Python 侧暂不消费。
                bool red = LampActive(mb.transform, "red_glow");
                bool yellow = LampActive(mb.transform, "yellow_glow");
                bool green = LampActive(mb.transform, "green_glow");
                bool hasLamps = LampExists(mb.transform, "red_glow")
                    || LampExists(mb.transform, "yellow_glow")
                    || LampExists(mb.transform, "green_glow");
                Vector3 p = mb.transform.position;
                if (i++ > 0) sb.Append(',');
                sb.Append("{\"n\":").Append(Jstr(nm))
                  .Append(",\"x\":").Append(F(p.x))
                  .Append(",\"z\":").Append(F(p.z))
                  .Append(",\"on\":").Append(on ? "true" : "false")
                  .Append(",\"on_car_b\":").Append(onB ? "true" : "false")
                  .Append(",\"red\":").Append(red ? "true" : "false")
                  .Append(",\"yellow\":").Append(yellow ? "true" : "false")
                  .Append(",\"green\":").Append(green ? "true" : "false")
                  .Append(",\"has_lamps\":").Append(hasLamps ? "true" : "false")
                  .Append('}');
            }
            sb.Append("],\"cars\":[");

            HashSet<int> cars = new HashSet<int>();
            int j = 0;
            foreach (MonoBehaviour mb in _all)
            {
                if (mb == null) continue;
                string nm = mb.name;
                if (nm != "Blue_Car" && nm != "Green_Car") continue;
                if (!cars.Add(mb.gameObject.GetInstanceID())) continue;
                Bounds kill = new Bounds();
                bool found = false;
                foreach (Collider c in mb.GetComponentsInChildren<Collider>())
                {
                    if (c == null) continue;
                    if (c.gameObject.name.IndexOf("KillPlane") < 0) continue;
                    kill = c.bounds;
                    found = true;
                    break;
                }
                if (!found) continue;
                Vector3 p = mb.transform.position;
                if (j++ > 0) sb.Append(',');
                sb.Append("{\"n\":").Append(Jstr(nm))
                  .Append(",\"x\":").Append(F(p.x))
                  .Append(",\"z\":").Append(F(p.z))
                  .Append(",\"minx\":").Append(F(kill.min.x))
                  .Append(",\"maxx\":").Append(F(kill.max.x))
                  .Append(",\"minz\":").Append(F(kill.min.z))
                  .Append(",\"maxz\":").Append(F(kill.max.z))
                  .Append('}');
            }
            sb.Append("]}");
        }

        // 灯 prefab 的三个灯泡是子 GameObject（red_glow / yellow_glow / green_glow），
        // Animator 用 m_IsActive 切换亮灭；灭掉的灯是 inactive，必须带 true 才能找到。
        static bool LampExists(Transform root, string name)
        {
            foreach (Transform t in root.GetComponentsInChildren<Transform>(true))
            {
                if (t != null && t.name == name) return true;
            }
            return false;
        }

        static bool LampActive(Transform root, string name)
        {
            foreach (Transform t in root.GetComponentsInChildren<Transform>(true))
            {
                if (t != null && t.name == name) return t.gameObject.activeSelf;
            }
            return false;
        }

        static string F(float v)
        {
            return v.ToString("0.##", CultureInfo.InvariantCulture);
        }
    }
}

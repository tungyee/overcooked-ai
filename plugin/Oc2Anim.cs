using System;
using System.Globalization;
using System.Reflection;
using System.Text;
using UnityEngine;

namespace Oc2AI
{
    // 会动附属网格（气球篮子）的动画进度：Python 侧 ai/space/seam_clock.py 用它
    // 交叉验证从门洞开关沿学出来的周期。篮子挂在 Animator 下、由动画驱动（不是
    // 物理/随机），一圈固定时长（Balloon_2_3 实测 ≈27s）。
    //
    // Animator 在 AnimationModule 里，build.sh 没引用它，按反射读：
    // GetCurrentAnimatorStateInfo(0) → normalizedTime / length / speedMultiplier / loop。
    public partial class Oc2AIPlugin
    {
        static Component AnimatorAbove(Transform t)
        {
            for (; t != null; t = t.parent)
                foreach (Component c in t.GetComponents<Component>())
                    if (c != null && c.GetType().Name == "Animator") return c;
            return null;
        }

        // {"state":hash,"t":normalizedTime,"len":秒,"speed":倍率,"loop":bool}；读不到返回 "null"。
        static string AnimJson(Transform t)
        {
            try
            {
                Component anim = AnimatorAbove(t);
                if (anim == null) return "null";
                Type at = anim.GetType();
                MethodInfo gi = at.GetMethod("GetCurrentAnimatorStateInfo", new Type[] { typeof(int) });
                if (gi == null) return "null";
                object info = gi.Invoke(anim, new object[] { 0 });
                Type it = info.GetType();
                float nt = Convert.ToSingle(it.GetProperty("normalizedTime").GetValue(info, null));
                float len = Convert.ToSingle(it.GetProperty("length").GetValue(info, null));
                float sm = Convert.ToSingle(it.GetProperty("speedMultiplier").GetValue(info, null));
                bool loop = Convert.ToBoolean(it.GetProperty("loop").GetValue(info, null));
                int hash = Convert.ToInt32(it.GetProperty("fullPathHash").GetValue(info, null));
                PropertyInfo sp = at.GetProperty("speed");
                float speed = sp != null ? Convert.ToSingle(sp.GetValue(anim, null)) : 1f;
                var ci = CultureInfo.InvariantCulture;
                return new StringBuilder("{\"state\":").Append(hash)
                    .Append(",\"t\":").Append(nt.ToString("0.####", ci))
                    .Append(",\"len\":").Append(len.ToString("0.###", ci))
                    .Append(",\"speed\":").Append((speed * sm).ToString("0.###", ci))
                    .Append(",\"loop\":").Append(loop ? "true" : "false")
                    .Append('}').ToString();
            }
            catch { return "null"; }
        }
    }
}

// Oc2Commands.cs —— tap / hold / throw 命令。
//
// 与 MoveCommand 同接口（IExecCommand：Done/Ok/Why/BlockedCell + Tick）。
// tap 是交互闭环的关键：句柄确认 → 按键 → 验证手持变化 → 重试一次。
//
// Ruling（Task 6 v1）:
//   - HoldCommand 按住到 timeout，不做内建 pred 早退（切/洗/喷在动作完成后继续
//     按住是幂等/无害的，Python 用 Confirm 做语义校验）。pred 早退留实机标定后再加。
//   - WaitUntil 保持纯 Python（语义等待，走 5Hz 快照），不发 wait 命令。
using System;
using System.Collections.Generic;
using UnityEngine;

namespace Oc2AI
{
    internal static class PadButtons
    {
        public static Vector2 DirVec(string name)
        {
            if (name == "left") return new Vector2(-1f, 0f);
            if (name == "right") return new Vector2(1f, 0f);
            if (name == "up") return new Vector2(0f, 1f);
            if (name == "down") return new Vector2(0f, -1f);
            return Vector2.zero;
        }

        public static bool HandleMatches(ChefFrame f, Dictionary<string, object> target, string kind)
        {
            double? tgx = Req.Num(target, "gx");
            double? tgz = Req.Num(target, "gz");
            string tname = Req.Str(target, "name");
            if (kind == "pickup")
            {
                // 名字优先（实例感知）：句柄名 = 对象身份（FindNearbyObjects 权威
                // 结果），不受坐标系影响——站点 gz 坏值（WashingPart 报 gz=2 而非
                // -4）与篮子本地坐标都成立。坐标只作兜底，无坐标才退 NamesMatch。
                if (NameMatchesStrict(f.PickupName, tname))
                    return true;
                if (tgx.HasValue && tgz.HasValue && f.PickupGx > -9000f)
                    return Mathf.Abs(f.PickupGx - (float)tgx.Value) < 0.1f
                        && Mathf.Abs(f.PickupGz - (float)tgz.Value) < 0.1f;
                return NamesMatch(f.PickupName, tname);
            }
            if (kind == "placement")
            {
                if (NameMatchesStrict(f.PlacementName, tname))
                    return true;
                if (tgx.HasValue && tgz.HasValue && f.PlacementGx > -9000f)
                    return Mathf.Abs(f.PlacementGx - (float)tgx.Value) < 0.1f
                        && Mathf.Abs(f.PlacementGz - (float)tgz.Value) < 0.1f;
                return NamesMatch(f.PlacementName, tname);
            }
            // kind=null：通用朝向句柄（m_gridLocation）——盛饭/倒料等端盘面对
            // 锅的交互，pickup/placement 会被脚边散料抢走或为 null，只有
            // m_gridLocation 稳定指向面朝的目标。
            if (NameMatchesStrict(f.GridName, tname))
                return true;
            if (tgx.HasValue && tgz.HasValue && f.GridGx > -9000f)
                return Mathf.Abs(f.GridGx - (float)tgx.Value) < 0.1f
                    && Mathf.Abs(f.GridGz - (float)tgz.Value) < 0.1f;
            return NamesMatch(f.GridName, tname);
        }

        public static bool UseMatches(ChefFrame f, Dictionary<string, object> target)
        {
            return NamesMatch(f.UseName, Req.Str(target, "name"));
        }

        private static bool NamesMatch(string a, string b)
        {
            if (a == null || b == null) return false;
            return a.IndexOf(b, StringComparison.Ordinal) >= 0
                || b.IndexOf(a, StringComparison.Ordinal) >= 0;
        }

        // 严格名匹配：Python 侧 station_name_matches 的 C# 等价。
        // 实例后缀必须一致（cooker_01 vs cooker_01 (3) 是不同站点），允许
        // 父节点前缀差异（段边界 "/" 或 "." 处结束）。
        private static bool NameMatchesStrict(string a, string b)
        {
            if (a == null || b == null) return false;
            a = a.Trim(); b = b.Trim();
            if (a.Length == 0 || b.Length == 0) return false;
            if (a == b) return true;
            if (InstanceId(a) != InstanceId(b)) return false;
            string ab = StripInstance(a), bb = StripInstance(b);
            if (ab == bb) return true;
            string s = ab.Length <= bb.Length ? ab : bb;
            string l = ab.Length <= bb.Length ? bb : ab;
            if (s.Length == 0) return false;
            if (l.EndsWith(s, StringComparison.Ordinal))
            {
                string boundary = l.Substring(0, l.Length - s.Length);
                return boundary.EndsWith("/") || boundary.EndsWith(".");
            }
            return false;
        }

        private static string InstanceId(string name)
        {
            // 末尾 " (n)" 实例后缀；没有或非纯数字返回 ""。
            if (string.IsNullOrEmpty(name)) return "";
            name = name.TrimEnd();
            if (!name.EndsWith(")")) return "";
            int open = name.LastIndexOf('(');
            if (open < 0) return "";
            string num = name.Substring(open + 1, name.Length - open - 2);
            if (num.Length == 0) return "";
            foreach (char c in num)
                if (!char.IsDigit(c)) return "";
            return "(" + num + ")";
        }

        private static string StripInstance(string name)
        {
            string id = InstanceId(name);
            if (id.Length == 0) return name;
            // 去掉实例后缀及其前面的空白。
            return name.Substring(0, name.Length - id.Length).TrimEnd();
        }
    }

    /// <summary>tap：校验句柄 → 按一次 → 验证手持变化 → 失败重瞄重按一次。</summary>
    public sealed class TapCommand : IExecCommand
    {
        private const float REAIM_TIME = 0.6f;
        private const float TAP_HOLD = 0.1f;
        private const float VERIFY_TIME = 0.5f;

        private enum Phase { Reaim, Press, Verify }
        private Phase _phase = Phase.Reaim;
        private readonly string _kind;
        private readonly Dictionary<string, object> _target;
        private readonly Vector2 _targetWorld;
        private readonly bool _hasTargetWorld;
        private readonly Vector2 _holdDir;
        private readonly float _timeout;
        private bool _started;
        private float _startTime;
        private float _phaseStart;
        private string _heldBefore;
        private int _retries;

        public bool Done { get; private set; }
        public bool Ok { get; private set; }
        public string Why { get; private set; }
        public double[] BlockedCell { get { return null; } }

        public TapCommand(Dictionary<string, object> cmd)
        {
            _kind = Req.Str(cmd, "kind") ?? "pickup";
            _target = Req.Obj(cmd, "target");
            var tw = Req.List(cmd, "target_world");
            if (tw != null && tw.Count >= 2)
            {
                _targetWorld = new Vector2((float)(double)tw[0], (float)(double)tw[1]);
                _hasTargetWorld = true;
            }
            _holdDir = PadButtons.DirVec(Req.Str(cmd, "hold_dir"));
            _timeout = (float)(Req.Num(cmd, "timeout_ms") ?? 5000.0) / 1000f;
        }

        public void Tick(ChefFrame f, float now)
        {
            if (Done) return;
            if (!_started) { _started = true; _startTime = now; _phaseStart = now; }
            if (now - _startTime > _timeout) { Finish(false, "timeout"); return; }
            if (!f.Valid) { Finish(false, "no chef"); return; }
            f.RefreshInteraction();

            switch (_phase)
            {
                case Phase.Reaim:
                    if (_target == null || PadButtons.HandleMatches(f, _target, _kind))
                    {
                        PadEngagement.Pad.Stop();
                        _phase = Phase.Press;
                        _phaseStart = now;
                        _heldBefore = f.HeldName;
                    }
                    else if (_hasTargetWorld)
                    {
                        if (now - _phaseStart > REAIM_TIME)
                        {
                            if (++_retries >= 2) { LogHandleMismatch(f); Finish(false, "handle mismatch"); return; }
                            _phaseStart = now;   // 再瞄一轮
                        }
                        Vector2 pos = new Vector2(f.PosX, f.PosZ);
                        Vector2 dir = (_targetWorld - pos).normalized;
                        PadEngagement.Pad.Move(f.Chef, dir.x * 0.6f, dir.y * 0.6f);
                    }
                    else
                    {
                        LogHandleMismatch(f);
                        Finish(false, "handle mismatch");
                    }
                    break;

                case Phase.Press:
                    if (_holdDir != Vector2.zero) PadEngagement.Pad.Stick(_holdDir.x, _holdDir.y);
                    PadEngagement.Pad.Press(PlayerInputLookup.LogicalButtonID.PickupAndDrop, true);
                    if (now - _phaseStart >= TAP_HOLD)
                    {
                        PadEngagement.Pad.Press(PlayerInputLookup.LogicalButtonID.PickupAndDrop, false);
                        if (_holdDir != Vector2.zero) PadEngagement.Pad.Stop();
                        _phase = Phase.Verify;
                        _phaseStart = now;
                    }
                    break;

                case Phase.Verify:
                    if (f.HeldName != _heldBefore) { Finish(true, null); return; }
                    if (now - _phaseStart > VERIFY_TIME)
                    {
                        if (++_retries >= 2) { Finish(false, "no effect"); return; }
                        _phase = Phase.Reaim;   // 重瞄重按一次
                        _phaseStart = now;
                    }
                    break;
            }
        }

        private void Finish(bool ok, string why)
        {
            PadEngagement.Pad.Stop();
            PadEngagement.Pad.Press(PlayerInputLookup.LogicalButtonID.PickupAndDrop, false);
            Done = true;
            Ok = ok;
            Why = why;
        }

        private void LogHandleMismatch(ChefFrame f)
        {
            UnityEngine.Debug.Log("[EXEC] tap mismatch kind=" + _kind
                + " pickup[name=" + f.PickupName + " id=" + f.PickupId
                + " gx=" + f.PickupGx + "," + f.PickupGz + "]"
                + " placement[name=" + f.PlacementName + " id=" + f.PlacementId
                + " gx=" + f.PlacementGx + "," + f.PlacementGz + "]"
                + " target[name=" + Req.Str(_target, "name")
                + " gx=" + Req.Num(_target, "gx") + "," + Req.Num(_target, "gz") + "]");
        }
    }

    /// <summary>hold：按住 WorkstationInteract 到 timeout，逐帧重验位置（句柄漂了就松键 FAILURE）。</summary>
    public sealed class HoldCommand : IExecCommand
    {
        private const float REAIM_TIME = 0.6f;

        private readonly Dictionary<string, object> _target;
        private readonly string _kind;
        private readonly Vector2 _targetWorld;
        private readonly bool _hasTargetWorld;
        private readonly float _timeout;
        private bool _started;
        private float _startTime;
        private float _phaseStart;
        private int _reaimRetries;

        public bool Done { get; private set; }
        public bool Ok { get; private set; }
        public string Why { get; private set; }
        public double[] BlockedCell { get { return null; } }

        public HoldCommand(Dictionary<string, object> cmd)
        {
            _target = Req.Obj(cmd, "target");
            _kind = Req.Str(cmd, "kind");
            var tw = Req.List(cmd, "target_world");
            if (tw != null && tw.Count >= 2)
            {
                _targetWorld = new Vector2((float)(double)tw[0], (float)(double)tw[1]);
                _hasTargetWorld = true;
            }
            _timeout = (float)(Req.Num(cmd, "timeout_ms") ?? 5000.0) / 1000f;
        }

        public void Tick(ChefFrame f, float now)
        {
            if (Done) return;
            if (!_started) { _started = true; _startTime = now; _phaseStart = now; }
            if (now - _startTime > _timeout) { Finish(false, "timeout"); return; }
            if (!f.Valid) { Finish(false, "no chef"); return; }
            f.RefreshInteraction();
            // 句柄没命中（没正对目标）→ 有 target_world 就先 re-aim 转向，
            // 转正后下一拍命中再按键；无世界坐标则 drifted 交 Python。
            // spray（灭火器）：use 句柄是手持灭火器本身而非灶，改比朝向句柄
            // （m_gridLocation，HandleMatches(kind=null)）——正对灶才算命中。
            if (_target != null && !MatchesTarget(f))
            {
                if (!_hasTargetWorld)
                {
                    Finish(false, "drifted");
                    return;
                }
                if (now - _phaseStart > REAIM_TIME)
                {
                    if (++_reaimRetries >= 2) { Finish(false, "drifted"); return; }
                    _phaseStart = now;
                }
                Vector2 pos = new Vector2(f.PosX, f.PosZ);
                Vector2 dir = (_targetWorld - pos).normalized;
                PadEngagement.Pad.Move(f.Chef, dir.x * 0.6f, dir.y * 0.6f);
                return;
            }

            PadEngagement.Pad.Stop();
            PadEngagement.Pad.Press(PlayerInputLookup.LogicalButtonID.WorkstationInteract, true);
        }

        private bool MatchesTarget(ChefFrame f)
        {
            // spray 比朝向句柄（m_gridLocation），其余比 use 句柄（m_interactable）。
            if (_kind == "spray") return PadButtons.HandleMatches(f, _target, null);
            return PadButtons.UseMatches(f, _target);
        }

        private void Finish(bool ok, string why)
        {
            PadEngagement.Pad.Stop();
            PadEngagement.Pad.Press(PlayerInputLookup.LogicalButtonID.WorkstationInteract, false);
            Done = true;
            Ok = ok;
            Why = why;
        }
    }

    /// <summary>throw：面朝目标 → 单击 WorkstationInteract（切/洗/喷/投掷同键，靠上下文区分）。</summary>
    public sealed class ThrowCommand : IExecCommand
    {
        private const float FACE_TIME = 0.35f;
        private const float TAP_HOLD = 0.1f;

        private readonly Vector2 _targetWorld;
        private readonly bool _hasTargetWorld;
        private bool _started;
        private float _startTime;
        private bool _tapped;

        public bool Done { get; private set; }
        public bool Ok { get; private set; }
        public string Why { get; private set; }
        public double[] BlockedCell { get { return null; } }

        public ThrowCommand(Dictionary<string, object> cmd)
        {
            var tw = Req.List(cmd, "target_world");
            if (tw != null && tw.Count >= 2)
            {
                _targetWorld = new Vector2((float)(double)tw[0], (float)(double)tw[1]);
                _hasTargetWorld = true;
            }
        }

        public void Tick(ChefFrame f, float now)
        {
            if (Done) return;
            if (!_started) { _started = true; _startTime = now; }
            if (!f.Valid) { Finish(false, "no chef"); return; }

            if (!_tapped)
            {
                if (_hasTargetWorld && now - _startTime < FACE_TIME)
                {
                    // 面朝目标：短暂朝目标方向推（投掷方向 = 朝向）。
                    Vector2 pos = new Vector2(f.PosX, f.PosZ);
                    Vector2 dir = (_targetWorld - pos).normalized;
                    PadEngagement.Pad.Move(f.Chef, dir.x * 0.6f, dir.y * 0.6f);
                    return;
                }
                PadEngagement.Pad.Stop();
                PadEngagement.Pad.Press(PlayerInputLookup.LogicalButtonID.WorkstationInteract, true);
                _tapped = true;
                _startTime = now;   // 复用计时：给单击时长
            }
            if (now - _startTime >= TAP_HOLD) { Finish(true, null); return; }
        }

        private void Finish(bool ok, string why)
        {
            PadEngagement.Pad.Stop();
            PadEngagement.Pad.Press(PlayerInputLookup.LogicalButtonID.WorkstationInteract, false);
            Done = true;
            Ok = ok;
            Why = why;
        }
    }
}

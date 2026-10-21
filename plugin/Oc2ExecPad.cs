// Oc2ExecPad.cs —— 虚拟手柄 + engage（从 spike 提炼并生产化）。
//
// VirtualPad：一个 InControl.InputDevice，游戏把它当普通手柄接入本地玩家。
//   完整控制集（摇杆 + 按键）+ 逻辑按键→物理按键映射，照 cortico 的
//   VirtualPad.cs（同一游戏、已实机验证）。
// PadEngagement：attach 到 InputManager、在主菜单 engage 成第二个本地玩家。
using System;
using System.Collections.Generic;
using System.Reflection;
using InControl;
using UnityEngine;

namespace Oc2AI
{
    public sealed class VirtualPad : InputDevice
    {
        private readonly HashSet<InputControlType> _down = new HashSet<InputControlType>();
        private Vector2 _stick;

        public VirtualPad(string name) : base(name)
        {
            Meta = name;
            AddControl(InputControlType.LeftStickLeft, "Left Stick Left");
            AddControl(InputControlType.LeftStickRight, "Left Stick Right");
            AddControl(InputControlType.LeftStickUp, "Left Stick Up");
            AddControl(InputControlType.LeftStickDown, "Left Stick Down");
            AddControl(InputControlType.RightStickLeft, "Right Stick Left");
            AddControl(InputControlType.RightStickRight, "Right Stick Right");
            AddControl(InputControlType.RightStickUp, "Right Stick Up");
            AddControl(InputControlType.RightStickDown, "Right Stick Down");
            AddControl(InputControlType.LeftTrigger, "Left Trigger");
            AddControl(InputControlType.RightTrigger, "Right Trigger");
            AddControl(InputControlType.DPadUp, "DPad Up");
            AddControl(InputControlType.DPadDown, "DPad Down");
            AddControl(InputControlType.DPadLeft, "DPad Left");
            AddControl(InputControlType.DPadRight, "DPad Right");
            AddControl(InputControlType.Action1, "A");
            AddControl(InputControlType.Action2, "B");
            AddControl(InputControlType.Action3, "X");
            AddControl(InputControlType.Action4, "Y");
            AddControl(InputControlType.LeftBumper, "Left Bumper");
            AddControl(InputControlType.RightBumper, "Right Bumper");
            AddControl(InputControlType.LeftStickButton, "Left Stick Button");
            AddControl(InputControlType.RightStickButton, "Right Stick Button");
            AddControl(InputControlType.Start, "Start");
            AddControl(InputControlType.Back, "Back");
        }

        public override void Update(ulong updateTick, float deltaTime)
        {
            float x = _stick.x, y = _stick.y;
            GetControl(InputControlType.LeftStickLeft).UpdateWithValue(Mathf.Max(0f, -x), updateTick, deltaTime);
            GetControl(InputControlType.LeftStickRight).UpdateWithValue(Mathf.Max(0f, x), updateTick, deltaTime);
            float up = InputManager.InvertYAxis ? -y : y;
            GetControl(InputControlType.LeftStickUp).UpdateWithValue(Mathf.Max(0f, up), updateTick, deltaTime);
            GetControl(InputControlType.LeftStickDown).UpdateWithValue(Mathf.Max(0f, -up), updateTick, deltaTime);
            foreach (var type in Buttons) GetControl(type).UpdateWithState(_down.Contains(type), updateTick, deltaTime);
            Commit(updateTick, deltaTime);
        }

        private static readonly InputControlType[] Buttons =
        {
            InputControlType.Action1, InputControlType.Action2, InputControlType.Action3, InputControlType.Action4,
            InputControlType.LeftBumper, InputControlType.RightBumper, InputControlType.LeftStickButton,
            InputControlType.RightStickButton, InputControlType.Start, InputControlType.Back,
            InputControlType.DPadUp, InputControlType.DPadDown, InputControlType.DPadLeft, InputControlType.DPadRight,
        };

        /// <summary>摇杆：世界坐标系（x 朝 +X，y 朝 +Z），长度 0..1。</summary>
        public void Stick(float x, float y)
        {
            _stick = Vector2.ClampMagnitude(new Vector2(x, y), 1f);
        }

        /// <summary>摇杆：按 chef 的移动轴对齐翻转（某些关卡轴镜像）。</summary>
        public void Move(PlayerControls chef, float worldX, float worldZ)
        {
            float sx = chef.Movement.XAxisAllignment != 0 ? -1f : 1f;
            float sz = chef.Movement.YAxisAllignment != 0 ? -1f : 1f;
            _stick = Vector2.ClampMagnitude(new Vector2(sx * worldX, sz * worldZ), 1f);
        }

        public void Stop() { _stick = Vector2.zero; }

        public void Press(InputControlType type, bool down)
        {
            if (down) _down.Add(type);
            else _down.Remove(type);
        }

        /// <summary>逻辑按键 → 物理按键（照游戏运行时的控件映射表）。</summary>
        public void Press(PlayerInputLookup.LogicalButtonID id, bool down)
        {
            foreach (var type in Physical(id))
            {
                if (down) _down.Add(type);
                else _down.Remove(type);
            }
        }

        public bool AnyInput { get { return _down.Count > 0 || _stick != Vector2.zero; } }

        public void ReleaseAll()
        {
            _down.Clear();
            _stick = Vector2.zero;
        }

        private readonly Dictionary<PlayerInputLookup.LogicalButtonID, InputControlType[]> _mapping =
            new Dictionary<PlayerInputLookup.LogicalButtonID, InputControlType[]>();

        private InputControlType[] Physical(PlayerInputLookup.LogicalButtonID id)
        {
            if (_mapping.TryGetValue(id, out InputControlType[] cached)) return cached;
            var types = new List<InputControlType>();
            foreach (var b in PlayerInputLookup.GetRealButtons(id, PadSide.Both))
            {
                var t = ToControl(b);
                if (t != InputControlType.None) types.Add(t);
            }
            if (types.Count == 0) throw new InvalidOperationException("游戏没有给 " + id + " 绑定手柄按键");
            var result = types.ToArray();
            _mapping[id] = result;
            return result;
        }

        private static InputControlType ToControl(ControlPadInput.Button b)
        {
            switch (b)
            {
                case ControlPadInput.Button.A: return InputControlType.Action1;
                case ControlPadInput.Button.B: return InputControlType.Action2;
                case ControlPadInput.Button.X: return InputControlType.Action3;
                case ControlPadInput.Button.Y: return InputControlType.Action4;
                case ControlPadInput.Button.LB: return InputControlType.LeftBumper;
                case ControlPadInput.Button.RB: return InputControlType.RightBumper;
                case ControlPadInput.Button.LTrigger: return InputControlType.LeftTrigger;
                case ControlPadInput.Button.RTrigger: return InputControlType.RightTrigger;
                case ControlPadInput.Button.DPadLeft: return InputControlType.DPadLeft;
                case ControlPadInput.Button.DPadRight: return InputControlType.DPadRight;
                case ControlPadInput.Button.DPadUp: return InputControlType.DPadUp;
                case ControlPadInput.Button.DPadDown: return InputControlType.DPadDown;
                case ControlPadInput.Button.Back: return InputControlType.Back;
                case ControlPadInput.Button.Start: return InputControlType.Start;
                case ControlPadInput.Button.LeftAnalog: return InputControlType.LeftStickButton;
                case ControlPadInput.Button.RightAnalog: return InputControlType.RightStickButton;
                default: return InputControlType.None;
            }
        }
    }

    /// <summary>虚拟手柄的 attach 与 engage（只在本机本地合作里作为 P2 接入）。</summary>
    public static class PadEngagement
    {
        public static VirtualPad Pad { get; private set; }
        private static bool _attachLogged = false;
        private static bool _engageLogged = false;
        private static float _lastTryTime = -10f;

        private static readonly FieldInfo EngagedPadsField =
            typeof(PCPadInputProvider).GetField("s_engagedPads", BindingFlags.NonPublic | BindingFlags.Static);
        private static readonly MethodInfo GetActionSetMethod =
            typeof(PCPadInputProvider).GetMethod("GetActionSet", BindingFlags.NonPublic | BindingFlags.Static);

        public static bool Attach()
        {
            if (Pad != null) return true;
            if (!InputManager.IsSetup) return false;
            try
            {
                Pad = new VirtualPad("OC2 Bot Pad");
                InputManager.AttachDevice(Pad);
                if (!_attachLogged)
                {
                    _attachLogged = true;
                    UnityEngine.Debug.Log("[EXEC] pad attached; devices=" + InputManager.Devices.Count);
                }
                return true;
            }
            catch (Exception)
            {
                Pad = null;
                return false;
            }
        }

        public static bool IsEngaged()
        {
            return Slot() >= 0;
        }

        /// <summary>当前 occupy 的 pad 编号；未 engage 返回 -1。</summary>
        public static int Slot()
        {
            if (Pad == null || EngagedPadsField == null) return -1;
            var engaged = EngagedPadsField.GetValue(null) as Dictionary<ControlPadInput.PadNum, StandardActionSet>;
            if (engaged == null) return -1;
            foreach (var pair in engaged)
            {
                if (pair.Value != null && pair.Value.Device == Pad) return (int)pair.Key;
            }
            return -1;
        }

        /// <summary>在主菜单把虚拟手柄 engage 成第二个本地玩家；成功返回 true。</summary>
        public static bool TryEngage()
        {
            if (!Attach()) return false;
            if (IsEngaged()) return true;

            // 节流：未 engage 时别每帧 FindObjectOfType/RequireManager（全场景扫描，
            // 60Hz 会把 CPU 打满，实机菜单阶段 101%）。1 秒重试一次足够。
            if (Time.unscaledTime - _lastTryTime < 1.0f) return false;
            _lastTryTime = Time.unscaledTime;

            try
            {
                if (Team17.Online.ServerUserSystem.EngagementsLocked ||
                    UnityEngine.Object.FindObjectOfType<T17FrontendFlow>() == null)
                    return false;   // 只在主菜单允许加入

                var manager = GameUtils.RequireManager<PlayerManager>();
                if (manager == null || manager.GetUser(EngagementSlot.One) == null)
                    return false;   // 等 P1 先按键进入
                if (manager.IsBusy() || !manager.HasFreeEngagementSlot())
                    return false;

                var engaged = EngagedPadsField.GetValue(null) as Dictionary<ControlPadInput.PadNum, StandardActionSet>;
                if (engaged == null || GetActionSetMethod == null) return false;
                for (int i = 0; i < 12; i++)
                {
                    var num = (ControlPadInput.PadNum)i;
                    if (engaged.ContainsKey(num)) continue;
                    var set = GetActionSetMethod.Invoke(null, new object[] { num }) as StandardActionSet;
                    if (set == null || set.Device != Pad) continue;
                    manager.StartPadEngagement(num, null, null);
                    if (!_engageLogged)
                    {
                        _engageLogged = true;
                        UnityEngine.Debug.Log("[EXEC] engaged");
                    }
                    return true;
                }
                return false;
            }
            catch (Exception)
            {
                return false;
            }
        }
    }
}

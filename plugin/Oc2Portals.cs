using System;
using System.Collections.Generic;
using System.Reflection;
using System.Text;
using UnityEngine;

namespace Oc2AI
{
    // 传送门（Teleportal）：静态参数 + 运行时状态 + 传送事件。
    //
    // 反编译（Assembly-CSharp）要点：
    //   - 触发：ServerTeleportal.OnTriggerStay，对象在触发碰撞体内、位置落在
    //     门正面 transform.right ± m_teleportArc（XZ 锥，看位置不看朝向）、门不在
    //     传送/接收/冷却中、出口门有能接这类对象的接收器。
    //   - 刚传过的对象进 m_recentlyTeleported（除非 m_allowImmediateReteleport），
    //     离开触发区或面朝门（±45°）才清掉。
    //   - 人：TeleportalPlayerSender/Receiver，落点 = 出口门接收器 m_attachPoint
    //     向下贴地，朝向 = 该点朝向。手持物是厨师子物体，随人走。
    //   - 物：TeleportalAttachmentSender/Receiver，只收未被持有的 TeleportableItem
    //     （飞行中 / 散落），出口由 AttachmentThrower 沿接收点 forward 重新抛出。
    //   - 传送带：TeleportalConveyenceReceiver，传送带把物品送进门。
    // 这些组件挂没挂在 prefab 上，DLL 看不到，只能运行时读。
    //
    // 协议（新增字段，旧 Python 忽略）：
    //   portals[]：每扇门一条（每帧，含 enabled / teleporting / cooldown 动态态）
    //   teleports[]：最近 TELEPORT_EVENT_KEEP 条传送事件（start/end 各一条，seq 递增）
    //   move 命令可选 portals[]：{enter, exit=enter+1, portal, push:[x,z], land:[x,z], max_wait}
    //     路点 enter = 推门点（入口门触发盒中心），exit = 出口落地点（ai/space/portals.py）。

    /// <summary>move 命令里的一段过门：走到 enter 前一个路点后朝推门点推，等被传走、落地再续走。</summary>
    public sealed class PortalHop
    {
        public readonly int Enter, Exit;
        public readonly string Portal;
        public readonly Vector2 Push, Land;
        public readonly float MaxWait;

        public PortalHop(Dictionary<string, object> d)
        {
            Enter = (int)(Req.Num(d, "enter") ?? -1);
            Exit = (int)(Req.Num(d, "exit") ?? -1);
            Portal = Req.Str(d, "portal");
            Push = Vec(Req.List(d, "push"));
            Land = Vec(Req.List(d, "land"));
            MaxWait = (float)(Req.Num(d, "max_wait") ?? 3.0);
        }

        public bool Valid(int pathCount) { return Enter >= 1 && Exit == Enter + 1 && Exit < pathCount; }

        static Vector2 Vec(List<object> l)
        {
            if (l == null || l.Count < 2) return Vector2.zero;
            return new Vector2(Convert.ToSingle(l[0]), Convert.ToSingle(l[1]));
        }
    }

    public partial class Oc2AIPlugin
    {
        const int TELEPORT_EVENT_KEEP = 24;

        // ServerBaseTeleportable 实例 id → 上一拍是否在传送中 / 是否在飞
        readonly Dictionary<int, bool> _tpWasTeleporting = new Dictionary<int, bool>();
        readonly Dictionary<int, bool> _tpWasFlying = new Dictionary<int, bool>();
        readonly Dictionary<int, Vector3> _tpLastPos = new Dictionary<int, Vector3>();
        readonly LinkedList<string> _tpEvents = new LinkedList<string>();
        int _tpSeq;
        int _tpEpoch = -1;
        int _portalLogEpoch = -1;

        static readonly FieldInfo _fTeleporting = typeof(ServerTeleportal).GetField(
            "m_teleporting", BindingFlags.Instance | BindingFlags.NonPublic);
        static readonly FieldInfo _fCooldown = typeof(ServerTeleportal).GetField(
            "m_cooldown", BindingFlags.Instance | BindingFlags.NonPublic);

        static string F2(float v) { return v.ToString("0.##", Inv); }

        static void AppendXZY(StringBuilder sb, string key, Vector3 p)
        {
            sb.Append(",\"").Append(key).Append("\":[")
              .Append(F2(p.x)).Append(',').Append(F2(p.z)).Append(',').Append(F2(p.y)).Append(']');
        }

        static void AppendDir(StringBuilder sb, string key, Vector3 d)
        {
            sb.Append(",\"").Append(key).Append("\":[")
              .Append(F2(d.x)).Append(',').Append(F2(d.z)).Append(']');
        }

        static string PortalId(GameObject go)
        {
            return go == null ? null : go.name + "#" + go.GetInstanceID();
        }

        void AppendPortals(StringBuilder sb)
        {
            sb.Append('[');
            int i = 0;
            try
            {
                foreach (MonoBehaviour mb in _all)
                {
                    Teleportal tp = mb as Teleportal;
                    if (tp == null) continue;
                    GameObject go = tp.gameObject;
                    ServerTeleportal st = go.GetComponent<ServerTeleportal>();
                    Collider col = go.GetComponent<Collider>();
                    Vector3 c = col != null ? col.bounds.center : go.transform.position;

                    if (i > 0) sb.Append(',');
                    sb.Append("{\"id\":").Append(Jstr(PortalId(go)))
                      .Append(",\"name\":").Append(Jstr(go.name))
                      .Append(",\"exit\":").Append(Jstr(PortalId(tp.m_exitPortal)))
                      .Append(",\"active\":").Append(go.activeInHierarchy && tp.enabled ? "true" : "false")
                      .Append(",\"server_enabled\":").Append(
                          st == null ? "null" : (st.enabled ? "true" : "false"));
                    AppendXZY(sb, "center", c);
                    // 触发扇区方向 = transform.right（IsInTeleportArc），不是 forward
                    AppendDir(sb, "arc_dir", go.transform.right);
                    sb.Append(",\"arc_deg\":").Append(F2(tp.m_teleportArc))
                      .Append(",\"receive_delay\":").Append(F2(tp.m_receiveDelay))
                      .Append(",\"cooldown_time\":").Append(F2(tp.m_cooldownTime))
                      .Append(",\"allow_reteleport\":").Append(tp.m_allowImmediateReteleport ? "true" : "false");
                    if (col != null)
                    {
                        Bounds b = col.bounds;
                        sb.Append(",\"trigger\":{\"type\":").Append(Jstr(col.GetType().Name))
                          .Append(",\"is_trigger\":").Append(col.isTrigger ? "true" : "false")
                          .Append(",\"enabled\":").Append(col.enabled ? "true" : "false")
                          .Append(",\"minx\":").Append(F2(b.min.x)).Append(",\"maxx\":").Append(F2(b.max.x))
                          .Append(",\"minz\":").Append(F2(b.min.z)).Append(",\"maxz\":").Append(F2(b.max.z))
                          .Append(",\"miny\":").Append(F2(b.min.y)).Append(",\"maxy\":").Append(F2(b.max.y))
                          .Append('}');
                    }
                    else sb.Append(",\"trigger\":null");
                    if (tp.m_teleportPoint != null)
                    {
                        AppendXZY(sb, "point", tp.m_teleportPoint.position);
                        AppendDir(sb, "point_fwd", tp.m_teleportPoint.forward);
                    }

                    // 本门能「送出 / 接收」哪类对象。落点看接收器 m_attachPoint：
                    // 人从别的门进来，落在本门 player_receiver 的点上。
                    var ps = go.GetComponent<TeleportalPlayerSender>();
                    var pr = go.GetComponent<TeleportalPlayerReceiver>();
                    var isd = go.GetComponent<TeleportalAttachmentSender>();
                    var ir = go.GetComponent<TeleportalAttachmentReceiver>();
                    var cr = go.GetComponent<TeleportalConveyenceReceiver>();
                    sb.Append(",\"player_sender\":").Append(ps != null ? "true" : "false")
                      .Append(",\"item_sender\":").Append(isd != null ? "true" : "false")
                      .Append(",\"conveyor_in\":").Append(cr != null ? "true" : "false");
                    sb.Append(",\"player_receiver\":");
                    AppendReceiver(sb, pr != null ? pr.m_attachPoint : null, pr != null,
                        pr != null ? (bool?)pr.m_groundPlayer : null);
                    sb.Append(",\"item_receiver\":");
                    AppendReceiver(sb, ir != null ? ir.m_attachPoint : null, ir != null, null);

                    if (st != null)
                    {
                        object tg = _fTeleporting != null ? _fTeleporting.GetValue(st) : null;
                        object cd = _fCooldown != null ? _fCooldown.GetValue(st) : null;
                        sb.Append(",\"teleporting\":").Append(tg is bool b1 ? (b1 ? "true" : "false") : "null")
                          .Append(",\"cooldown\":").Append(cd is bool b2 ? (b2 ? "true" : "false") : "null")
                          .Append(",\"receiving\":").Append(st.IsReceiving() ? "true" : "false");
                    }
                    sb.Append('}');
                    i++;
                }
            }
            catch (Exception e) { Logger.LogWarning("[PORTAL] append error: " + e.Message); }
            sb.Append(']');
        }

        static void AppendReceiver(StringBuilder sb, Transform pt, bool present, bool? ground)
        {
            if (!present) { sb.Append("null"); return; }
            sb.Append('{');
            if (pt != null)
            {
                Vector3 p = pt.position, f = pt.forward;
                sb.Append("\"point\":[").Append(F2(p.x)).Append(',').Append(F2(p.z)).Append(',').Append(F2(p.y))
                  .Append("],\"fwd\":[").Append(F2(f.x)).Append(',').Append(F2(f.z)).Append(']');
            }
            else sb.Append("\"point\":null");
            if (ground.HasValue) sb.Append(",\"ground\":").Append(ground.Value ? "true" : "false");
            sb.Append('}');
        }

        // 厨师此刻是否在传送中（服务端 ServerTeleportablePlayer.IsTeleporting：
        // 进门那一刻置位，出口门接收动画播完才清）。MoveCommand 据此停摇杆、等落地。
        internal static bool ChefTeleporting(Component chef)
        {
            if (chef == null) return false;
            ServerTeleportablePlayer t = chef.GetComponent<ServerTeleportablePlayer>();
            return t != null && t.IsTeleporting();
        }

        // 每拍扫一遍可传送对象，按 IsTeleporting 的上升 / 下降沿记 start / end 事件。
        // 传送动画 ≥ 0.5 s，5 Hz 快照不会漏沿。
        void TrackTeleports()
        {
            if (_tpEpoch != _sceneEpoch)
            {
                _tpEpoch = _sceneEpoch;
                _tpWasTeleporting.Clear(); _tpWasFlying.Clear(); _tpLastPos.Clear();
                _tpEvents.Clear();
            }
            try
            {
                var seen = new HashSet<int>();
                foreach (MonoBehaviour mb in _all)
                {
                    ServerBaseTeleportable t = mb as ServerBaseTeleportable;
                    if (t == null) continue;
                    int id = t.GetInstanceID();
                    seen.Add(id);
                    bool now = t.IsTeleporting();
                    bool was;
                    _tpWasTeleporting.TryGetValue(id, out was);
                    Vector3 pos = t.transform.position;
                    bool player = t is ServerTeleportablePlayer;
                    if (now != was)
                    {
                        Vector3 last;
                        if (!_tpLastPos.TryGetValue(id, out last)) last = pos;
                        bool flew;
                        _tpWasFlying.TryGetValue(id, out flew);
                        // start：用上一拍的位置（这一拍已被门吸住）；end：用当前落点
                        RecordTeleport(now ? "start" : "end", t, player, now ? last : pos, flew);
                    }
                    _tpWasTeleporting[id] = now;
                    if (!now)
                    {
                        _tpLastPos[id] = pos;
                        // 手持物不能单独传送（ServerTeleportableItem.CanTeleport 要求未挂接），
                        // 所以「上一拍在手上、这一拍在门里」= 刚扔出去。5 Hz 快照常常采不到
                        // 飞行那一下（trace-20261101-160703 奶酪 was_flying=false），两者都算。
                        if (!player) _tpWasFlying[id] = IsFlying(t)
                            || t.GetComponentInParent<PlayerControls>() != null;
                    }
                }
                // 销毁的对象清掉，免得字典随关卡时长增长
                if (_tpWasTeleporting.Count > seen.Count)
                {
                    var dead = new List<int>();
                    foreach (int k in _tpWasTeleporting.Keys) if (!seen.Contains(k)) dead.Add(k);
                    foreach (int k in dead) { _tpWasTeleporting.Remove(k); _tpWasFlying.Remove(k); _tpLastPos.Remove(k); }
                }
            }
            catch (Exception e) { Logger.LogWarning("[PORTAL] track error: " + e.Message); }
        }

        void RecordTeleport(string phase, ServerBaseTeleportable t, bool player, Vector3 pos, bool wasFlying)
        {
            GameObject portal = NearestPortal(pos);
            string name = player ? t.gameObject.name : ObjNames.Of(t.gameObject);
            _tpSeq++;
            var sb = new StringBuilder();
            sb.Append("{\"seq\":").Append(_tpSeq)
              .Append(",\"phase\":").Append(Jstr(phase))
              .Append(",\"t\":").Append(Time.timeSinceLevelLoad.ToString("0.###", Inv))
              .Append(",\"kind\":").Append(Jstr(player ? "player" : "item"))
              .Append(",\"obj\":").Append(Jstr(name))
              .Append(",\"obj_id\":").Append(t.gameObject.GetInstanceID());
            AppendXZY(sb, "pos", pos);
            sb.Append(",\"portal\":").Append(Jstr(PortalId(portal)));
            if (!player) sb.Append(",\"thrown\":").Append(wasFlying ? "true" : "false");
            if (player)
            {
                // 手里拿着什么（随人一起传）；与 chefs[].held 同一套取法
                string held = "null";
                try
                {
                    PlayerControls pc = t.GetComponent<PlayerControls>();
                    if (pc != null) held = ItemName(Held(pc));
                }
                catch { }
                sb.Append(",\"held\":").Append(held);
            }
            sb.Append('}');
            string ev = sb.ToString();
            _tpEvents.AddLast(ev);
            while (_tpEvents.Count > TELEPORT_EVENT_KEEP) _tpEvents.RemoveFirst();
            Logger.LogInfo("[TELEPORT] " + ev);
        }

        GameObject NearestPortal(Vector3 pos)
        {
            GameObject best = null;
            float bestD = float.MaxValue;
            foreach (MonoBehaviour mb in _all)
            {
                Teleportal tp = mb as Teleportal;
                if (tp == null) continue;
                Vector3 d = tp.transform.position - pos;
                float dd = d.x * d.x + d.z * d.z;
                if (dd < bestD) { bestD = dd; best = tp.gameObject; }
            }
            return best;
        }

        void AppendTeleports(StringBuilder sb)
        {
            sb.Append('[');
            int i = 0;
            foreach (string ev in _tpEvents)
            {
                if (i++ > 0) sb.Append(',');
                sb.Append(ev);
            }
            sb.Append(']');
        }

        // 每场景一次把门参数写进 BepInEx 日志（不依赖 Discover 开关）
        void LogPortalsOnce(string portalsJson)
        {
            if (_portalLogEpoch == _sceneEpoch) return;
            _portalLogEpoch = _sceneEpoch;
            if (portalsJson == "[]") return;
            Logger.LogInfo("[PORTALS] level=" + _level + " epoch=" + _sceneEpoch + " " + portalsJson);
        }
    }
}

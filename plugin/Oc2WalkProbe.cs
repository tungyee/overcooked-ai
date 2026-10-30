using System;
using System.Collections.Generic;
using System.Globalization;
using System.Text;
using UnityEngine;

namespace Oc2AI
{
    // 可走性物理诊断（只观测、不参与决策）：在会动的附属网格（气球篮子）
    // 关卡里，对每个厨师周围 ±WALK_PROBE_RADIUS 格做逐子点物理测量，
    // 用厨师自己的碰撞体尺寸 OverlapCapsule，对照 Python 侧「格中心 +
    // terrain AABB 外扩 CHEF_CLEARANCE」的判定，定位接缝处可走误判
    // （trace-20261029-234145：缝宽 1.7m，两格中心离墙都 <0.35m，P1 能过）。
    //
    // 做法参考 cortico-world-overcooked2 的 Nav.Walkable/BodyBlocker：
    // 向下 Raycast 找地板（非 trigger、法线朝上；KillPlane 不算地板），
    // 再在地板高度放厨师胶囊做 OverlapCapsule。厨师、物品、非 kinematic
    // 刚体、传送带、斜坡层和层碰撞矩阵里厨师穿得过的层不算阻挡。
    //
    // 每格 3×3 子点（偏移 -cell/3、0、+cell/3），掩码字符：
    //   '.' 站得下   '#' 有阻挡   ' ' 没地板   '^'/'v' 地板比脚下高/低超过台阶
    public partial class Oc2AIPlugin
    {
        const int WALK_PROBE_RADIUS = 2;
        const int WALK_PROBE_SUB = 3;
        const float WALK_PROBE_STEP = 0.35f;     // 地板高差超过它按台阶/台面算
        const float WALK_PROBE_LIFT = 0.1f;      // 胶囊抬离地板，脚下地板不算重叠
        const int LAYER_KILLPLANE = 30;
        const int WALK_PROBE_MAX_BLOCKERS = 40;

        BepInEx.Configuration.ConfigEntry<bool> _walkProbeEnabled;
        readonly Collider[] _probeOverlaps = new Collider[32];
        readonly RaycastHit[] _probeHits = new RaycastHit[16];
        readonly Dictionary<Collider, bool> _probeIgnore = new Dictionary<Collider, bool>();
        static readonly CultureInfo Inv = CultureInfo.InvariantCulture;

        void BindWalkProbe()
        {
            _walkProbeEnabled = Config.Bind<bool>("Diagnostics", "WalkProbe", true,
                "Physics walkability probe near chefs; only runs on levels with a moving grid.");
        }

        // 有挂在 Animator 下的非主网格才算「会动的厨房」，否则不测。
        bool HasMovingGrid()
        {
            foreach (MonoBehaviour mb in _all)
            {
                if (mb == null || mb.GetType().Name != "QuadGridManager") continue;
                if (_mainGrid != null && (UnityEngine.Object)mb == _mainGrid) continue;
                if (UnderAnimator(mb.transform)) return true;
            }
            return false;
        }

        void AppendWalkProbe(StringBuilder sb)
        {
            if (_walkProbeEnabled == null || !_walkProbeEnabled.Value || !_gridReady || !HasMovingGrid())
            {
                sb.Append("null");
                return;
            }
            var watch = System.Diagnostics.Stopwatch.StartNew();
            _probeIgnore.Clear();
            var blockers = new List<Collider>();
            var chefs = new StringBuilder();
            int n = 0;
            foreach (MonoBehaviour mb in _all)
            {
                if (mb == null || mb.GetType().Name != "PlayerControls") continue;
                if (n++ > 0) chefs.Append(',');
                AppendChefProbe(chefs, mb, blockers);
            }
            sb.Append("{\"ms\":").Append(watch.Elapsed.TotalMilliseconds.ToString("0.00", Inv))
              .Append(",\"ox\":").Append(_originX.ToString("0.####", Inv))
              .Append(",\"oz\":").Append(_originZ.ToString("0.####", Inv))
              .Append(",\"cell\":").Append(_cellX.ToString("0.####", Inv))
              .Append(",\"sub\":").Append(WALK_PROBE_SUB)
              .Append(",\"chefs\":[").Append(chefs).Append("],\"blockers\":[");
            for (int i = 0; i < blockers.Count; i++)
            {
                if (i > 0) sb.Append(',');
                AppendBlocker(sb, blockers[i]);
            }
            sb.Append("]}");
        }
        static string PNum(float v) { return v.ToString("0.##", Inv); }

        // 厨师身体：优先自身/子物体上的非 trigger CapsuleCollider，没有就取第一个非 trigger collider。
        // MoveCommand 的门洞跟随（Oc2Gate.cs）也用它。
        internal static Collider ChefBody(Component chef)
        {
            Collider fallback = null;
            foreach (Collider c in chef.GetComponentsInChildren<Collider>())
            {
                if (c == null || c.isTrigger || !c.enabled) continue;
                if (c is CapsuleCollider) return c;
                if (fallback == null) fallback = c;
            }
            return fallback;
        }

        // 不算阻挡的碰撞体：厨师、物品、传送带、非 kinematic 刚体（会被推开）。
        internal static bool WalkIgnored(Collider c)
        {
            if (c.attachedRigidbody != null && !c.attachedRigidbody.isKinematic) return true;
            for (Transform t = c.transform; t != null; t = t.parent)
                foreach (Component comp in t.GetComponents<Component>())
                {
                    if (comp == null) continue;
                    string tn = comp.GetType().Name;
                    if (tn == "PlayerControls" || tn == "ServerPhysicalAttachment"
                        || tn == "ClientPhysicalAttachment" || tn == "Travelator") return true;
                }
            return false;
        }

        bool ProbeIgnored(Collider c)
        {
            bool ign;
            if (_probeIgnore.TryGetValue(c, out ign)) return ign;
            ign = WalkIgnored(c);
            _probeIgnore[c] = ign;
            return ign;
        }

        // 向下找地板：最近的非 trigger、非忽略命中；法线朝上（或斜坡层）才算地板，
        // KillPlane 永远不算。返回地板高度，没有地板返回 null。
        float? ProbeFloor(float x, float topY, float z)
        {
            int n = Physics.RaycastNonAlloc(new Vector3(x, topY, z), Vector3.down, _probeHits, 8f, ~0, QueryTriggerInteraction.Ignore);
            int best = -1;
            for (int i = 0; i < n; i++)
            {
                Collider c = _probeHits[i].collider;
                if (c == null || ProbeIgnored(c)) continue;
                if (best < 0 || _probeHits[i].distance < _probeHits[best].distance) best = i;
            }
            float? result = null;
            if (best >= 0)
            {
                RaycastHit h = _probeHits[best];
                int layer = h.collider.gameObject.layer;
                if (layer != LAYER_KILLPLANE && (h.normal.y > 0.7f || layer == SlopedLayer()))
                    result = h.point.y;
            }
            for (int i = 0; i < n; i++) _probeHits[i] = default(RaycastHit);
            return result;
        }

        static int _slopedLayer = -2;
        internal static int SlopedLayer()
        {
            if (_slopedLayer == -2) _slopedLayer = LayerMask.NameToLayer("SlopedGround");
            return _slopedLayer;
        }

        // 胶囊放在 (x, floorY, z)：形状取厨师 body 的世界 bounds，半径扣接触余量，
        // 底部抬 LIFT。返回第一个挡住的 collider，站得下返回 null。
        Collider ProbeBody(Collider body, float footOffset, float x, float floorY, float z)
        {
            Bounds b = body.bounds;
            float skin = Physics.defaultContactOffset;
            float radius = Mathf.Max(skin, Mathf.Min(b.extents.x, b.extents.z) - skin);
            float bottom = floorY + footOffset + WALK_PROBE_LIFT;
            float top = floorY + footOffset + b.size.y - skin;
            float y0 = bottom + radius, y1 = Mathf.Max(y0, top - radius);
            int n = Physics.OverlapCapsuleNonAlloc(new Vector3(x, y0, z), new Vector3(x, y1, z), radius,
                _probeOverlaps, ~0, QueryTriggerInteraction.Ignore);
            Collider blocker = null;
            int bodyLayer = body.gameObject.layer;
            for (int i = 0; i < n; i++)
            {
                Collider o = _probeOverlaps[i];
                _probeOverlaps[i] = null;
                if (blocker != null || o == null) continue;
                if (ProbeIgnored(o) || o.gameObject.layer == SlopedLayer()) continue;
                if (Physics.GetIgnoreLayerCollision(bodyLayer, o.gameObject.layer)) continue;
                blocker = o;
            }
            return blocker;
        }
        // 单个厨师：身体形状、原地自检、周围 (2R+1)² 格逐子点掩码。
        // rows 按 gz 从小到大、每行 gx 从小到大，每格 SUB×SUB 个字符
        // （子点 dz 外层、dx 内层，偏移 -cell/3、0、+cell/3）；who 与 rows 同形，
        // '#' 处是 blockers 下标（0-9a-z，超出记 '+'），其余为 '-'。
        void AppendChefProbe(StringBuilder sb, MonoBehaviour chef, List<Collider> blockers)
        {
            Vector3 p = chef.transform.position;
            Collider body = ChefBody(chef);
            sb.Append("{\"name\":").Append(Jstr(chef.name))
              .Append(",\"x\":").Append(PNum(p.x)).Append(",\"z\":").Append(PNum(p.z));
            if (body == null) { sb.Append(",\"body\":null}"); return; }
            Bounds b = body.bounds;
            // 射线起点放在脚上方（不能放头顶：Balloon_2_3 实测会先打到 y=3 的天花板，
            // floor_y=3/foot=-3）；台面顶 ≤1m、台阶 ≤0.7m，从脚上 1.5m 往下够用。
            float topY = b.min.y + 1.5f;
            float? own = ProbeFloor(p.x, topY, p.z);
            float ownY = own ?? b.min.y;
            float footOffset = b.min.y - ownY;
            sb.Append(",\"body\":{\"type\":").Append(Jstr(body.GetType().Name))
              .Append(",\"layer\":").Append(body.gameObject.layer)
              .Append(",\"radius\":").Append(PNum(Mathf.Min(b.extents.x, b.extents.z)))
              .Append(",\"height\":").Append(PNum(b.size.y));
            var cap = body as CapsuleCollider;
            if (cap != null)
                sb.Append(",\"cap_radius\":").Append(PNum(cap.radius))
                  .Append(",\"cap_height\":").Append(PNum(cap.height))
                  .Append(",\"scale\":").Append(PNum(body.transform.lossyScale.x));
            sb.Append(",\"skin\":").Append(PNum(Physics.defaultContactOffset)).Append('}')
              .Append(",\"floor_y\":").Append(own.HasValue ? PNum(own.Value) : "null")
              .Append(",\"foot\":").Append(PNum(footOffset));

            // 原地自检：厨师此刻就站在这里，若测出阻挡说明过滤或形状有问题。
            Collider self = ProbeBody(body, footOffset, p.x, ownY, p.z);
            sb.Append(",\"self\":").Append(self == null ? "null" : BlockerIndex(blockers, self).ToString(Inv));

            int cx = (int)Math.Round(Math.Round((p.x - _originX) / _cellX, 4));
            int cz = (int)Math.Round(Math.Round((p.z - _originZ) / _cellZ, 4));
            sb.Append(",\"gx0\":").Append(cx - WALK_PROBE_RADIUS)
              .Append(",\"gz0\":").Append(cz - WALK_PROBE_RADIUS)
              .Append(",\"rows\":[");
            var who = new StringBuilder();
            var mask = new StringBuilder();
            for (int gz = cz - WALK_PROBE_RADIUS; gz <= cz + WALK_PROBE_RADIUS; gz++)
            {
                mask.Length = 0;
                who.Length = 0;
                for (int gx = cx - WALK_PROBE_RADIUS; gx <= cx + WALK_PROBE_RADIUS; gx++)
                    for (int sz = 0; sz < WALK_PROBE_SUB; sz++)
                        for (int sx = 0; sx < WALK_PROBE_SUB; sx++)
                        {
                            float x = _originX + _cellX * gx + (sx - 1) * _cellX / 3f;
                            float z = _originZ + _cellZ * gz + (sz - 1) * _cellZ / 3f;
                            char ch, wc = '-';
                            float? fy = ProbeFloor(x, topY, z);
                            if (!fy.HasValue) ch = ' ';
                            else if (fy.Value - ownY > WALK_PROBE_STEP) ch = '^';
                            else if (ownY - fy.Value > WALK_PROBE_STEP) ch = 'v';
                            else
                            {
                                Collider blk = ProbeBody(body, footOffset, x, fy.Value, z);
                                if (blk == null) ch = '.';
                                else
                                {
                                    ch = '#';
                                    int bi = BlockerIndex(blockers, blk);
                                    wc = bi < 0 ? '+' : bi < 10 ? (char)('0' + bi) : bi < 36 ? (char)('a' + bi - 10) : '+';
                                }
                            }
                            mask.Append(ch);
                            who.Append(wc);
                        }
                if (gz > cz - WALK_PROBE_RADIUS) sb.Append(',');
                sb.Append(Jstr(mask.ToString()));
                _whoRows.Add(who.ToString());
            }
            sb.Append("],\"who\":[");
            for (int i = 0; i < _whoRows.Count; i++)
            {
                if (i > 0) sb.Append(',');
                sb.Append(Jstr(_whoRows[i]));
            }
            _whoRows.Clear();
            sb.Append("]}");
        }

        readonly List<string> _whoRows = new List<string>();

        // 阻挡体去重编号；超出上限返回 -1（掩码里记 '+'）。
        static int BlockerIndex(List<Collider> blockers, Collider c)
        {
            int i = blockers.IndexOf(c);
            if (i >= 0) return i;
            if (blockers.Count >= WALK_PROBE_MAX_BLOCKERS) return -1;
            blockers.Add(c);
            return blockers.Count - 1;
        }

        // 阻挡体明细：用来判断 terrain AABB 推算是否可信（是否轴对齐 Box、
        // 层号是否在 TERRAIN_LAYERS 里、是否挂在会动的网格下）。
        void AppendBlocker(StringBuilder sb, Collider c)
        {
            if (c == null) { sb.Append("null"); return; }
            Bounds b = c.bounds;
            Vector3 e = c.transform.eulerAngles;
            Transform t = c.transform;
            string path = t.name;
            if (t.parent != null) path = t.parent.name + "/" + path;
            if (t.parent != null && t.parent.parent != null) path = t.parent.parent.name + "/" + path;
            sb.Append("{\"path\":").Append(Jstr(path))
              .Append(",\"type\":").Append(Jstr(c.GetType().Name))
              .Append(",\"layer\":").Append(c.gameObject.layer)
              .Append(",\"layer_name\":").Append(Jstr(LayerMask.LayerToName(c.gameObject.layer)))
              .Append(",\"terrain\":").Append(Array.IndexOf(TERRAIN_LAYERS, c.gameObject.layer) >= 0 ? "true" : "false")
              .Append(",\"moving\":").Append(UnderAnimator(t) ? "true" : "false")
              .Append(",\"rot\":[").Append(PNum(e.x)).Append(',').Append(PNum(e.y)).Append(',').Append(PNum(e.z)).Append(']')
              .Append(",\"aabb\":[").Append(PNum(b.min.x)).Append(',').Append(PNum(b.max.x)).Append(',')
              .Append(PNum(b.min.z)).Append(',').Append(PNum(b.max.z)).Append(',')
              .Append(PNum(b.min.y)).Append(',').Append(PNum(b.max.y)).Append(']');
            var box = c as BoxCollider;
            if (box != null)
                sb.Append(",\"box_size\":[").Append(PNum(box.size.x)).Append(',').Append(PNum(box.size.y)).Append(',').Append(PNum(box.size.z)).Append(']');
            sb.Append('}');
        }
    }
}

// Oc2ExecServer.cs —— exec 通道服务端：27412 入站读命令、回结果、分派。
//
// .NET 3.5 / 游戏无 JSON 库，这里内联一个最小 JSON 解析器（照 cortico 的
// Json.cs 思路：对象→Dictionary<string,object>、数组→List<object>、数→double）。
// 结果写回用 StringBuilder 手工拼（固定 5 字段形状）。
//
// Task 4 只做骨架：OnCommand 解析 cmd/cmd_id、打日志、回「not implemented」；
// Task 5/6 在 switch 里接真实执行。
using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Net;
using System.Net.Sockets;
using System.Text;
using System.Threading;
using UnityEngine;

namespace Oc2AI
{
    public sealed class ExecServer
    {
        public const int Port = 27412;

        // 单命令硬上限（秒）：任何命令都不该超过它，超了就是 Python 崩了或
        // 状态机 bug，松键兜底。比各命令自己的 timeout_ms 大一个量级。
        private const float WATCHDOG_SECONDS = 20f;

        private TcpListener _listener;
        private Thread _thread;
        private readonly object _lock = new object();
        private StreamWriter _writer;
        private bool _running;

        // 活跃命令（后台线程写、主线程 tick）。同一时刻只有一个活跃命令。
        private ChefFrame _chef;
        private IExecCommand _active;
        private long _activeCmdId;
        private float _activeStartedAt;
        private long _lastTickedCmdId = -1;
        private readonly object _cmdLock = new object();

        public void Start()
        {
            _running = true;
            _thread = new Thread(ServeLoop) { IsBackground = true };
            _thread.Start();
        }

        private void ServeLoop()
        {
            try
            {
                _listener = new TcpListener(IPAddress.Loopback, Port);
                _listener.Start();
                UnityEngine.Debug.Log("[EXEC] listening 127.0.0.1:" + Port);
            }
            catch (Exception e)
            {
                UnityEngine.Debug.LogWarning("[EXEC] listen fail: " + e.Message);
                return;
            }
            while (_running)
            {
                try
                {
                    TcpClient c = _listener.AcceptTcpClient();
                    var reader = new StreamReader(c.GetStream(), Encoding.UTF8);
                    lock (_lock) { _writer = new StreamWriter(c.GetStream(), new UTF8Encoding(false)) { AutoFlush = true }; }
                    UnityEngine.Debug.Log("[EXEC] client connected");
                    string line;
                    while ((line = reader.ReadLine()) != null)
                    {
                        try { OnCommand(line); }
                        catch (Exception e) { UnityEngine.Debug.LogWarning("[EXEC] cmd error: " + e); }
                    }
                }
                catch (Exception)
                {
                    // 客户端断开 / 连接错误：清 writer，回 accept 等待重连
                }
                finally
                {
                    lock (_lock) { _writer = null; }
                }
            }
        }

        private void OnCommand(string line)
        {
            var obj = Json.Parse(line) as Dictionary<string, object>;
            if (obj == null) return;
            long cmdId = Req.Int(obj, "cmd_id");
            string cmd = Req.Str(obj, "cmd");
            if (cmd == null) return;
            UnityEngine.Debug.Log("[EXEC] cmd=" + cmd + " id=" + cmdId);

            if (cmd != "halt" && !PadEngagement.IsEngaged())
            {
                // 未 engage（游戏已在关卡内才启动 agent，虚拟手柄接不进去）→ 回
                // not engaged，Python 侧据此回退旧路径。
                SendResult(cmdId, false, "not engaged");
                return;
            }

            switch (cmd)
            {
                case "move":
                    lock (_cmdLock) { _active = new MoveCommand(obj); _activeCmdId = cmdId; _activeStartedAt = Time.unscaledTime; }
                    break;
                case "tap":
                    lock (_cmdLock) { _active = new TapCommand(obj); _activeCmdId = cmdId; _activeStartedAt = Time.unscaledTime; }
                    break;
                case "hold":
                    lock (_cmdLock) { _active = new HoldCommand(obj); _activeCmdId = cmdId; _activeStartedAt = Time.unscaledTime; }
                    break;
                case "throw":
                    lock (_cmdLock) { _active = new ThrowCommand(obj); _activeCmdId = cmdId; _activeStartedAt = Time.unscaledTime; }
                    break;
                case "halt":
                    Halt();
                    SendResult(cmdId, true, null);
                    break;
                default:
                    SendResult(cmdId, false, "not implemented");
                    break;
            }
        }

        /// <summary>清活跃命令（halt / watchdog）。**不碰 pad**——pad 只在主线程 Tick 里改。</summary>
        public void Halt()
        {
            lock (_cmdLock) { _active = null; _activeCmdId = 0; _activeStartedAt = 0f; }
        }

        /// <summary>主线程统一释放：只在确有输入时清，避免逐帧空刷。</summary>
        private void EnsureReleased()
        {
            if (PadEngagement.Pad != null && PadEngagement.Pad.AnyInput)
                PadEngagement.Pad.ReleaseAll();
        }

        /// <summary>主线程每帧调用：刷新状态、tick 活跃命令、命令完成时回结果。</summary>
        public void Tick()
        {
            if (_chef == null) _chef = new ChefFrame();
            _chef.Refresh();
            float now = Time.unscaledTime;
            IExecCommand active;
            long cid;
            lock (_cmdLock) { active = _active; cid = _activeCmdId; }
            if (active == null)
            {
                // 从有命令转到空闲（halt 清空 / 命令自然结束）：松一次残留输入。
                if (_lastTickedCmdId != -1) { EnsureReleased(); _lastTickedCmdId = -1; }
                return;
            }
            if (cid != _lastTickedCmdId)
            {
                // 新命令到达（旧命令被替换）：先清掉旧命令可能残留的按键。
                EnsureReleased();
                _lastTickedCmdId = cid;
            }
            // watchdog：单命令硬上限，超了松键兜底（Python 崩了或状态机 bug）。
            float started;
            lock (_cmdLock) { started = _activeStartedAt; }
            if (started > 0f && now - started > WATCHDOG_SECONDS)
            {
                UnityEngine.Debug.Log("[EXEC] watchdog released");
                SendResult(cid, false, "watchdog");
                lock (_cmdLock)
                {
                    if (ReferenceEquals(_active, active)) { _active = null; _activeCmdId = 0; _activeStartedAt = 0f; }
                }
                EnsureReleased();   // 松残留（Halt 只清状态不碰 pad）
                _lastTickedCmdId = -1;
                return;
            }
            active.Tick(_chef, now);
            if (active.Done)
            {
                EnsureReleased();
                SendResult(cid, active.Ok, active.Why, null, active.BlockedCell);
                lock (_cmdLock)
                {
                    if (ReferenceEquals(_active, active)) { _active = null; _activeCmdId = 0; _activeStartedAt = 0f; }
                }
                _lastTickedCmdId = -1;
            }
        }

        /// <summary>写一条结果回客户端。heldAfter / blockedCell 可空。</summary>
        public void SendResult(long cmdId, bool ok, string why,
                               string heldAfter = null, double[] blockedCell = null)
        {
            UnityEngine.Debug.Log("[EXEC] result id=" + cmdId + " ok=" + (ok ? "true" : "false")
                                 + " why=" + (why ?? "null"));
            lock (_lock)
            {
                if (_writer == null) return;
                var sb = new StringBuilder("{\"cmd_id\":")
                    .Append(cmdId.ToString(CultureInfo.InvariantCulture))
                    .Append(",\"ok\":").Append(ok ? "true" : "false")
                    .Append(",\"why\":").Append(Json.Quote(why))
                    .Append(",\"held_after\":").Append(Json.Quote(heldAfter));
                if (blockedCell != null && blockedCell.Length == 2)
                {
                    sb.Append(",\"blocked_cell\":[")
                      .Append(blockedCell[0].ToString("R", CultureInfo.InvariantCulture)).Append(',')
                      .Append(blockedCell[1].ToString("R", CultureInfo.InvariantCulture)).Append(']');
                }
                else
                {
                    sb.Append(",\"blocked_cell\":null");
                }
                sb.Append('}');
                try { _writer.WriteLine(sb.ToString()); }
                catch (Exception) { }
            }
        }
    }

    /// <summary>最小 JSON 解析：对象→Dictionary&lt;string,object&gt;，数组→List&lt;object&gt;，数→double。</summary>
    internal static class Json
    {
        public static object Parse(string text)
        {
            int i = 0;
            var value = Value(text, ref i);
            Skip(text, ref i);
            if (i != text.Length) throw new FormatException("trailing characters at " + i);
            return value;
        }

        public static string Quote(string s)
        {
            if (s == null) return "null";
            var sb = new StringBuilder(s.Length + 2);
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
            return sb.Append('"').ToString();
        }

        private static void Skip(string s, ref int i)
        {
            while (i < s.Length && char.IsWhiteSpace(s[i])) i++;
        }

        private static object Value(string s, ref int i)
        {
            Skip(s, ref i);
            if (i >= s.Length) throw new FormatException("unexpected end");
            char c = s[i];
            if (c == '{')
            {
                var obj = new Dictionary<string, object>();
                i++;
                Skip(s, ref i);
                if (s[i] == '}') { i++; return obj; }
                while (true)
                {
                    Skip(s, ref i);
                    string key = String(s, ref i);
                    Skip(s, ref i);
                    if (s[i++] != ':') throw new FormatException("expected ':' at " + i);
                    obj[key] = Value(s, ref i);
                    Skip(s, ref i);
                    char next = s[i++];
                    if (next == '}') return obj;
                    if (next != ',') throw new FormatException("expected ',' at " + i);
                }
            }
            if (c == '[')
            {
                var list = new List<object>();
                i++;
                Skip(s, ref i);
                if (s[i] == ']') { i++; return list; }
                while (true)
                {
                    list.Add(Value(s, ref i));
                    Skip(s, ref i);
                    char next = s[i++];
                    if (next == ']') return list;
                    if (next != ',') throw new FormatException("expected ',' at " + i);
                }
            }
            if (c == '"') return String(s, ref i);
            if (s.Length - i >= 4 && string.CompareOrdinal(s, i, "true", 0, 4) == 0) { i += 4; return true; }
            if (s.Length - i >= 5 && string.CompareOrdinal(s, i, "false", 0, 5) == 0) { i += 5; return false; }
            if (s.Length - i >= 4 && string.CompareOrdinal(s, i, "null", 0, 4) == 0) { i += 4; return null; }
            int start = i;
            while (i < s.Length && "+-0123456789.eE".IndexOf(s[i]) >= 0) i++;
            if (start == i) throw new FormatException("unexpected '" + c + "' at " + i);
            return double.Parse(s.Substring(start, i - start), NumberStyles.Float, CultureInfo.InvariantCulture);
        }

        private static string String(string s, ref int i)
        {
            if (s[i] != '"') throw new FormatException("expected string at " + i);
            i++;
            var sb = new StringBuilder();
            while (true)
            {
                char c = s[i++];
                if (c == '"') return sb.ToString();
                if (c != '\\') { sb.Append(c); continue; }
                char e = s[i++];
                switch (e)
                {
                    case 'n': sb.Append('\n'); break;
                    case 'r': sb.Append('\r'); break;
                    case 't': sb.Append('\t'); break;
                    case 'b': sb.Append('\b'); break;
                    case 'f': sb.Append('\f'); break;
                    case 'u': sb.Append((char)int.Parse(s.Substring(i, 4), NumberStyles.HexNumber)); i += 4; break;
                    default: sb.Append(e); break;
                }
            }
        }
    }

    /// <summary>从解析出的命令对象里做类型化读取。</summary>
    internal static class Req
    {
        public static string Str(Dictionary<string, object> r, string key)
        {
            return r.TryGetValue(key, out object v) ? v as string : null;
        }

        public static long Int(Dictionary<string, object> r, string key)
        {
            if (r.TryGetValue(key, out object v) && v is double d) return (long)d;
            return 0;
        }

        public static double? Num(Dictionary<string, object> r, string key)
        {
            if (r.TryGetValue(key, out object v) && v is double d) return d;
            return null;
        }

        public static bool Bool(Dictionary<string, object> r, string key)
        {
            return r.TryGetValue(key, out object v) && v is bool b && b;
        }

        public static List<object> List(Dictionary<string, object> r, string key)
        {
            return r.TryGetValue(key, out object v) ? v as List<object> : null;
        }

        public static Dictionary<string, object> Obj(Dictionary<string, object> r, string key)
        {
            return r.TryGetValue(key, out object v) ? v as Dictionary<string, object> : null;
        }
    }
}

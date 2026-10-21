using System;
using System.Globalization;
using UnityEngine;

namespace Oc2AI
{
    public partial class Oc2AIPlugin
    {
        readonly string _sessionId = Guid.NewGuid().ToString("N");
        int _sceneEpoch, _flowId;
        volatile bool _stopping;
        BepInEx.Configuration.ConfigEntry<bool> _discoverEnabled;
        string _performanceJson = "null";
        int _frameCount, _slowFrames;
        double _frameSum, _frameMax;

        void SavePerformance(System.Diagnostics.Stopwatch watch)
        {
            _performanceJson = "{\"sample\":\"previous_snapshot\",\"snapshot_ms\":"
                + watch.Elapsed.TotalMilliseconds.ToString("0.00", CultureInfo.InvariantCulture)
                + ",\"components\":" + _all.Length + ",\"render_frames\":" + _frameCount
                + ",\"mean_frame_ms\":" + (_frameCount == 0 ? 0 : _frameSum / _frameCount).ToString("0.00", CultureInfo.InvariantCulture)
                + ",\"max_frame_ms\":" + _frameMax.ToString("0.00", CultureInfo.InvariantCulture)
                + ",\"frames_over_33ms\":" + _slowFrames + "}";
            _frameCount = _slowFrames = 0; _frameSum = _frameMax = 0;
        }

        void OnDestroy()
        {
            _stopping = true; CancelInvoke("Snapshot");
            try { _listener?.Stop(); } catch { }
            lock (_lock)
            {
                foreach (var client in _clients) try { client.Close(); } catch { }
                _clients.Clear(); _out.Clear();
            }
        }
    }
}

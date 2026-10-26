// PerfCattle Unity capture helper.
//
// Drop this file (and PerfCattleCaptureRunner.cs) anywhere in a Unity project.
// No scene wiring is needed: RuntimeInitializeOnLoadMethod starts it in the
// player.  It does two things:
//
//   1. writes the Unity Profiler binary log to persistentDataPath/perfcattle/*.raw
//      (logging is off until PerfCattle sends "start")
//   2. accepts commands from PerfCattle on 127.0.0.1 (port written to port.txt)
//
// Commands (one line each):
//   ping                 -> ok <unity_version> <product_name> <debug_build>
//   start <name>         -> ok <path>       rotate the binary log to <name>.raw
//   snapshot <name>      -> started <path> then ok <path> / err <reason>
//   stats                -> ok <json>       live Unity counters (frame time, draw calls, ...)
//   stop                 -> ok              flush and close the log
//
// Requirements: a Development Build (the Profiler API is inert otherwise) and,
// for snapshots, the Memory Profiler package in the project.
using System;
using System.Globalization;
using System.IO;
using System.Net;
using System.Net.Sockets;
using System.Reflection;
using System.Text;
using System.Threading;
using UnityEngine;
using UnityEngine.Profiling;
#if UNITY_2019_3_OR_NEWER
using Unity.Profiling;
#endif

public static class PerfCattleCapture
{
    const int BasePort = 39777;
    const int PortTries = 24;

    static TcpListener listener;
    static Thread acceptThread;
    static string captureDir;
    static volatile bool running;

    [RuntimeInitializeOnLoadMethod(RuntimeInitializeLoadType.BeforeSceneLoad)]
    static void Bootstrap()
    {
        if (Application.isEditor) return;
        try { StartCapture(); }
        catch (Exception e) { Debug.LogWarning("[PerfCattle] capture init failed: " + e); }
    }

    static void StartCapture()
    {
        captureDir = Path.Combine(Application.persistentDataPath, "perfcattle");
        Directory.CreateDirectory(captureDir);

        var runner = new GameObject("PerfCattleCaptureRunner");
        runner.hideFlags = HideFlags.HideAndDontSave;
        UnityEngine.Object.DontDestroyOnLoad(runner);
        runner.AddComponent<PerfCattleCaptureRunner>();

        int port = StartListener();
        File.WriteAllText(Path.Combine(captureDir, "port.txt"), port.ToString());
        WriteInfo();
        running = true;
        acceptThread = new Thread(AcceptLoop) { IsBackground = true, Name = "PerfCattleCapture" };
        acceptThread.Start();
        Debug.Log("[PerfCattle] capture helper ready on 127.0.0.1:" + port);
    }

    static int StartListener()
    {
        for (int i = 0; i < PortTries; i++)
        {
            int port = BasePort + i;
            try
            {
                var candidate = new TcpListener(IPAddress.Loopback, port);
                candidate.Start();
                listener = candidate;
                return port;
            }
            catch (SocketException) { }
        }
        throw new Exception("no free port in " + BasePort + "-" + (BasePort + PortTries - 1));
    }

    static void AcceptLoop()
    {
        while (running)
        {
            TcpClient client;
            try { client = listener.AcceptTcpClient(); }
            catch (Exception) { break; }
            try { Handle(client); }
            catch (Exception e) { Debug.LogWarning("[PerfCattle] command failed: " + e.Message); }
            finally { try { client.Close(); } catch (Exception) { } }
        }
    }

    static void Handle(TcpClient client)
    {
        using (var stream = client.GetStream())
        using (var reader = new StreamReader(stream, Encoding.UTF8))
        using (var writer = new StreamWriter(stream, new UTF8Encoding(false)) { AutoFlush = true })
        {
            string line;
            while ((line = reader.ReadLine()) != null)
            {
                line = line.Trim();
                if (line.Length == 0) continue;
                string[] parts = line.Split(new[] { ' ' }, 2);
                try
                {
                    switch (parts[0])
                    {
                        case "ping":
                            writer.WriteLine("ok " + Application.unityVersion + " "
                                             + Token(Application.productName) + " "
                                             + (Debug.isDebugBuild ? 1 : 0));
                            break;
                        case "start":
                            writer.WriteLine("ok " + RotateLog(Argument(parts, "profiler")));
                            break;
                        case "snapshot":
                            TakeSnapshot(Argument(parts, DateTime.Now.ToString("yyyyMMdd_HHmmss")), writer);
                            break;
                        case "stats":
                            writer.WriteLine("ok " + PerfCattleGameStats.Snapshot);
                            break;
                        case "stop":
                            // keep listening: another PerfCattle session can start later
                            DisableProfilerOnMainThread();
                            writer.WriteLine("ok");
                            break;
                        default:
                            writer.WriteLine("err unknown_command");
                            break;
                    }
                }
                catch (Exception e)
                {
                    writer.WriteLine("err " + Token(e.GetType().Name));
                }
            }
        }
    }

    static string Argument(string[] parts, string fallback)
    {
        return parts.Length > 1 && parts[1].Trim().Length > 0 ? parts[1].Trim() : fallback;
    }

    static string RotateLog(string name)
    {
        string path = Path.Combine(captureDir, name + ".raw");
        Profiler.logFile = path;
        Profiler.enableBinaryLog = true;
        Profiler.enabled = true;
        return path;
    }

    static void TakeSnapshot(string name, StreamWriter writer)
    {
        string path = Path.Combine(captureDir, name + ".snapshot");
        writer.WriteLine("started " + path);

        MethodInfo method = FindSnapshotMethod();
        if (method == null)
        {
            writer.WriteLine("err memoryprofiler_not_installed");
            return;
        }

        var done = new ManualResetEventSlim(false);
        Action<string, bool> finished = (snapshotPath, ok) =>
        {
            try { writer.WriteLine(ok ? "ok " + snapshotPath : "err snapshot_failed"); }
            catch (Exception) { }
            finally { done.Set(); }
        };

        object[] args = method.GetParameters().Length >= 3
            ? new object[] { path, finished, false }
            : new object[] { path, finished };

        // snapshotting must run on the main thread
        PerfCattleCaptureRunner.Enqueue(() =>
        {
            try { method.Invoke(null, args); }
            catch (Exception e)
            {
                Exception inner = e.InnerException ?? e;
                try { writer.WriteLine("err " + Token(inner.GetType().Name)); }
                catch (Exception) { }
                done.Set();
            }
        });

        if (!done.Wait(TimeSpan.FromSeconds(300)))
        {
            try { writer.WriteLine("err snapshot_timeout"); }
            catch (Exception) { }
        }
    }

    static MethodInfo FindSnapshotMethod()
    {
        foreach (var assembly in AppDomain.CurrentDomain.GetAssemblies())
        {
            var type = assembly.GetType("Unity.MemoryProfiler.MemoryProfiler", false);
            if (type == null) continue;
            foreach (var method in type.GetMethods(BindingFlags.Public | BindingFlags.Static))
            {
                if (method.Name != "TakeSnapshot") continue;
                var parameters = method.GetParameters();
                if (parameters.Length >= 2 && parameters[0].ParameterType == typeof(string))
                {
                    return method;
                }
            }
        }
        return null;
    }

    static void DisableProfilerOnMainThread()
    {
        var done = new ManualResetEventSlim(false);
        PerfCattleCaptureRunner.Enqueue(() => { DisableProfiler(); done.Set(); });
        done.Wait(2000);
    }

    internal static void DisableProfiler()
    {
        try
        {
            Profiler.enableBinaryLog = false;
            Profiler.enabled = false;
            Profiler.logFile = string.Empty;
        }
        catch (Exception e) { Debug.LogWarning("[PerfCattle] disable profiler failed: " + e.Message); }
    }

    static void WriteInfo()
    {
        string json = "{\n"
            + "  \"unity_version\": \"" + Json(Application.unityVersion) + "\",\n"
            + "  \"product_name\": \"" + Json(Application.productName) + "\",\n"
            + "  \"company_name\": \"" + Json(Application.companyName) + "\",\n"
            + "  \"platform\": \"" + Json(Application.platform.ToString()) + "\",\n"
            + "  \"debug_build\": " + (Debug.isDebugBuild ? "true" : "false") + ",\n"
            + "  \"started_at\": \"" + DateTime.Now.ToString("yyyy-MM-dd HH:mm:ss") + "\"\n"
            + "}\n";
        File.WriteAllText(Path.Combine(captureDir, "info.json"), json);
    }

    static string Json(string value)
    {
        if (string.IsNullOrEmpty(value)) return string.Empty;
        return value.Replace("\\", "\\\\").Replace("\"", "\\\"")
                    .Replace("\r", " ").Replace("\n", " ");
    }

    static string Token(string value)
    {
        if (string.IsNullOrEmpty(value)) return "-";
        return value.Replace(' ', '_').Replace('\r', '_').Replace('\n', '_');
    }
}

// Live Unity performance counters for the PerfCattle game panel.
//
// Sampling runs on the main thread (PerfCattleCaptureRunner.Update -> Tick) and
// caches one JSON snapshot that the command thread can read at any time.
// ProfilerRecorder needs Unity 2019.3+; older versions keep answering stats
// with an explicit "unsupported_unity_version" reason instead of failing.
#if UNITY_2019_3_OR_NEWER
internal static class PerfCattleGameStats
{
    sealed class Metric
    {
        public string Key;
        public string Kind;
        public string[] Names;
        public ProfilerRecorder Recorder;
        public string MatchedName;
        public int Attempts;
        public float NextAttempt;

        public bool Valid { get { return Recorder.Valid; } }
    }

    const int MaxAttempts = 30;
    const float RetryInterval = 2f;
    const float SampleInterval = 0.5f;

    static readonly Metric[] Metrics = new Metric[]
    {
        New("cpu_frame_ms", "ns", "CPU Main Thread Frame Time", "PlayerLoop"),
        New("render_frame_ms", "ns", "CPU Render Thread Frame Time"),
        New("cpu_total_ms", "ns", "CPU Total Frame Time"),
        New("gpu_frame_ms", "ns", "GPU Frame Time"),
        New("draw_calls", "count", "Draw Calls Count", "Draw Calls"),
        New("setpass_calls", "count", "SetPass Calls Count", "SetPass Calls"),
        New("static_batches", "count", "Static Batches Count"),
        New("dynamic_batches", "count", "Dynamic Batches Count"),
        New("instanced_batches", "count", "Instanced Batches Count"),
        New("static_batched_draws", "count", "Static Batched Draw Calls Count"),
        New("dynamic_batched_draws", "count", "Dynamic Batched Draw Calls Count"),
        New("instanced_batched_draws", "count", "Instanced Batched Draw Calls Count"),
        New("batches", "count", "Batches Count", "Batches"),
        New("triangles", "count", "Triangles Count", "Triangles"),
        New("vertices", "count", "Vertices Count", "Vertices"),
        New("shadow_casters", "count", "Shadow Casters Count", "Shadow Casters"),
        New("render_textures", "count", "Render Textures Count"),
        New("render_textures_mb", "bytes_mb", "Render Textures Bytes"),
        New("render_textures_changes", "count", "Render Textures Changes Count"),
        New("used_buffers", "count", "Used Buffers Count"),
        New("used_buffers_mb", "bytes_mb", "Used Buffers Bytes"),
        New("vertex_upload", "count", "Vertex Buffer Upload In Frame Count"),
        New("vertex_upload_kb", "bytes_kb", "Vertex Buffer Upload In Frame Bytes"),
        New("index_upload", "count", "Index Buffer Upload In Frame Count"),
        New("index_upload_kb", "bytes_kb", "Index Buffer Upload In Frame Bytes"),
        New("gc_alloc_kb", "bytes_kb", "GC Allocated In Frame", "GC Alloc In Frame"),
        New("used_memory_mb", "bytes_mb", "Total Used Memory"),
        New("gc_used_memory_mb", "bytes_mb", "GC Used Memory"),
    };

    static bool started;
    static float nextSample;
    static volatile string snapshot = "{\"available\":false,\"reason\":\"not_started\"}";

    internal static string Snapshot { get { return snapshot; } }

    static Metric New(string key, string kind, params string[] names)
    {
        return new Metric { Key = key, Kind = kind, Names = names };
    }

    internal static void Tick()
    {
        float now = Time.unscaledTime;
        if (!started)
        {
            started = true;
            Initialize(now);
        }
        RetryMissing(now);
        if (now < nextSample) return;
        nextSample = now + SampleInterval;
        Sample();
    }

    static void Initialize(float now)
    {
        for (int i = 0; i < Metrics.Length; i++)
        {
            TryCreate(Metrics[i], now);
        }
    }

    static void RetryMissing(float now)
    {
        for (int i = 0; i < Metrics.Length; i++)
        {
            Metric metric = Metrics[i];
            if (metric.Valid || metric.Attempts >= MaxAttempts) continue;
            if (now < metric.NextAttempt) continue;
            TryCreate(metric, now);
        }
    }

    static void TryCreate(Metric metric, float now)
    {
        metric.Attempts++;
        metric.NextAttempt = now + RetryInterval;
        for (int i = 0; i < metric.Names.Length; i++)
        {
            string name = metric.Names[i];
            try
            {
                ProfilerRecorder recorder = ProfilerRecorder.StartNew(name, 1);
                if (recorder.Valid)
                {
                    metric.Recorder = recorder;
                    metric.MatchedName = name;
                    return;
                }
                recorder.Dispose();
            }
            catch (Exception)
            {
                // Marker is missing on this Unity version; try the next alias.
            }
        }
    }

    static void Sample()
    {
        var sb = new StringBuilder(768);
        int valid = 0;
        sb.Append("{\"available\":");
        // First pass only counts so an empty recorder set reports a stable reason.
        for (int i = 0; i < Metrics.Length; i++)
        {
            if (Metrics[i].Valid) valid++;
        }
        if (valid == 0)
        {
            snapshot = "{\"available\":false,\"reason\":\"no_markers\",\"debug_build\":"
                       + (Debug.isDebugBuild ? "true" : "false") + "}";
            return;
        }

        sb.Append("true,\"debug_build\":");
        sb.Append(Debug.isDebugBuild ? "true" : "false");
        sb.Append(",\"frame\":").Append(Time.frameCount);
        sb.Append(",\"values\":{");
        bool first = true;
        for (int i = 0; i < Metrics.Length; i++)
        {
            Metric metric = Metrics[i];
            if (!metric.Valid) continue;
            long raw;
            try { raw = metric.Recorder.LastValue; }
            catch (Exception) { continue; }
            double value = Normalize(metric.Kind, raw);
            if (double.IsNaN(value) || double.IsInfinity(value)) continue;
            if (!first) sb.Append(',');
            first = false;
            sb.Append('"').Append(metric.Key).Append("\":");
            sb.Append(value.ToString("0.##", CultureInfo.InvariantCulture));
        }
        sb.Append("},\"markers\":{");
        first = true;
        for (int i = 0; i < Metrics.Length; i++)
        {
            Metric metric = Metrics[i];
            if (!metric.Valid || string.IsNullOrEmpty(metric.MatchedName)) continue;
            if (!first) sb.Append(',');
            first = false;
            sb.Append('"').Append(metric.Key).Append("\":\"");
            sb.Append(metric.MatchedName.Replace("\"", "")).Append('"');
        }
        sb.Append("},\"missing\":[");
        first = true;
        for (int i = 0; i < Metrics.Length; i++)
        {
            if (Metrics[i].Valid) continue;
            if (!first) sb.Append(',');
            first = false;
            sb.Append('"').Append(Metrics[i].Key).Append('"');
        }
        sb.Append("]}");
        snapshot = sb.ToString();
    }

    static double Normalize(string kind, long raw)
    {
        if (kind == "ns") return raw / 1000000.0;          // nanoseconds -> ms
        if (kind == "bytes_kb") return raw / 1024.0;      // bytes -> KB
        if (kind == "bytes_mb") return raw / 1048576.0;   // bytes -> MB
        return raw;
    }
}
#else
internal static class PerfCattleGameStats
{
    internal static string Snapshot
    {
        get { return "{\"available\":false,\"reason\":\"unsupported_unity_version\"}"; }
    }

    internal static void Tick() { }
}
#endif

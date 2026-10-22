// PerfCat Unity capture helper.
//
// Drop this file (and PerfCatCaptureRunner.cs) anywhere in a Unity project.
// No scene wiring is needed: RuntimeInitializeOnLoadMethod starts it in the
// player.  It does two things:
//
//   1. writes the Unity Profiler binary log to persistentDataPath/perfcat/*.raw
//      (logging is off until PerfCat sends "start")
//   2. accepts commands from PerfCat on 127.0.0.1 (port written to port.txt)
//
// Commands (one line each):
//   ping                 -> ok <unity_version> <product_name> <debug_build>
//   start <name>         -> ok <path>       rotate the binary log to <name>.raw
//   snapshot <name>      -> started <path> then ok <path> / err <reason>
//   stop                 -> ok              flush and close the log
//
// Requirements: a Development Build (the Profiler API is inert otherwise) and,
// for snapshots, the Memory Profiler package in the project.
using System;
using System.IO;
using System.Net;
using System.Net.Sockets;
using System.Reflection;
using System.Text;
using System.Threading;
using UnityEngine;
using UnityEngine.Profiling;

public static class PerfCatCapture
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
        catch (Exception e) { Debug.LogWarning("[PerfCat] capture init failed: " + e); }
    }

    static void StartCapture()
    {
        captureDir = Path.Combine(Application.persistentDataPath, "perfcat");
        Directory.CreateDirectory(captureDir);

        var runner = new GameObject("PerfCatCaptureRunner");
        runner.hideFlags = HideFlags.HideAndDontSave;
        UnityEngine.Object.DontDestroyOnLoad(runner);
        runner.AddComponent<PerfCatCaptureRunner>();

        int port = StartListener();
        File.WriteAllText(Path.Combine(captureDir, "port.txt"), port.ToString());
        WriteInfo();
        running = true;
        acceptThread = new Thread(AcceptLoop) { IsBackground = true, Name = "PerfCatCapture" };
        acceptThread.Start();
        Debug.Log("[PerfCat] capture helper ready on 127.0.0.1:" + port);
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
            catch (Exception e) { Debug.LogWarning("[PerfCat] command failed: " + e.Message); }
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
                        case "stop":
                            // keep listening: another PerfCat session can start later
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
        PerfCatCaptureRunner.Enqueue(() =>
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
        PerfCatCaptureRunner.Enqueue(() => { DisableProfiler(); done.Set(); });
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
        catch (Exception e) { Debug.LogWarning("[PerfCat] disable profiler failed: " + e.Message); }
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

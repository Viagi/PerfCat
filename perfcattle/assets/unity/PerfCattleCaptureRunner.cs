// Main-thread dispatcher for PerfCattleCapture.
//
// Unity APIs used by the capture helper (Memory Profiler snapshots in
// particular) have to run on the main thread.  PerfCattleCapture creates this
// component automatically; do not add it to a scene yourself.
using System;
using System.Collections.Concurrent;
using UnityEngine;

public sealed class PerfCattleCaptureRunner : MonoBehaviour
{
    static readonly ConcurrentQueue<Action> queue = new ConcurrentQueue<Action>();

    internal static void Enqueue(Action action)
    {
        if (action != null) queue.Enqueue(action);
    }

    void Update()
    {
        PerfCattleGameStats.Tick();
        Action action;
        while (queue.TryDequeue(out action))
        {
            try { action(); }
            catch (Exception e) { Debug.LogException(e); }
        }
    }

    void OnApplicationQuit()
    {
        PerfCattleCapture.DisableProfiler();
    }
}

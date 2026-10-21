"""JSON-lines worker; each requested Instruments stream is opened once."""
import json
import queue
import sys
import threading

import tidevice
from logzero import logger
from perfcat.iosperf._perf import DataType, Performance

GROUPS = {
    "cpu": (DataType.CPU, DataType.MEMORY),
    "memory": (DataType.CPU, DataType.MEMORY),
    "fps": (DataType.FPS, DataType.GPU),
    "gpu": (DataType.FPS, DataType.GPU),
    "network": (DataType.NETWORK,),
}


def main():
    output = sys.stdout
    sys.stdout = sys.stderr
    lock = threading.Lock()
    commands = queue.Queue()
    stopped = threading.Event()
    streams = {}
    fps_samples = 0

    def emit(message):
        with lock:
            output.write(json.dumps(message) + "\n")
            output.flush()

    def on_sample(metric, value):
        nonlocal fps_samples
        if metric == DataType.FPS:
            fps_samples += 1
            if fps_samples == 1:
                return
        emit({"metric": metric.value, "value": value})

    def on_error(exc):
        emit({"error": str(exc)})
        stopped.set()

    def read_commands():
        try:
            for line in sys.stdin:
                commands.put(json.loads(line))
        finally:
            stopped.set()

    threading.Thread(target=read_commands, daemon=True).start()
    try:
        while not stopped.is_set():
            try:
                message = commands.get(timeout=0.2)
            except queue.Empty:
                continue
            group = GROUPS[message["metric"]]
            if group not in streams:
                perf = Performance(tidevice.Device(udid=sys.argv[1]), list(group))
                streams[group] = perf
                perf.on_error = on_error
                perf.start(sys.argv[2], callback=on_sample)
    except Exception as exc:
        logger.exception("iOS worker failed")
        on_error(exc)
    finally:
        for perf in streams.values():
            perf.stop()


if __name__ == "__main__":
    main()

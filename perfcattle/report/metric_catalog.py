# -*- coding: utf-8 -*-
"""对比报告的指标目录。

采集端往 ``unity_stats.log`` / ``gpu_stats.log`` 增加计数器时，在这里登记
一次即可：对比页的摘要行与曲线卡片、以及 ``compare.xls`` / ``compare.json``
的指标行都由本目录生成，避免“采集到了但对比报告里没有”的漂移。

字段：``key``、中文名、英文名、分组、小数位（0 表示整数）、
``page_extra``（对比页此前未展示，需要额外生成摘要行）、``chart``
（是否为该指标生成对比曲线）、单位。
"""

# key, cn, en, group, decimals, page_extra, chart, unit
UNITY_METRICS = (
    ("cpu_frame_ms", "CPU 帧耗时", "CPU Frame (ms)", "frame", 2, False, False, "ms"),
    ("render_frame_ms", "渲染线程", "Render Thread (ms)", "frame", 2, True, True, "ms"),
    ("cpu_total_ms", "CPU 总耗时", "CPU Total (ms)", "frame", 2, True, True, "ms"),
    ("gpu_frame_ms", "GPU 帧耗时", "GPU Frame (ms)", "frame", 2, False, False, "ms"),
    ("draw_calls", "Draw Calls", "Draw Calls", "batching", 0, False, False, ""),
    ("setpass_calls", "SetPass Calls", "SetPass Calls", "batching", 0, True, True, ""),
    ("batches", "Batches", "Batches", "batching", 0, True, True, ""),
    ("static_batches", "静态合批", "Static Batches", "batching", 0, True, True, ""),
    ("dynamic_batches", "动态合批", "Dynamic Batches", "batching", 0, True, True, ""),
    ("instanced_batches", "实例化合批", "Instanced Batches", "batching", 0, True, True, ""),
    ("static_batched_draws", "静态合批 DC", "Static Batched Draw Calls", "batching", 0, True, False, ""),
    ("dynamic_batched_draws", "动态合批 DC", "Dynamic Batched Draw Calls", "batching", 0, True, False, ""),
    ("instanced_batched_draws", "实例化合批 DC", "Instanced Batched Draw Calls", "batching", 0, True, False, ""),
    ("shadow_casters", "阴影投射", "Shadow Casters", "batching", 0, True, False, ""),
    ("triangles", "三角面", "Triangles (k)", "geometry", 1, False, False, "k"),
    ("vertices", "顶点", "Vertices (k)", "geometry", 1, False, False, "k"),
    ("gc_alloc_kb", "GC Alloc/帧", "GC Alloc (KB/frame)", "memory", 2, True, True, "KB"),
    ("used_memory_mb", "Unity 已用内存", "Unity Memory (MB)", "memory", 2, True, True, "MB"),
    ("gc_used_memory_mb", "Mono / GC 已用内存", "Mono / GC Memory (MB)", "memory", 2, True, True, "MB"),
    ("native_memory_mb", "Native 内存", "Native Memory (MB)", "memory", 2, True, True, "MB"),
    ("render_textures", "Render Textures", "Render Textures (Count)", "resources", 0, False, False, ""),
    ("render_textures_mb", "Render Textures", "Render Textures (MB)", "resources", 2, False, False, "MB"),
    ("render_textures_changes", "RT 切换", "Render Textures Changes", "resources", 0, False, False, ""),
    ("used_buffers", "Used Buffers", "Used Buffers (Count)", "resources", 0, False, False, ""),
    ("used_buffers_mb", "Used Buffers", "Used Buffers (MB)", "resources", 2, False, False, "MB"),
    ("vertex_upload", "VB 上传", "Vertex Uploads (Count)", "resources", 0, False, False, ""),
    ("vertex_upload_kb", "VB 上传", "Vertex Upload (KB)", "resources", 2, False, False, "KB"),
    ("index_upload", "IB 上传", "Index Uploads (Count)", "resources", 0, False, False, ""),
    ("index_upload_kb", "IB 上传", "Index Upload (KB)", "resources", 2, False, False, "KB"),
)

# key, cn, en, group, decimals, page_extra, chart, unit
GPU_METRICS = (
    ("gpu_temp", "GPU 温度", "Temperature (C)", "gpu", 2, True, True, "°C"),
    ("gpu_clock", "GPU 频率", "Clock (MHz)", "gpu", 1, True, True, "MHz"),
    ("gpu_bus", "GPU 总线", "Bus (MHz)", "gpu", 1, True, True, "MHz"),
    ("gpu_mem_app", "显存(应用)", "App Memory (MB)", "gpu", 2, True, True, "MB"),
    ("gpu_mem_total", "显存(总计)", "Total Memory (MB)", "gpu", 2, True, True, "MB"),
    ("gpu_bw_read_frame", "带宽读/帧", "Read (MB/frame)", "gpu", 2, True, True, "MB/frame"),
    ("gpu_bw_write_frame", "带宽写/帧", "Write (MB/frame)", "gpu", 2, True, True, "MB/frame"),
)

# 对比页补充分组：group -> (中文标题, 英文标题)
EXTRA_SECTIONS = (
    ("frame", "Unity 帧耗时", "Unity Frame"),
    ("batching", "Unity 批处理", "Unity Batching"),
    ("geometry", "Unity 几何", "Unity Geometry"),
    ("memory", "Unity 内存 / GC", "Unity Memory / GC"),
    ("resources", "Unity 渲染资源", "Unity Render Resources"),
    ("gpu", "GPU 扩展", "GPU Details"),
)

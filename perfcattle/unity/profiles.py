# -*- coding: utf-8 -*-
"""Unity PlayerConnection 版本与消息表注册中心。

Unity 的 Profiler / MemoryProfiler 消息 GUID 是按版本内置在 Player 里的，
不同 Unity 版本可能不同。所有版本差异都收敛在本文件：

- ``UnityProfile`` 描述一个 PlayerConnection 协议版本
- ``PROFILES`` 是版本号到 profile 的映射
- 新版本适配只需要新增一个 profile，不需要改协议层和采集层

消息 GUID 的原始形式是 Unity 内部的 32 位十六进制字符串，编码规则见
``perfcattle.unity.protocol.guid_from_hex`` 和 docs/UNITY_CAPTURE.md。
"""
from dataclasses import dataclass
from typing import Dict, FrozenSet, Iterable, List, Optional, Tuple

# 功能能力位：采集层只依赖这些名字，不依赖具体消息 GUID
FEATURE_PROFILER_STREAM = "profiler_stream"
FEATURE_MEMORY_SNAPSHOT = "memory_snapshot"
FEATURE_DEEP_PROFILING = "deep_profiling"
FEATURE_MEMORY_RECORD_MODE = "memory_record_mode"
FEATURE_FUNCTIONS_DATA = "functions_data"
FEATURE_MARKER_FILTERING = "marker_filtering"
FEATURE_ENABLED_CATEGORIES = "enabled_categories"
FEATURE_OBJECT_MEMORY_PROFILE = "object_memory_profile"
FEATURE_INSTRUMENTABLE_FUNCTIONS = "instrumentable_functions"
FEATURE_FUNCTION_CALLEES = "function_callees"
FEATURE_AUDIO_CAPTURE_FLAGS = "audio_capture_flags"


@dataclass(frozen=True)
class ProfilerArea:
    """Unity Profiler 的一个 Module（ProfilerArea）。

    ``name`` 是 Unity ``ProfilerArea`` 枚举名，也是 UI 中勾选项目的稳定标识；
    ``category`` 是该模块在 Player 中对应的 ``ProfilerCategory.Name``。
    多个 Area 可以共享同一个 Category（例如 UI / UI Details 都使用 ``Gui``），
    因此协议层发送的是去重后的 Category 名称集合，而不是 Area 名称。
    """

    name: str
    label: str
    category: str
    default_enabled: bool = True

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "label": self.label,
            "category": self.category,
            "default_enabled": self.default_enabled,
        }


# Unity 2022.3 的 ProfilerArea（枚举名来自 global-metadata.dat），以及
# ProfilerCategoryActivator / LegacyProfilerAreaUtility 中 Area -> Category 的映射。
PROFILER_AREAS: Tuple[ProfilerArea, ...] = (
    ProfilerArea("CPU", "CPU Usage", "Scripts"),
    ProfilerArea("GPU", "GPU Usage", "GPU"),
    ProfilerArea("Rendering", "Rendering", "Render"),
    ProfilerArea("Memory", "Memory", "Memory"),
    ProfilerArea("Audio", "Audio", "Audio"),
    ProfilerArea("Video", "Video", "Video"),
    ProfilerArea("Physics", "Physics", "Physics"),
    ProfilerArea("Physics2D", "Physics 2D", "Physics2D"),
    ProfilerArea("NetworkMessages", "Network Messages", "Network"),
    ProfilerArea("NetworkOperations", "Network Operations", "Network Operations"),
    ProfilerArea("UI", "UI", "Gui"),
    ProfilerArea("UIDetails", "UI Details", "Gui"),
    ProfilerArea("GlobalIllumination", "Global Illumination", "Lighting"),
    ProfilerArea("VirtualTexturing", "Virtual Texturing", "Virtual Texturing"),
)


@dataclass(frozen=True)
class UnityProfile:
    """一个 Unity PlayerConnection 协议版本的完整描述。"""

    connection_version: int
    label: str
    verified: bool
    features: FrozenSet[str]
    profiler: Dict[str, str]
    snapshot: Dict[str, str]
    notes: str = ""
    areas: Tuple[ProfilerArea, ...] = ()

    def message(self, group: str, name: str) -> str:
        """返回 32 位十六进制消息 ID；缺失时给出明确错误。"""
        table = self.profiler if group == "profiler" else self.snapshot
        try:
            return table[name]
        except KeyError:
            raise KeyError(
                "Unity {} has no {} message '{}'".format(self.label, group, name)
            )

    def supports(self, feature: str) -> bool:
        return feature in self.features

    def area(self, name: str) -> ProfilerArea:
        for area in self.areas:
            if area.name == name:
                return area
        raise ValueError(
            "Unity {} has no profiler area '{}'".format(self.label, name)
        )

    def normalize_areas(self, areas: Optional[Iterable[str]]) -> Optional[List[str]]:
        """校验并去重 Area 名称；``None`` 表示不改变 Player 默认值。"""
        if areas is None:
            return None
        if isinstance(areas, str):
            areas = [item for item in areas.split(",") if item]
        result = []
        for name in areas:
            name = (name or "").strip()
            if not name or name in result:
                continue
            self.area(name)
            result.append(name)
        return result

    def categories_for_areas(self, areas: Iterable[str]) -> List[str]:
        """把 Area 名称翻译成需要发送给 Player 的 Category 名称（去重、保序）。"""
        wanted = set(areas)
        categories = []
        for area in self.areas:
            if area.name in wanted and area.category not in categories:
                categories.append(area.category)
        return categories

    def to_dict(self) -> dict:
        return {
            "connection_version": self.connection_version,
            "label": self.label,
            "verified": self.verified,
            "features": sorted(self.features),
            "profiler_stream": self.supports(FEATURE_PROFILER_STREAM),
            "memory_snapshot": self.supports(FEATURE_MEMORY_SNAPSHOT),
            "deep_profiling": self.supports(FEATURE_DEEP_PROFILING),
            "enabled_categories": self.supports(FEATURE_ENABLED_CATEGORIES),
            "areas": [area.to_dict() for area in self.areas],
            "notes": self.notes,
        }


# Unity 2022.3.62f2 / PlayerConnection version 1048832
# GUID 从 libunity.so 的静态初始化表提取，并在 Android IL2CPP 真机验证。
UNITY_2022_3 = UnityProfile(
    connection_version=1048832,
    label="2022.3",
    verified=True,
    features=frozenset(
        {
            FEATURE_PROFILER_STREAM,
            FEATURE_MEMORY_SNAPSHOT,
            FEATURE_DEEP_PROFILING,
            FEATURE_MEMORY_RECORD_MODE,
            FEATURE_FUNCTIONS_DATA,
            FEATURE_MARKER_FILTERING,
            FEATURE_ENABLED_CATEGORIES,
            FEATURE_OBJECT_MEMORY_PROFILE,
            FEATURE_INSTRUMENTABLE_FUNCTIONS,
            FEATURE_FUNCTION_CALLEES,
            FEATURE_AUDIO_CAPTURE_FLAGS,
        }
    ),
    profiler={
        "player_info": "94d3ae57ca4b4ed98408b827f180853d",
        "data": "c58d77184f4b4b59b3fffc6f800ae10e",
        "enable_capture": "2257466d0e0e47da89826cf04e68135c",
        "set_deep_profiler": "bf0c550cd24d498cbb28380a8467622d",
        "set_memory_record_mode": "c48d097f8fea463494b8f08b0b55d05a",
        "set_audio_capture_flags": "1e792ecb5c9f4a8381d0d03528b6ae7b",
        "object_memory_profile_snapshot": "14473694eb0a4963870aaab63efb7507",
        "object_memory_profile_data": "8584ee18ea264718873cd92b109a0761",
        "query_instrumentable_functions": "302b3998e168478eb8713b086c7693a9",
        "query_function_callees": "d8f38a5539cc4b608792c273efe6a969",
        "functions_data": "e2acb618e8c8465a901eb7b6f667cc41",
        "set_marker_filtering": "18207525e148469ea059ec2cdfb026a5",
        "set_enabled_categories": "77a4016e6b86420b8ede43de4e66a3f5",
    },
    snapshot={
        "request": "4b9386de901a4ae2bb70084edca4c7f4",
        "begin": "c626f85c1d3492369e59250f015f8129",
        "chunk": "edcadf462a69df81547ff4f30a52530c",
        "end": "80491b35c7645ea780edbe7c1d834138",
        "terminate": "249d2ef8bbc643e1808e45b25a5418b8",
    },
    notes=(
        "Verified on Unity 2022.3.62f2, Android IL2CPP player, "
        "PlayerConnection version 1048832."
    ),
    areas=PROFILER_AREAS,
)


PROFILES: Dict[int, UnityProfile] = {
    UNITY_2022_3.connection_version: UNITY_2022_3,
}


def get_profile(connection_version: Optional[int]) -> Optional[UnityProfile]:
    """按 PlayerConnection ``[Version]`` 查找 profile。"""
    if connection_version is None:
        return None
    return PROFILES.get(int(connection_version))


def supported_profiles() -> List[UnityProfile]:
    return sorted(PROFILES.values(), key=lambda item: item.connection_version)


def supported_versions() -> List[dict]:
    """给 API/UI 使用的版本清单。"""
    return [profile.to_dict() for profile in supported_profiles()]


def supported_labels() -> List[str]:
    return [profile.label for profile in supported_profiles()]


def supported_areas(connection_version: Optional[int] = None) -> List[dict]:
    """给 API/UI 使用的 Profiler Module 清单。"""
    profile = get_profile(connection_version) if connection_version else None
    if profile is None:
        profiles = supported_profiles()
        profile = profiles[-1] if profiles else None
    return [area.to_dict() for area in profile.areas] if profile else []


def default_areas(connection_version: Optional[int] = None) -> List[str]:
    return [
        item["name"]
        for item in supported_areas(connection_version)
        if item["default_enabled"]
    ]

"""Strict, browser-safe contracts for the WebConsole AG-UI projection."""

from __future__ import annotations

import json
import re
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal, Self, cast

from pydantic import Field, StringConstraints, field_validator, model_validator
from pydantic.alias_generators import to_camel

from glodex.agent.contracts import AgentDemoResponse, Platform
from glodex.agent.presentation import agent_display_summary
from glodex.api.contracts import ApiDTO
from glodex.api.durable_contracts import DurableRunStateDTO
from glodex.contracts import MAX_RESULT_EVIDENCE_IDS, EvidenceSummary, Identifier

_MAX_BODY_BYTES = 65_536
_MAX_STATE_BYTES = 65_536

AgUiMessageText = Annotated[str, StringConstraints(min_length=1, max_length=2_000)]
WebConsoleSummaryText = Annotated[str, StringConstraints(min_length=1, max_length=280)]
WebConsoleEvidenceText = Annotated[str, StringConstraints(min_length=1, max_length=256)]
AgUiName = Annotated[str, StringConstraints(min_length=1, max_length=128)]
AgUiCode = Identifier
SourceCursor = Annotated[
    str,
    StringConstraints(
        min_length=3,
        max_length=160,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/-]*:[1-9][0-9]*$",
    ),
]


class WebConsoleDTO(ApiDTO):
    """Immutable WebConsole DTO serialized with the AG-UI camelCase wire convention."""

    model_config = ApiDTO.model_config | {"alias_generator": to_camel, "populate_by_name": True}


class AgUiUserMessage(WebConsoleDTO):
    """The only client message accepted by the bounded shopping console."""

    id: Identifier
    role: Literal["user"]
    content: AgUiMessageText

    @field_validator("content", mode="before")
    @classmethod
    def trim_content(cls, value: object) -> object:
        if isinstance(value, str):
            return value.strip()
        return value


class AgUiForwardedProps(WebConsoleDTO):
    """The only AG-UI forwarded values mapped into an existing SearchRequest."""

    locale: Literal["zh-CN"] = "zh-CN"
    display_currency: Literal["CNY"] = "CNY"
    top_k: Annotated[int, Field(strict=True, ge=1, le=3)] = 3
    snapshot_version: Literal["synthetic-interview-commerce-v1"] = "synthetic-interview-commerce-v1"


class AgUiRunInput(WebConsoleDTO):
    """A deliberately narrow, standard-shaped AG-UI RunAgentInput subset."""

    thread_id: Identifier
    run_id: Identifier
    state: dict[str, object]
    messages: Annotated[tuple[AgUiUserMessage, ...], Field(min_length=1, max_length=1)]
    tools: tuple[object, ...]
    context: tuple[object, ...]
    forwarded_props: AgUiForwardedProps

    @field_validator("messages", "tools", "context", mode="before")
    @classmethod
    def json_arrays_are_normalized_once(cls, value: object) -> object:
        """HTTP JSON arrays are the canonical wire form; no other coercion is allowed."""

        return tuple(value) if type(value) is list else value

    @model_validator(mode="after")
    def input_is_bounded_and_browser_safe(self) -> Self:
        if self.state or self.tools or self.context:
            raise ValueError("state, tools, and context must be empty")
        serialized = self.model_dump_json(by_alias=True, exclude_none=False).encode("utf-8")
        if len(serialized) > _MAX_BODY_BYTES:
            raise ValueError("AG-UI request exceeds the body limit")
        return self

    def search_request_payload(self) -> dict[str, object]:
        """Return only the pre-existing SearchRequest fields, never browser metadata."""

        props = self.forwarded_props
        return {
            "query": self.messages[0].content,
            "locale": props.locale,
            "display_currency": props.display_currency,
            "top_k": props.top_k,
            "snapshot_version": props.snapshot_version,
        }


class WebConsoleWsStart(WebConsoleDTO):
    """Start exactly one durable run on an otherwise idle socket."""

    type: Literal["START"]
    input: AgUiRunInput


class WebConsoleWsAttach(WebConsoleDTO):
    """Attach an idle socket to one owner-scoped durable run."""

    type: Literal["ATTACH"]
    run_id: Identifier
    after_cursor: SourceCursor | None = None


class WebConsoleWsCancel(WebConsoleDTO):
    """Cancel only the run currently relayed by this socket."""

    type: Literal["CANCEL"]
    run_id: Identifier


class WebConsoleWsClose(WebConsoleDTO):
    """Close the current relay and socket without changing durable truth."""

    type: Literal["CLOSE"]


WebConsoleWsCommand = Annotated[
    WebConsoleWsStart | WebConsoleWsAttach | WebConsoleWsCancel | WebConsoleWsClose,
    Field(discriminator="type"),
]


class WebConsoleStageState(StrEnum):
    """Small lifecycle set for an independently renderable stage."""

    RUNNING = "RUNNING"
    FINISHED = "FINISHED"


class WebConsoleTracePhase(StrEnum):
    """Public research-loop phase; never a model chain-of-thought token."""

    THINK = "THINK"
    ACT = "ACT"
    OBSERVE = "OBSERVE"
    REFLECT = "REFLECT"


class WebConsoleTraceBullet(WebConsoleDTO):
    """One source-labelled fact in a browser-visible research step."""

    label: Annotated[str, StringConstraints(min_length=1, max_length=48)]
    value: Annotated[str, StringConstraints(min_length=1, max_length=192)]
    source: Literal["USER_INPUT", "TRUSTED_TOOL", "VERIFIED_STATE"]


class WebConsoleTraceStep(WebConsoleDTO):
    """Backend-authored progress copy derived from trusted runtime facts."""

    id: Identifier
    owner_run_id: Identifier
    phase: WebConsoleTracePhase
    title: Annotated[str, StringConstraints(min_length=1, max_length=96)]
    detail: Annotated[str, StringConstraints(min_length=1, max_length=256)]
    state: WebConsoleStageState
    bullets: Annotated[tuple[WebConsoleTraceBullet, ...], Field(max_length=6)] = ()
    tool_name: Identifier | None = None
    safe_code: AgUiCode | None = None
    platforms: Annotated[tuple[Platform, ...], Field(max_length=8)] = ()
    candidate_count: Annotated[int, Field(strict=True, ge=0, le=50)] | None = None


class WebConsoleForkState(StrEnum):
    """Fork UI state intentionally contains no child input or context."""

    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    ABORTED = "ABORTED"


class WebConsoleRelayState(StrEnum):
    """Health of the browser relay, distinct from the durable run truth."""

    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"


class WebConsoleRelayCode(StrEnum):
    """The complete bounded WebConsole-owned failure vocabulary."""

    UPSTREAM_UNAVAILABLE = "WEB_CONSOLE_UPSTREAM_UNAVAILABLE"
    PROJECTION_INVALID = "WEB_CONSOLE_PROJECTION_INVALID"
    STREAM_INTERRUPTED = "WEB_CONSOLE_STREAM_INTERRUPTED"
    REQUEST_REJECTED = "WEB_CONSOLE_REQUEST_REJECTED"


class WebConsoleStageView(WebConsoleDTO):
    """One safe stage displayed in the browser timeline."""

    name: AgUiName
    state: WebConsoleStageState
    safe_code: AgUiCode | None = None
    tool_call_id: Identifier | None = None
    platforms: Annotated[tuple[Platform, ...], Field(max_length=8)] = ()
    candidate_count: Annotated[int, Field(strict=True, ge=0, le=50)] | None = None


class WebConsoleForkView(WebConsoleDTO):
    """A child identity/depth/status projection with no demand text."""

    child_id: Identifier
    depth: Annotated[int, Field(strict=True, ge=1, le=2)]
    state: WebConsoleForkState
    platforms: Annotated[tuple[Platform, ...], Field(min_length=1, max_length=8)]


class WebConsoleRelayView(WebConsoleDTO):
    """The only relay status visible to the browser."""

    state: WebConsoleRelayState
    safe_code: AgUiCode | None = None

    @model_validator(mode="after")
    def relay_code_matches_state(self) -> Self:
        if (self.state is WebConsoleRelayState.HEALTHY) is (self.safe_code is not None):
            raise ValueError("healthy relay has no code and degraded relay requires one")
        return self


class WebConsoleOfferView(WebConsoleDTO):
    """The selected offer facts allowed on a trusted result card."""

    source_label: Annotated[str, StringConstraints(min_length=1, max_length=64)]
    currency: Literal["CNY"]
    landed_cost: Annotated[str, StringConstraints(min_length=1, max_length=64)]


class WebConsoleNeedCoverageView(WebConsoleDTO):
    """One user requirement and whether this result has supporting evidence."""

    label: Annotated[str, StringConstraints(min_length=1, max_length=256)]
    importance: Literal["required", "preferred"]
    status: Literal["VERIFIED", "UNVERIFIED", "MISSING", "INSUFFICIENT", "CONFLICTED"]
    detail: Annotated[str, StringConstraints(min_length=1, max_length=320)]
    known_facts: Annotated[tuple[WebConsoleEvidenceText, ...], Field(max_length=8)] = ()
    missing_evidence: WebConsoleEvidenceText | None = None
    conclusion: Annotated[str, StringConstraints(min_length=1, max_length=128)]
    evidence: Annotated[tuple[WebConsoleEvidenceFactView, ...], Field(max_length=8)] = ()


class WebConsoleEvidenceFactView(WebConsoleDTO):
    """One human-readable, source-linked catalog fact used by the result."""

    evidence_id: Identifier
    field_label: Annotated[str, StringConstraints(min_length=1, max_length=64)]
    source_label: Annotated[str, StringConstraints(min_length=1, max_length=128)]
    source_url: Annotated[str, StringConstraints(min_length=8, max_length=4_096)]
    captured_at: Annotated[str, StringConstraints(min_length=1, max_length=64)]

    @field_validator("source_url")
    @classmethod
    def source_is_https(cls, value: str) -> str:
        if not value.startswith("https://"):
            raise ValueError("browser evidence source must use HTTPS")
        return value


class WebConsoleResultView(WebConsoleDTO):
    """A limited terminal product card, derived only from AgentDemoResponse."""

    product_id: Identifier
    title: Annotated[str, StringConstraints(min_length=1, max_length=256)]
    category_label: Annotated[str, StringConstraints(min_length=1, max_length=64)]
    selected_offer: WebConsoleOfferView
    need_coverage: Annotated[tuple[WebConsoleNeedCoverageView, ...], Field(max_length=32)] = ()
    evidence: Annotated[
        tuple[WebConsoleEvidenceFactView, ...],
        Field(max_length=MAX_RESULT_EVIDENCE_IDS),
    ] = ()


class WebConsoleEvidenceView(WebConsoleDTO):
    """Safe evidence metadata; it deliberately does not make an external request."""

    title: Annotated[str, StringConstraints(min_length=1, max_length=256)]
    url_domain: Annotated[str, StringConstraints(min_length=1, max_length=128)]
    published_at: str | None = None
    snippet: Annotated[str, StringConstraints(min_length=1, max_length=280)]
    source_type: Identifier


class WebConsoleToolSummaryView(WebConsoleDTO):
    """A terminal count/outcome projection without call arguments or tool output."""

    tool_name: Identifier
    # Keep this projection aligned with AgentToolSummary. A cross-platform
    # search can legitimately exceed four calls after bounded refinements.
    call_count: Annotated[int, Field(strict=True, ge=1, le=10)]
    safe_outcome: Identifier


class WebConsoleTerminalView(WebConsoleDTO):
    """Whitelisted trusted response facts used in a completed WebConsole UI state."""

    status: Literal["COMPLETED", "NO_MATCH"]
    content_kind: Literal["SHOPPING_RESULTS", "CHAT_FALLBACK"]
    summary: WebConsoleSummaryText
    results: Annotated[tuple[WebConsoleResultView, ...], Field(max_length=3)] = ()
    evidence: Annotated[tuple[WebConsoleEvidenceView, ...], Field(max_length=8)] = ()
    tool_summary: Annotated[tuple[WebConsoleToolSummaryView, ...], Field(max_length=10)] = ()

    @model_validator(mode="after")
    def display_kind_matches_terminal_shape(self) -> Self:
        if self.content_kind == "CHAT_FALLBACK":
            if self.status != "COMPLETED" or self.results or self.evidence:
                raise ValueError("chat fallback cannot contain shopping results or evidence")
        elif (self.status == "COMPLETED") is not bool(self.results):
            raise ValueError("shopping terminal status must match result presence")
        return self

    @classmethod
    def from_agent_response(cls, response: AgentDemoResponse) -> WebConsoleTerminalView:
        """Produce the only terminal business payload WebConsole permits into the browser."""

        if response.status.value not in {"COMPLETED", "NO_MATCH"} or response.answer is None:
            raise ValueError("only successful Agent terminal responses can be rendered")
        display = agent_display_summary(response)
        search_response = response.search_response
        results: tuple[WebConsoleResultView, ...] = ()
        if search_response is not None:
            results = tuple(
                WebConsoleResultView(
                    product_id=result.product_id,
                    title=result.title,
                    category_label=_category_label(result.category),
                    selected_offer=WebConsoleOfferView(
                        source_label=_source_label(result.product_id),
                        currency=_cny_currency(result.landed_cost.currency),
                        landed_cost=result.landed_cost.display,
                    ),
                    need_coverage=_need_coverage(
                        required=search_response.interpreted_request.required,
                        preferred=search_response.interpreted_request.preferred,
                        matched_requirements=result.matched_requirements,
                        unknowns=result.unknowns,
                        landed_cost=result.landed_cost.display,
                        category_label=_category_label(result.category),
                        result_reason=result.reason,
                        result_evidence=result.evidence,
                    ),
                    evidence=_catalog_evidence_facts(result.evidence),
                )
                for result in search_response.results
            )
        evidence = tuple(
            WebConsoleEvidenceView(
                title=item.title,
                url_domain=item.url_domain,
                published_at=None if item.published_at is None else item.published_at.isoformat(),
                snippet=item.snippet,
                source_type=item.source_type.value,
            )
            for item in response.web_evidence
        )
        tool_summary = tuple(
            WebConsoleToolSummaryView(
                tool_name=item.tool_name.value,
                call_count=item.call_count,
                safe_outcome=item.safe_outcome,
            )
            for item in response.tool_summary
        )
        return cls(
            status=cast(Literal["COMPLETED", "NO_MATCH"], response.status.value),
            content_kind=display.content_kind.value,
            summary=display.text,
            results=results,
            evidence=evidence,
            tool_summary=tool_summary,
        )


def _need_coverage(
    *,
    required: tuple[object, ...],
    preferred: tuple[object, ...],
    matched_requirements: tuple[str, ...],
    unknowns: tuple[str, ...],
    landed_cost: str,
    category_label: str,
    result_reason: str,
    result_evidence: tuple[EvidenceSummary, ...],
) -> tuple[WebConsoleNeedCoverageView, ...]:
    """Translate internal requirement codes into query-language evidence states."""

    values: list[WebConsoleNeedCoverageView] = []
    for criterion in required:
        kind = getattr(criterion, "kind", None)
        source_span = getattr(criterion, "source_span", None)
        label = getattr(source_span, "text", None)
        if type(kind) is not str or type(label) is not str:
            raise ValueError("required criterion projection is invalid")
        detail = _required_criterion_detail(
            kind=kind,
            value=getattr(criterion, "value", None),
            label=label,
            landed_cost=landed_cost,
            category_label=category_label,
            verified=kind in matched_requirements,
        )
        required_status: Literal["VERIFIED", "UNVERIFIED"] = (
            "VERIFIED" if kind in matched_requirements else "UNVERIFIED"
        )
        known_facts, missing_evidence, conclusion = _coverage_semantics(
            label=label,
            status=required_status,
            detail=detail,
        )
        values.append(
            WebConsoleNeedCoverageView(
                label=label,
                importance="required",
                status=required_status,
                detail=detail,
                known_facts=known_facts,
                missing_evidence=missing_evidence,
                conclusion=conclusion,
                evidence=_criterion_evidence(detail, result_evidence),
            )
        )
    for criterion in preferred:
        source_span = getattr(criterion, "source_span", None)
        label = getattr(source_span, "text", None)
        if type(label) is not str:
            raise ValueError("preferred criterion projection is invalid")
        status = (
            "INSUFFICIENT"
            if f"偏好证据不足：{label}" in unknowns  # noqa: RUF001
            else "UNVERIFIED"
            if f"未证实偏好：{label}" in unknowns  # noqa: RUF001
            else "VERIFIED"
        )
        detail = _preferred_criterion_detail(
            label=label,
            status=status,
            result_reason=result_reason,
        )
        known_facts, missing_evidence, conclusion = _coverage_semantics(
            label=label,
            status=status,
            detail=detail,
        )
        values.append(
            WebConsoleNeedCoverageView(
                label=label,
                importance="preferred",
                status=cast(
                    Literal[
                        "VERIFIED",
                        "UNVERIFIED",
                        "MISSING",
                        "INSUFFICIENT",
                        "CONFLICTED",
                    ],
                    status,
                ),
                detail=detail,
                known_facts=known_facts,
                missing_evidence=missing_evidence,
                conclusion=conclusion,
                evidence=_criterion_evidence(detail, result_evidence),
            )
        )
    return tuple(values)


def _coverage_semantics(
    *,
    label: str,
    status: str,
    detail: str,
) -> tuple[tuple[str, ...], str | None, str]:
    known_text = detail
    for prefix in ("已核验规格：", "现有规格："):  # noqa: RUF001
        if known_text.startswith(prefix):
            known_text = known_text.removeprefix(prefix)
            break
    known_text = known_text.partition("；不足以直接验证")[0]  # noqa: RUF001
    known_text = known_text.partition("；该事实不支持")[0]  # noqa: RUF001
    known_facts = tuple(
        _humanize_known_fact(part.strip())
        for part in known_text.split("；")  # noqa: RUF001
        if part.strip()
    )[:8]
    if status == "VERIFIED":
        return known_facts, None, "现有证据支持这项需求。"
    if status == "CONFLICTED":
        return known_facts, "需要消解不同来源之间的事实冲突。", "当前不能确认。"
    missing = f"仍缺少能够直接验证“{label}”的厂商明确指标或标准化实测。"
    return known_facts, missing, "当前证据不足，暂不判定满足。"  # noqa: RUF001


_EVIDENCE_FIELD_LABELS = {
    "title": "商品名称",
    "category": "商品类目",
    "entity_kind": "商品类型",
    "brand": "品牌",
    "model": "型号",
    "model_key": "型号标识",
    "release_date": "发布时间",
    "processor": "处理器",
    "gpu": "显卡",
    "ram": "内存",
    "storage": "存储",
    "screen": "屏幕",
    "screen_size": "屏幕尺寸",
    "resolution": "分辨率",
    "panel_type": "面板",
    "refresh_rate": "刷新率",
    "response_time": "响应时间",
    "brightness": "亮度",
    "color_gamut": "色域",
    "hdr": "HDR",
    "adaptive_sync": "可变刷新率",
    "battery_capacity": "电池容量",
    "battery_life": "续航数据",
    "charging_power": "充电功率",
    "front_camera": "前置摄像头",
    "rear_cameras": "后置摄像头",
    "video_recording": "视频录制",
    "water_resistance": "防尘防水",
    "weight": "重量",
    "connectivity": "连接与接口",
    "layout": "键盘配列",
    "switch_type": "轴体",
    "connection": "连接方式",
    "polling_rate": "回报率",
    "actuation_force": "触发压力",
    "actuation_point": "触发行程",
    "rapid_trigger": "快速触发",
    "keycap_material": "键帽",
    "sensor": "传感器",
    "dpi": "DPI",
    "buttons": "按键数量",
    "form_factor": "机身形态",
    "power_supply": "电源",
    "socket": "处理器接口",
    "cores_threads": "核心与线程",
    "base_clock": "基础频率",
    "boost_clock": "最高频率",
    "tdp": "功耗",
    "integrated_graphics": "核芯显卡",
    "architecture": "架构",
    "vram": "显存",
    "memory_interface": "显存位宽",
    "power": "功率",
    "outputs": "视频输出",
    "capacity": "容量",
    "kit_configuration": "套装规格",
    "memory_type": "内存类型",
    "speed": "速度",
    "latency": "时序",
    "voltage": "电压",
    "interface": "接口",
    "sequential_read": "顺序读取",
    "sequential_write": "顺序写入",
    "endurance": "写入寿命",
    "warranty": "保修",
    "audio": "音响系统",
    "system": "操作系统",
    "compatibility": "兼容平台",
    "feedback": "震动与反馈",
    "driver": "发声单元",
    "codec": "音频编码",
    "noise_cancelling": "降噪",
    "microphone": "麦克风",
    "channels": "声道",
    "frequency_response": "频率响应",
    "waterproof": "防水等级",
    "transducer": "换能类型",
    "polar_pattern": "指向性",
    "sample_rate": "采样率",
    "bit_depth": "位深",
    "monitoring": "监听",
    "mount": "镜头卡口",
    "autofocus": "自动对焦",
    "stabilization": "防抖",
    "video": "视频规格",
    "viewfinder": "取景器",
    "display": "屏幕",
    "sensors": "健康传感器",
    "positioning": "定位",
    "memory": "运行内存",
    "dimensions": "尺寸",
    "wifi_standard": "无线标准",
    "bands": "频段",
    "max_rate": "理论速率",
    "ports": "网络接口",
    "mesh_support": "Mesh组网",
    "security": "网络安全",
    "storage_type": "存储类型",
    "encryption": "加密",
    "technology": "打印技术",
    "color": "彩色能力",
    "functions": "功能",
    "print_speed": "打印速度",
    "duplex": "双面打印",
    "paper": "纸张规格",
    "drive_bays": "硬盘位",
    "network_ports": "网络接口",
    "max_capacity": "最大容量",
    "filesystem": "文件系统",
    "features": "主要功能",
    "storage_features": "存储功能",
    "optical_drive": "光驱与实体介质",
    "projection": "投影技术",
    "light_source": "光源",
    "throw_ratio": "投射方式",
    "frame_rate": "视频帧率",
    "field_of_view": "视野范围",
    "privacy_shutter": "隐私遮罩",
    "front_light": "阅读前光",
    "stylus": "手写笔",
    "health_features": "健康功能",
    "chipset": "芯片组",
    "memory_slots": "内存插槽",
    "expansion": "扩展插槽",
    "storage_interfaces": "存储接口",
    "network": "网络配置",
    "power_design": "主板供电",
    "spindle_speed": "硬盘转速",
    "cache": "缓存",
    "recording_technology": "记录技术",
    "workload": "适用负载",
    "wattage": "额定功率",
    "atx_standard": "电源标准",
    "efficiency": "转换效率",
    "modularity": "模组方式",
    "gpu_connector": "显卡供电接口",
    "fan": "电源风扇",
    "protections": "电气保护",
    "cooler_type": "散热类型",
    "radiator_size": "冷排尺寸",
    "socket_support": "支持插槽",
    "fan_size": "风扇尺寸",
    "height": "散热器高度",
    "tdp_class": "散热能力",
    "noise_control": "噪声控制",
    "noise": "运行噪声",
    "case_size": "机箱尺寸",
    "motherboard_support": "主板兼容",
    "gpu_clearance": "显卡限长",
    "cooler_clearance": "散热器限高",
    "radiator_support": "冷排支持",
    "front_io": "前置接口",
    "airflow": "机箱风道",
    "focal_length": "焦距",
    "max_aperture": "最大光圈",
    "format": "适用画幅",
    "focus_motor": "对焦马达",
    "filter_size": "滤镜口径",
    "weather_sealing": "防护设计",
    "ethernet": "有线网口",
    "coverage": "覆盖范围",
    "backhaul": "回程方式",
    "mesh_nodes": "Mesh节点",
    "adapter_type": "网卡类型",
    "host_interface": "主机接口",
    "bluetooth": "蓝牙版本",
    "antenna": "天线",
    "read_speed": "读取速度",
    "write_speed": "写入速度",
    "connector": "连接端口",
    "card_format": "存储卡格式",
    "speed_class": "速度等级",
    "durability": "耐用性",
    "configuration": "配置版本",
    "configuration_detail": "配置说明",
    "product_family": "产品系列",
    "market_segment": "产品定位",
    "device_type": "设备类型",
    "platform": "平台类型",
    "display_class": "显示类型",
    "component_type": "组件类型",
    "usage": "适用场景",
    "package_scope": "产品形态",
    "graphics_family": "图形产品系列",
    "camera_family": "相机系列",
    "connection_class": "连接类型",
    "display_family": "显示器系列",
    "rated_capacity": "匹数",
    "energy_efficiency": "能效等级",
    "cooling_capacity": "制冷量",
    "applicable_area": "适用面积",
    "fresh_air": "新风功能",
    "cooling_type": "制冷方式",
    "compartments": "储藏分区",
    "odor_control": "净味除菌",
    "wash_capacity": "洗涤容量",
    "drying": "烘干方式",
    "motor": "电机",
    "hygiene": "除菌功能",
    "suction_power": "吸力",
    "navigation": "导航方式",
    "obstacle_avoidance": "避障能力",
    "base_station": "基站功能",
    "mopping": "拖地功能",
    "threshold": "越障能力",
    "filtration": "过滤系统",
    "brush_heads": "刷头配置",
    "dust_detection": "灰尘感应",
    "cadr": "颗粒物CADR",
    "filter": "滤芯系统",
    "formaldehyde": "甲醛净化",
    "flow_rate": "净水流量",
    "membrane": "滤膜",
    "wastewater_ratio": "净废水比",
    "filter_life": "滤芯寿命",
    "installation": "安装方式",
    "heating": "加热方式",
    "inner_pot": "内胆",
    "pressure": "烹饪压力",
    "reservation": "预约功能",
    "servings": "适用人数",
    "machine_type": "咖啡机类型",
    "pump_pressure": "泵压",
    "grinder": "研磨系统",
    "milk_system": "奶泡系统",
    "cleaning": "清洁维护",
    "beverages": "饮品类型",
    "vibration_frequency": "振动频率",
    "cleaning_modes": "清洁模式",
    "pressure_sensor": "压力感应",
    "brush_head_cost": "刷头耗材",
    "place_settings": "餐具套数",
    "wash_programs": "洗涤程序",
    "water_consumption": "单次耗水量",
    "heater_type": "热水器类型",
    "temperature_control": "温度控制",
    "safety": "安全保护",
    "microwave_power": "微波功率",
    "cooking_modes": "烹饪模式",
    "inverter": "变频方式",
    "temperature_range": "温度范围",
    "heating_power": "加热功率",
    "heating_system": "加热系统",
    "window": "可视设计",
    "rated_power": "额定功率",
    "burners": "炉头数量",
    "power_levels": "火力档位",
    "cookware": "适用锅具",
    "inner_material": "内胆材质",
    "keep_warm": "保温能力",
    "motor_speed": "电机转速",
    "air_speed": "风速",
    "ions": "护发离子",
    "fan_type": "风扇类型",
    "air_distance": "送风距离",
    "oscillation": "摇头范围",
    "smart_control": "智能控制",
    "humidification_type": "加湿方式",
    "humidification_rate": "加湿量",
    "tank_capacity": "水箱容量",
    "dehumidification_capacity": "日除湿量",
    "humidity_control": "湿度控制",
    "drying_mode": "干衣模式",
    "steam_modes": "蒸汽模式",
    "water_tank": "水箱配置",
    "static_pressure": "最大静压",
    "hood_style": "烟机形态",
    "heat_load": "热负荷",
    "thermal_efficiency": "热效率",
    "ignition": "点火方式",
    "pan_support": "锅架",
    "programs": "预设程序",
    "motor_power": "电机功率",
    "juicing_type": "榨汁方式",
    "feed_chute": "投料口",
    "juice_yield": "出汁表现",
    "rotation_speed": "工作转速",
    "steam_output": "蒸汽量",
    "steam_pressure": "蒸汽压力",
    "heat_up_time": "预热时间",
    "ironing_modes": "熨烫方式",
    "shaving_system": "剃须系统",
    "blade_count": "刀头数量",
    "wet_dry": "干湿双剃",
    "amplitude": "冲击振幅",
    "stall_force": "最大推力",
    "speed_levels": "力度档位",
    "percussions": "冲击频率",
    "roller_system": "滚刷系统",
    "self_cleaning": "自清洁方式",
    "edge_cleaning": "贴边清洁",
    "tapping_frequency": "拍打频率",
    "uv_sterilization": "紫外线除菌",
    "hot_air": "热风功能",
    "dust_cup": "尘杯设计",
    "unlock_methods": "解锁方式",
    "biometrics": "生物识别",
    "lock_cylinder": "锁芯等级",
    "night_vision": "夜视能力",
    "tracking": "识别追踪",
    "privacy": "隐私保护",
    "image_sensor": "图像传感器",
    "parking_monitoring": "停车监控",
    "gps": "卫星定位",
    "camera": "相机系统",
    "flight_time": "飞行时间",
    "transmission_range": "图传距离",
    "gimbal": "云台",
    "output_power": "输出功率",
    "fast_charge_protocols": "快充协议",
    "airline_compliance": "航空携带",
    "gan": "氮化镓",
    "power_distribution": "功率分配",
    "foldable_plug": "插脚设计",
    "port_count": "端口数量",
    "display_output": "显示输出",
    "power_delivery": "主机供电",
    "storage_expansion": "存储扩展",
    "measurement_metrics": "身体指标",
    "electrodes": "电极方案",
    "weight_accuracy": "称重精度",
    "user_profiles": "用户识别",
    "heart_rate": "心率测量",
    "pressure_range": "水压范围",
    "pressure_modes": "水压档位",
    "nozzles": "喷嘴配置",
    "cutting_length": "修剪长度",
    "blade_material": "刀头材质",
    "speed_modes": "速度档位",
    "measurement_site": "测量部位",
    "cuff_range": "袖带范围",
    "accuracy": "测量精度",
    "arrhythmia_detection": "心律不齐提示",
    "memory_users": "用户记忆",
    "measurement_method": "测量方式",
    "measurement_time": "测量时间",
    "fever_alert": "发热提醒",
    "memory_records": "历史记录",
    "age_mode": "年龄模式",
    "illuminance": "照度等级",
    "color_rendering": "显色指数",
    "color_temperature": "色温范围",
    "dimming": "调光方式",
    "flicker_control": "频闪控制",
    "soleplate": "熨烫底板",
    "anti_scale": "防垢设计",
    "dispenser": "自动投料",
    "crust_control": "烤色调节",
    "slots": "烘烤槽位",
    "browning_levels": "烘烤档位",
    "slot_width": "槽口宽度",
    "crumb_tray": "集屑盘",
    "lift": "取物设计",
    "bowl_capacity": "料理杯容量",
    "attachments": "附件配置",
    "heating_method": "加热方式",
    "containers": "分盒配置",
    "sealing": "密封设计",
}


def _humanize_known_fact(value: str) -> str:
    """Translate an internal attribute name without changing its sourced value."""

    field_name, separator, fact_value = value.partition("：")  # noqa: RUF001
    if not separator:
        return value
    label = _EVIDENCE_FIELD_LABELS.get(field_name, field_name)
    return f"{label}：{fact_value}"  # noqa: RUF001


def _catalog_evidence_facts(
    result_evidence: tuple[EvidenceSummary, ...],
) -> tuple[WebConsoleEvidenceFactView, ...]:
    facts: list[WebConsoleEvidenceFactView] = []
    for item in result_evidence:
        source_uri = item.source_uri
        field_path = item.field_path
        if not source_uri.startswith("https://"):
            continue
        field_name = field_path.rpartition(".")[2]
        facts.append(
            WebConsoleEvidenceFactView(
                evidence_id=item.evidence_id,
                field_label=_EVIDENCE_FIELD_LABELS.get(field_name, field_name),
                source_label=_evidence_source_label(item.provider_id),
                source_url=source_uri,
                captured_at=item.captured_at,
            )
        )
    return tuple(facts)


def _evidence_source_label(provider_id: object) -> str:
    if type(provider_id) is not str:
        raise ValueError("catalog evidence provider is invalid")
    source_kinds = {
        "manufacturer-spec-": "厂商官方规格",
        "manufacturer-price-": "厂商官方标价",
        "public-product-spec-": "公开商品规格",
        "public-retailer-price-": "公开零售挂牌价",
        "public-price-report-": "公开价格报道",
        "catalog-reference-price-": "商品参考价",
    }
    for prefix, label in source_kinds.items():
        if provider_id.startswith(prefix) and len(provider_id) > len(prefix):
            return label
    raise ValueError("catalog evidence provider namespace is invalid")


def _criterion_evidence(
    detail: str,
    result_evidence: tuple[EvidenceSummary, ...],
) -> tuple[WebConsoleEvidenceFactView, ...]:
    facts_by_id = {fact.evidence_id: fact for fact in _catalog_evidence_facts(result_evidence)}
    matched: list[WebConsoleEvidenceFactView] = []
    for item in result_evidence:
        evidence_id = item.evidence_id
        field_path = item.field_path
        fact = facts_by_id.get(evidence_id)
        if fact is None:
            continue
        field_name = field_path.rpartition(".")[2]
        if field_name in detail or fact.field_label in detail:
            matched.append(fact)
    return tuple(matched[:8])


def _required_criterion_detail(
    *,
    kind: str,
    value: object,
    label: str,
    landed_cost: str,
    category_label: str,
    verified: bool,
) -> str:
    if not verified:
        return "当前结果没有足够事实证明该必要条件。"
    if kind == "budget_max":
        return _budget_criterion_detail(value=value, label=label, landed_cost=landed_cost)
    if kind == "target_category":
        return f"商品类目为{category_label}，与目标类目一致。"  # noqa: RUF001
    if kind == "stock_required":
        return "所选报价已通过库存状态校验。"
    if kind == "exclusion":
        return f"商品已通过“{label}”排除条件检查。"
    return "该必要条件已通过后端硬性规则校验。"


def _budget_criterion_detail(*, value: object, label: str, landed_cost: str) -> str:
    if type(value) is not str:
        return f"到手价 {landed_cost} CNY，已通过“{label}”预算校验。"  # noqa: RUF001
    summary, separator, currency = value.rpartition(" ")
    if not separator or not currency.isalpha() or len(currency) != 3:
        summary = value
        currency = "CNY"
    fields: dict[str, str] = {}
    for part in summary.split(";"):
        key, separator, field_value = part.partition("=")
        if separator and key and field_value:
            fields[key] = field_value
    bounds = fields.get("bounds")
    if currency != "CNY" or not bounds:
        return f"到手价 {landed_cost} CNY，已完成币种换算并通过“{label}”预算校验。"  # noqa: RUF001
    lower, separator, upper = bounds.partition("..")
    if separator and lower and upper:
        return f"到手价 {landed_cost} CNY，位于允许区间 {lower}-{upper} CNY 内。"  # noqa: RUF001
    return f"到手价 {landed_cost} CNY，不高于预算上限 {bounds} CNY。"  # noqa: RUF001


def _preferred_criterion_detail(
    *,
    label: str,
    status: str,
    result_reason: str,
) -> str:
    reason_prefix = {
        "VERIFIED": "匹配偏好",
        "INSUFFICIENT": "偏好证据不足",
        "UNVERIFIED": "偏好证据不足",
    }.get(status)
    if reason_prefix is not None:
        marker = re.compile(
            rf"(?:^|; ){re.escape(reason_prefix)}: {re.escape(label)} "
            rf"\((.*?)\)(?=; (?:匹配偏好|偏好证据不足):|$)"
        )
        matched = marker.search(result_reason)
        if matched is not None and (detail := " ".join(matched.group(1).split())):
            return _humanize_known_detail(detail)[:320]
    if status == "VERIFIED":
        return "商品属性中存在与该偏好匹配的来源证据。"
    if status == "INSUFFICIENT":
        return "已有相关商品事实，但不足以证明这项偏好。"  # noqa: RUF001
    if status == "CONFLICTED":
        return "不同来源的商品事实相互冲突，暂时不能确认。"  # noqa: RUF001
    return "当前商品数据没有足够事实判断这项偏好。"


def _humanize_known_detail(detail: str) -> str:
    humanized = detail
    for field_name, label in _EVIDENCE_FIELD_LABELS.items():
        humanized = re.sub(
            rf"(?<![A-Za-z0-9_]){re.escape(field_name)}(?=：)",  # noqa: RUF001
            label,
            humanized,
        )
    return humanized


@lru_cache(maxsize=1)
def _taxonomy_category_labels() -> dict[str, str]:
    path = Path(__file__).resolve().parents[3] / "data/digital-interview-v1/category-taxonomy.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    groups = payload.get("groups") if type(payload) is dict else None
    if type(groups) is not list:
        raise ValueError("digital category taxonomy is invalid")
    labels: dict[str, str] = {}
    for group in groups:
        categories = group.get("categories") if type(group) is dict else None
        if type(categories) is not list:
            raise ValueError("digital category taxonomy group is invalid")
        for item in categories:
            category_id = item.get("id") if type(item) is dict else None
            label = item.get("label") if type(item) is dict else None
            if type(category_id) is not str or type(label) is not str or not label:
                raise ValueError("digital category taxonomy entry is invalid")
            labels[category_id] = label
    if not labels:
        raise ValueError("digital category taxonomy is empty")
    return labels


def _category_label(category: str) -> str:
    aliases = {
        "headphone": "headphones",
        "laptop-accessory": "laptop",
        "laptop-decoration": "laptop",
        "laptop-part": "laptop",
    }
    canonical = aliases.get(category, category)
    try:
        label = _taxonomy_category_labels()[canonical]
    except KeyError as error:
        raise ValueError("result category is absent from the taxonomy") from error
    suffixes = {
        "laptop-accessory": "配件",
        "laptop-decoration": "装饰",
        "laptop-part": "零部件",
    }
    return label + suffixes.get(category, "")


def _source_label(product_id: str) -> str:
    platform = product_id.partition(".")[0]
    labels = {
        "amazon": "Amazon",
        "shopee": "Shopee",
        "aliexpress": "AliExpress",
        "ebay": "eBay",
        "alibaba": "Alibaba",
        "walmart": "Walmart",
        "shein": "SHEIN",
    }
    return labels.get(platform, "商品来源")


def _cny_currency(currency: str) -> Literal["CNY"]:
    if currency != "CNY":
        raise ValueError("WebConsole only renders CNY prices")
    return "CNY"


class GlodexWebConsoleState(WebConsoleDTO):
    """Bounded state snapshot rendered by the Vue console."""

    schema_version: Literal["glodex.web-console.ui-state.v6"] = "glodex.web-console.ui-state.v6"
    thread_id: Identifier
    run_id: Identifier
    state: DurableRunStateDTO
    source_cursor: SourceCursor | None = None
    stages: Annotated[tuple[WebConsoleStageView, ...], Field(max_length=64)] = ()
    trace: Annotated[tuple[WebConsoleTraceStep, ...], Field(max_length=48)] = ()
    forks: Annotated[tuple[WebConsoleForkView, ...], Field(max_length=20)] = ()
    terminal: WebConsoleTerminalView | None = None
    relay: WebConsoleRelayView = WebConsoleRelayView(state=WebConsoleRelayState.HEALTHY)

    @model_validator(mode="after")
    def state_is_bounded_and_terminal_consistent(self) -> Self:
        terminal_states = {DurableRunStateDTO.COMPLETED, DurableRunStateDTO.NO_MATCH}
        if (self.state in terminal_states) is not (self.terminal is not None):
            raise ValueError("business terminal state requires exactly one terminal view")
        serialized = self.model_dump_json(by_alias=True, exclude_none=True).encode("utf-8")
        if len(serialized) > _MAX_STATE_BYTES:
            raise ValueError("WebConsole state snapshot exceeds the size limit")
        return self


class AgUiEvent(WebConsoleDTO):
    """One serializable event in the WebConsole frozen AG-UI subset."""

    type: AgUiName
    timestamp: Annotated[int, Field(strict=True, ge=0)]


class AgUiRunStarted(AgUiEvent):
    type: Literal["RUN_STARTED"] = "RUN_STARTED"
    thread_id: Identifier
    run_id: Identifier


class AgUiRunFinished(AgUiEvent):
    type: Literal["RUN_FINISHED"] = "RUN_FINISHED"
    thread_id: Identifier
    run_id: Identifier


class AgUiRunError(AgUiEvent):
    type: Literal["RUN_ERROR"] = "RUN_ERROR"
    message: Literal["Run could not be completed."] = "Run could not be completed."
    code: AgUiCode


class AgUiStepStarted(AgUiEvent):
    type: Literal["STEP_STARTED"] = "STEP_STARTED"
    step_name: AgUiName


class AgUiStepFinished(AgUiEvent):
    type: Literal["STEP_FINISHED"] = "STEP_FINISHED"
    step_name: AgUiName


class AgUiToolCallStart(AgUiEvent):
    type: Literal["TOOL_CALL_START"] = "TOOL_CALL_START"
    tool_call_id: Identifier
    tool_call_name: AgUiName


class AgUiToolCallEnd(AgUiEvent):
    type: Literal["TOOL_CALL_END"] = "TOOL_CALL_END"
    tool_call_id: Identifier


class AgUiStateSnapshot(AgUiEvent):
    type: Literal["STATE_SNAPSHOT"] = "STATE_SNAPSHOT"
    snapshot: GlodexWebConsoleState


class AgUiCustom(AgUiEvent):
    type: Literal["CUSTOM"] = "CUSTOM"
    name: Literal["glodex.web-console.run", "glodex.web-console.relay"]
    value: dict[str, object]


class AgUiTextMessageStart(AgUiEvent):
    type: Literal["TEXT_MESSAGE_START"] = "TEXT_MESSAGE_START"
    message_id: Identifier
    role: Literal["assistant"] = "assistant"


class AgUiTextMessageContent(AgUiEvent):
    type: Literal["TEXT_MESSAGE_CONTENT"] = "TEXT_MESSAGE_CONTENT"
    message_id: Identifier
    delta: AgUiMessageText


class AgUiTextMessageEnd(AgUiEvent):
    type: Literal["TEXT_MESSAGE_END"] = "TEXT_MESSAGE_END"
    message_id: Identifier


AgUiPublicEvent = (
    AgUiRunStarted
    | AgUiRunFinished
    | AgUiRunError
    | AgUiStepStarted
    | AgUiStepFinished
    | AgUiToolCallStart
    | AgUiToolCallEnd
    | AgUiStateSnapshot
    | AgUiCustom
    | AgUiTextMessageStart
    | AgUiTextMessageContent
    | AgUiTextMessageEnd
)


class WebConsoleWsReady(WebConsoleDTO):
    """Safe snapshot sent after START or owner-scoped ATTACH succeeds."""

    type: Literal["READY"] = "READY"
    state: GlodexWebConsoleState


class WebConsoleWsEvent(WebConsoleDTO):
    """One AG-UI projection event with a replay-safe group position."""

    type: Literal["EVENT"] = "EVENT"
    event: AgUiPublicEvent
    source_cursor: SourceCursor
    projection_ordinal: Annotated[int, Field(strict=True, ge=0, le=63)]
    projection_count: Annotated[int, Field(strict=True, ge=1, le=64)]

    @model_validator(mode="after")
    def projection_position_is_valid(self) -> Self:
        if self.projection_ordinal >= self.projection_count:
            raise ValueError("WebSocket projection position is invalid")
        return self


class WebConsoleWsError(WebConsoleDTO):
    """Stable browser-safe relay failure without raw exception text."""

    type: Literal["ERROR"] = "ERROR"
    code: WebConsoleRelayCode


class WebConsoleWsClosed(WebConsoleDTO):
    """Terminal transport frame; durable truth remains in the state stream."""

    type: Literal["CLOSED"] = "CLOSED"
    code: AgUiCode | None = None


WebConsoleWsServerFrame = (
    WebConsoleWsReady | WebConsoleWsEvent | WebConsoleWsError | WebConsoleWsClosed
)


def encode_agui_event(event: AgUiPublicEvent) -> str:
    """Encode exactly one browser-safe AG-UI event as canonical JSON."""

    return json.dumps(
        event.model_dump(mode="json", by_alias=True, exclude_none=True),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


__all__ = [
    "AgUiCustom",
    "AgUiEvent",
    "AgUiForwardedProps",
    "AgUiPublicEvent",
    "AgUiRunError",
    "AgUiRunFinished",
    "AgUiRunInput",
    "AgUiRunStarted",
    "AgUiStateSnapshot",
    "AgUiStepFinished",
    "AgUiStepStarted",
    "AgUiTextMessageContent",
    "AgUiTextMessageEnd",
    "AgUiTextMessageStart",
    "AgUiToolCallEnd",
    "AgUiToolCallStart",
    "GlodexWebConsoleState",
    "WebConsoleEvidenceView",
    "WebConsoleForkState",
    "WebConsoleForkView",
    "WebConsoleRelayCode",
    "WebConsoleRelayState",
    "WebConsoleRelayView",
    "WebConsoleResultView",
    "WebConsoleStageState",
    "WebConsoleStageView",
    "WebConsoleTerminalView",
    "WebConsoleTraceBullet",
    "WebConsoleTracePhase",
    "WebConsoleTraceStep",
    "WebConsoleWsAttach",
    "WebConsoleWsCancel",
    "WebConsoleWsClose",
    "WebConsoleWsClosed",
    "WebConsoleWsCommand",
    "WebConsoleWsError",
    "WebConsoleWsEvent",
    "WebConsoleWsReady",
    "WebConsoleWsServerFrame",
    "WebConsoleWsStart",
    "encode_agui_event",
]

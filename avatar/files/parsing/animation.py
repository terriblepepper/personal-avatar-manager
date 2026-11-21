"""Bounded declarative animation documents shared by uploads, indexing and chat."""
import io
import json
import re
from typing import Annotated, Literal

from fastapi import HTTPException
from pydantic import AfterValidator, BeforeValidator, BaseModel, ConfigDict, Field, ValidationError, field_serializer, model_validator

ANIMATION_MIME = "application/vnd.acps.animation+json"
MAX_SOURCE_BYTES = 100 * 1024 * 1024
MAX_ENVELOPE_BYTES = 6 * MAX_SOURCE_BYTES + 65536
_UNSAFE_TEXT = re.compile(
    r"[\x00-\x1f\x7f<>]|://|//[\w.-]+|(?:data|javascript|vbscript)\s*:"
    r"|\b(?:on[a-z]+|style)\s*=|\b(?:url|expression)\s*\(|\{\s*(?:--)?[a-z][\w-]*\s*:"
    r"|@(?:import|font-face|media|keyframes|supports|layer|namespace|page|charset)\b", re.IGNORECASE,
)


def is_animation_file(row):
    return row['content_type'] == ANIMATION_MIME or (
        row['content_type'] == 'text/html' and json.loads(row['animation_metadata'] or '{}').get('html_mode') in ('2d', '3d'))


def safe_text(value):
    if _UNSAFE_TEXT.search(value) or any(0xd800 <= ord(char) <= 0xdfff for char in value):
        raise ValueError("动画文字包含不支持的标记、网址或控制字符")
    value = value.strip()
    if not value:
        raise ValueError("动画文字不能为空")
    return value


class Fields(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False, serialize_by_alias=True)


# JSON numbers do not distinguish 1 from 1.0 in browser parsers.
Integer = Annotated[int, BeforeValidator(lambda value: int(value) if type(value) is float and value.is_integer() else value)]
Id = Annotated[str, Field(pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")]
Kind = Literal["device", "browser", "router", "server", "cloud", "database", "network", "agent"]
NodeKind = Literal[Kind, "person", "document", "queue", "decision", "shield"]
Template = Literal["classic", "blueprint", "timeline", "orbit"]
PacketMarker = Literal["dot", "arrow", "envelope", "pulse"]
Label = Annotated[str, Field(min_length=1, max_length=120), AfterValidator(safe_text)]
Title = Annotated[str, Field(min_length=1, max_length=160), AfterValidator(safe_text)]
Note = Annotated[str, Field(min_length=1, max_length=500), AfterValidator(safe_text)]
Description = Annotated[str, Field(min_length=1, max_length=2000), AfterValidator(safe_text)]


class Canvas(Fields):
    width: Integer = Field(ge=1, le=4096)
    height: Integer = Field(ge=1, le=4096)
    background: Literal["paper", "light", "dark"] = "paper"


class Accessibility(Fields):
    alt: Description
    reducedMotion: Literal["final-frame"] = "final-frame"


class Node(Fields):
    id: Id
    label: Label
    kind: NodeKind
    x: float = Field(ge=0, le=4096)
    y: float = Field(ge=0, le=4096)

    @field_serializer('x', 'y')
    def json_coordinate(self, value):
        return int(value) if value.is_integer() else value


class Edge(Fields):
    id: Id
    start: Id = Field(alias="from")
    to: Id
    kind: Kind


class Packet(Fields):
    start: Id = Field(alias="from")
    to: Id
    edge: Id
    label: Label
    marker: PacketMarker = "dot"


class Step(Fields):
    id: Id
    title: Title
    detail: Description
    at: Integer = Field(ge=0, le=60000)
    duration: Integer = Field(ge=1, le=60000)
    focus: list[Id] = Field(default_factory=list, max_length=1500)
    packets: list[Packet] = Field(default_factory=list, max_length=5000)
    notes: list[Note] = Field(default_factory=list, max_length=32)


class Animation(Fields):
    format: Literal["acps.animation"]
    template: Template = "classic"
    version: Integer = Field(ge=1, le=1)
    title: Title
    summary: Description
    tags: list[Label] = Field(default_factory=list, max_length=32)
    canvas: Canvas
    durationMs: Integer = Field(ge=1, le=60000)
    loop: bool = False
    accessibility: Accessibility
    nodes: list[Node] = Field(min_length=1, max_length=500)
    edges: list[Edge] = Field(max_length=1000)
    steps: list[Step] = Field(min_length=1, max_length=200)

    @model_validator(mode="after")
    def check_scene(self):
        nodes = {node.id for node in self.nodes}
        edges = {edge.id: edge for edge in self.edges}
        ids = nodes | edges.keys()
        count = len(self.nodes) + len(self.edges) + len(self.steps)
        if len(ids | {step.id for step in self.steps}) != count:
            raise ValueError("动画 ID 必须唯一")
        if count + sum(len(step.packets) for step in self.steps) > 5000:
            raise ValueError("动画绘图元素超过 5000 个")
        if any(node.x > self.canvas.width or node.y > self.canvas.height for node in self.nodes):
            raise ValueError("动画节点超出画布")
        if any(edge.start not in nodes or edge.to not in nodes for edge in self.edges):
            raise ValueError("动画边引用了未知节点")
        prior = 0
        for step in self.steps:
            if step.at < prior or step.at + step.duration > self.durationMs:
                raise ValueError("动画步骤时间无效")
            prior = step.at
            if not ids.issuperset(step.focus):
                raise ValueError("动画焦点引用了未知元素")
            for packet in step.packets:
                edge = edges.get(packet.edge)
                if not edge or {packet.start, packet.to} != {edge.start, edge.to}:
                    raise ValueError("动画数据包的端点与边不匹配")
        return self

    def metadata(self):
        return self.model_dump(include={"title", "tags", "durationMs"})

    def canonical(self):
        return json.dumps(self.model_dump(by_alias=True, exclude_defaults=True), sort_keys=True, ensure_ascii=False,
                          separators=(",", ":"), allow_nan=False).encode("utf-8")


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("动画 JSON 对象有重复字段")
        result[key] = value
    return result


# Avoid allocating millions of Python containers for a malformed 100 MiB upload.
JSON_TOKEN = re.compile(rb'[ \t\r\n]++|"(?:[^"\\]++|\\.)*+"|[][{},:]|[^][{}:," \t\r\n]++|.', re.DOTALL)


def _compact_large_source(data):
    output = io.BytesIO()
    tokens = containers = depth = 0
    for match in JSON_TOKEN.finditer(data):
        start, end = match.span()
        first = data[start]
        if first in b' \t\r\n':
            output.write(b' ')
            continue
        tokens += 1
        if first in b'[{':
            containers += 1
            depth += 1
        elif first in b']}':
            depth -= 1
        # v1 permits 200 × 1500 focus references and 5000 drawing elements.
        # An escaped UTF-16 surrogate pair uses up to 12 source bytes per character.
        if tokens > 850000 or containers > 10000 or depth > 8 or (first == ord('"') and end-start > 24002):
            raise HTTPException(422, '动画 JSON 超出 v1 字段或结构限制')
        output.write(memoryview(data)[start:end])
    return output.getvalue()


def load_animation_object(source):
    try:
        if len(source) > MAX_SOURCE_BYTES:
            raise HTTPException(413, "动画源码不能超过 100 MiB")
        if isinstance(source, str):
            if not source.isascii() and sum(len(source[i:i+65536].encode('utf-8')) for i in range(0,len(source),65536)) > MAX_SOURCE_BYTES:
                raise HTTPException(413, "动画源码不能超过 100 MiB")
            source = source.strip(" \r\n\t").encode("utf-8")
        if len(source) > 1024 ** 2:
            source = _compact_large_source(source)
        return json.loads(source[:].decode("utf-8"), object_pairs_hook=_unique_object)
    except (ValueError, UnicodeError, RecursionError):
        raise HTTPException(422, "动画包必须是有效的 UTF-8 v1 JSON，字段不能重复") from None


def validate_animation_object(document):
    try:
        scene = Animation.model_validate(document)
        if len(scene.canonical()) > MAX_SOURCE_BYTES:
            raise HTTPException(413, "规范化动画包不能超过 100 MiB")
        return scene
    except ValidationError as exc:
        error = exc.errors(include_input=False, include_url=False)[0]
        location = ".".join(str(item) for item in error["loc"])[:128]
        raise HTTPException(422, f"动画字段无效 {location}：{error['msg']}") from None
    except (ValueError, UnicodeError, RecursionError):
        raise HTTPException(422, "动画包必须是有效的 UTF-8 v1 JSON，字段不能重复") from None


def parse_animation(source):
    return validate_animation_object(load_animation_object(source))

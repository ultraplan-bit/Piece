"""知识层输入契约；服务、HTTP 与 MCP 共用，核心模式不依赖 GUI。"""

import unicodedata
from typing import Annotated, Literal
from uuid import UUID

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator


ObjectKind = Literal["concept", "entity", "topic", "synthesis", "source_summary"]
Status = Literal["active", "disputed", "outdated"]
Predicate = Literal["is_a", "part_of", "depends_on", "applies_to", "supports", "contradicts", "related_to"]
Basis = Literal["explicit", "synthesis", "inference", "user_statement"]
RecordKind = Literal["object", "relation", "evidence", "link"]
PositiveInt = Annotated[int, Field(strict=True, ge=1)]
NonNegative = Annotated[int, Field(strict=True, ge=0)]
Limit = Annotated[int, Field(strict=True, ge=1, le=100)]
MAX_EXTRACT_MATCHES = 50
Offset = Annotated[int, Field(strict=True, ge=0)]
Key = Annotated[str, Field(min_length=1, max_length=180), AfterValidator(lambda v: nonblank(v))]
Title = Annotated[str, Field(min_length=1, max_length=500), AfterValidator(lambda v: nonblank(v))]
Text = Annotated[str, Field(max_length=200000)]
ShortText = Annotated[str, Field(max_length=4000)]
Uuid = Annotated[str, AfterValidator(lambda v: str(UUID(v)))]
Ref = Annotated[str, Field(min_length=1, max_length=100), AfterValidator(lambda v: nonblank(v))]


def nonblank(value):
    value = value.strip()
    if not value:
        raise ValueError("不可为空白字符串")
    return value


def normalize_name(value):
    return unicodedata.normalize("NFKC", value).casefold().strip()


def unique_aliases(values):
    seen, result = set(), []
    for value in values:
        key = normalize_name(value)
        if key not in seen:
            seen.add(key)
            result.append(value.strip())
    return result


Aliases = Annotated[list[Title], Field(max_length=100), AfterValidator(unique_aliases)]


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Target(Input):
    """已有记录用 id，新记录用批次 ref；不按标题猜测身份。"""
    id: Uuid | None = None
    ref: Ref | None = None

    @model_validator(mode="after")
    def exactly_one(self):
        if len(self.model_fields_set) != 1 or (self.id is None and self.ref is None):
            raise ValueError("引用必须且只能提供 id 或 ref")
        return self


class ObjectCreate(Input):
    ref: Ref
    kind: ObjectKind
    title: Title
    summary: ShortText = ""
    body: Text = ""
    aliases: Aliases = Field(default_factory=list)
    status: Status = "active"


class Update(Input):
    id: Uuid
    expected_revision: PositiveInt

    @model_validator(mode="after")
    def no_explicit_null(self):
        if any(getattr(self, key) is None for key in self.model_fields_set):
            raise ValueError("更新字段不能为 null；未传字段保持原值，空字符串/数组用于清空")
        if not self.model_fields_set - {"id", "expected_revision"}:
            raise ValueError("至少提供一个待更新字段")
        return self


class ObjectUpdate(Update):
    kind: ObjectKind | None = None
    title: Title | None = None
    summary: ShortText | None = None
    body: Text | None = None
    aliases: Aliases | None = None
    status: Status | None = None


class LinkCreate(Input):
    source: Target
    target: Target


class RelationCreate(Input):
    ref: Ref
    source: Target
    predicate: Predicate
    target: Target
    description: Annotated[str, Field(min_length=1, max_length=4000), AfterValidator(nonblank)]
    qualifier: ShortText = ""
    basis: Basis
    status: Status = "active"


class RelationUpdate(Update):
    source: Target | None = None
    predicate: Predicate | None = None
    target: Target | None = None
    description: Annotated[str, Field(min_length=1, max_length=4000), AfterValidator(nonblank)] | None = None
    qualifier: ShortText | None = None
    basis: Basis | None = None
    status: Status | None = None


class EvidenceCreate(Input):
    object: Target | None = None
    relation: Target | None = None
    source_kind: Literal["piece", "external", "user"]
    stance: Literal["supports", "contradicts", "context"] = "supports"
    source_library_id: Uuid | None = None
    source_file_id: PositiveInt | None = None
    source_chunk_id: PositiveInt | None = None
    source_title: Title | None = None
    source_url: Annotated[str, Field(max_length=4000)] | None = None
    quote: Annotated[str, Field(min_length=1, max_length=100000), AfterValidator(nonblank)]
    expected_content_hash: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")] | None = None

    @model_validator(mode="after")
    def source_fields(self):
        if (self.object is None) == (self.relation is None):
            raise ValueError("证据必须且只能附属于 object 或 relation")
        if self.source_kind == "piece":
            if None in (self.source_library_id, self.source_file_id, self.source_chunk_id):
                raise ValueError("Piece 来源必须提供库 UUID、文件 ID 和卡片 ID")
            if self.source_url is not None:
                raise ValueError("Piece 来源使用库/文件/卡片定位，不接受 source_url")
        else:
            if any(value is not None for value in (self.source_library_id, self.source_file_id,
                                                   self.source_chunk_id, self.expected_content_hash)):
                raise ValueError("非 Piece 来源不能声明本地定位或正文哈希")
            if self.source_kind == "external" and (not self.source_title or not self.source_url):
                raise ValueError("外部来源需要 source_title、source_url 和 quote")
            if self.source_kind == "user" and self.source_url is not None:
                raise ValueError("用户陈述不能伪造网页来源")
        return self


class ApplyInput(Input):
    request_key: Key | None = None
    reason: Annotated[str, Field(min_length=1, max_length=2000), AfterValidator(nonblank)]
    objects: Annotated[list[ObjectCreate | ObjectUpdate], Field(max_length=20)] = Field(default_factory=list)
    links: Annotated[list[LinkCreate], Field(max_length=200)] = Field(default_factory=list)
    relations: Annotated[list[RelationCreate | RelationUpdate], Field(max_length=100)] = Field(default_factory=list)
    evidence: Annotated[list[EvidenceCreate], Field(max_length=200)] = Field(default_factory=list)
    dry_run: bool = False

    @model_validator(mode="after")
    def nonempty(self):
        if not self.dry_run and not self.request_key:
            raise ValueError("正式提交必须提供 request_key")
        if not any((self.objects, self.links, self.relations, self.evidence)):
            raise ValueError("批次不能为空")
        return self


class ListInput(Input):
    kind: ObjectKind | None = None
    status: Status | None = None
    limit: Limit = 50
    offset: Offset = 0


class SearchInput(ListInput):
    query: Annotated[str, Field(min_length=1, max_length=1000), AfterValidator(nonblank)]


class GetInput(Input):
    kind: RecordKind
    id: Uuid
    limit: Limit = 50
    offset: Offset = 0


class GraphInput(Input):
    root_id: Uuid
    depth: Literal[1, 2] = 1
    edge_types: Annotated[list[Literal["link", "relation"]], Field(min_length=1, max_length=2)] = Field(default_factory=lambda: ["link", "relation"])
    predicates: Annotated[list[Predicate], Field(max_length=7)] | None = None
    statuses: Annotated[list[Status], Field(max_length=3)] | None = None
    max_nodes: Annotated[int, Field(strict=True, ge=1, le=100)] = 100
    max_edges: Annotated[int, Field(strict=True, ge=1, le=300)] = 300


class ReferencesInput(Input):
    source_library_id: Uuid
    source_file_id: PositiveInt
    limit: Limit = 50
    offset: Offset = 0


class LintInput(Input):
    object_ids: Annotated[list[Uuid], Field(max_length=100)] | None = None
    limit: Limit = 50
    offset: Offset = 0


class HistoryInput(Input):
    kind: Literal["object", "relation"]
    id: Uuid
    limit: Limit = 50
    offset: Offset = 0


class RequestInput(Input):
    request_key: Key


class ChunkExtractInput(Input):
    """从卡片正文切出精确引文，供直接提交为知识证据；lines 与 grep 二选一。

    定位可以模糊，输出始终是正文的精确子串，仍受逐字校验约束。
    """
    chunk_id: PositiveInt
    lines: Annotated[str, Field(min_length=1, max_length=40)] | None = None
    grep: Annotated[str, Field(min_length=1, max_length=4000)] | None = None
    context: Annotated[int, Field(strict=True, ge=0, le=100)] = 0
    max_matches: Annotated[int, Field(strict=True, ge=1, le=MAX_EXTRACT_MATCHES)] = 20
    regex: bool = False

    @model_validator(mode="after")
    def one_locator(self):
        if (self.lines is None) == (self.grep is None):
            raise ValueError("lines 与 grep 必须且只能提供一个")
        if self.lines is not None and self.context:
            raise ValueError("按行号取引文时不接受 context")
        return self


class DeleteInput(Input):
    kind: RecordKind
    id: Uuid
    expected_revision: PositiveInt | None = None
    request_key: Key | None = None
    impact_token: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")] | None = None
    dry_run: bool = True
    confirmed: bool = False

    @model_validator(mode="after")
    def confirmation_fields(self):
        if self.kind in ("object", "relation") and self.expected_revision is None:
            raise ValueError("删除对象/关系必须提供 expected_revision")
        if self.kind in ("evidence", "link") and self.expected_revision is not None:
            raise ValueError("不可变证据/页面链接不接受 expected_revision")
        if not self.dry_run and (not self.request_key or not self.impact_token):
            raise ValueError("正式删除必须提供 request_key 和预览的 impact_token")
        return self

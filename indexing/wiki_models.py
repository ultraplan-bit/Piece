"""Wiki 输入契约；页面和来源属于 Markdown 文件，不是图谱实体。"""

from typing import Annotated, Literal

from pydantic import Field, model_validator

from .knowledge_models import (
    Aliases, Input, Key, Limit, Offset, PositiveInt, Ref, ShortText, SourceInput,
    Status, Target, Text, Title, Uuid, ReferencesInput, RequestInput, nonblank,
)
from pydantic import AfterValidator

PageKind = Literal["concept", "entity", "topic", "synthesis", "source_summary"]
RecordKind = Literal["page", "evidence"]
Hash = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class PageCreate(Input):
    ref: Ref
    kind: PageKind
    title: Title
    summary: ShortText = ""
    body: Text = ""
    aliases: Aliases = Field(default_factory=list)
    status: Status = "active"


class PageUpdate(Input):
    id: Uuid
    expected_revision: PositiveInt
    expected_content_hash: Hash
    kind: PageKind | None = None
    title: Title | None = None
    summary: ShortText | None = None
    body: Text | None = None
    aliases: Aliases | None = None
    status: Status | None = None

    @model_validator(mode="after")
    def no_null(self):
        if any(getattr(self, key) is None for key in self.model_fields_set):
            raise ValueError("更新字段不能为 null；未传保持原值，空字符串/数组用于清空")
        return self


class EvidenceCreate(SourceInput):
    page: Target


class ApplyInput(Input):
    request_key: Key | None = None
    reason: Annotated[str, Field(min_length=1, max_length=2000), AfterValidator(nonblank)]
    pages: Annotated[list[PageCreate | PageUpdate], Field(min_length=1, max_length=20)]
    evidence: Annotated[list[EvidenceCreate], Field(max_length=200)] = Field(default_factory=list)
    dry_run: bool = False

    @model_validator(mode="after")
    def valid_batch(self):
        if not self.dry_run and not self.request_key:
            raise ValueError("正式提交必须提供 request_key")
        refs, ids = set(), set()
        for page in self.pages:
            bucket, key = (ids, page.id) if isinstance(page, PageUpdate) else (refs, page.ref)
            if key in bucket:
                raise ValueError("同一批次不可重复页面 id/ref")
            bucket.add(key)
        for item in self.evidence:
            if (item.page.id not in ids if item.page.id else item.page.ref not in refs):
                raise ValueError("证据必须指向本批次 pages 中的页面；已有页也须提供版本和文件哈希")
        for page in self.pages:
            if isinstance(page, PageUpdate) and not page.model_fields_set - {"id", "expected_revision", "expected_content_hash"}:
                if not any(item.page.id == page.id for item in self.evidence):
                    raise ValueError("页面更新至少提供一个字段或一条证据")
        return self


class ListInput(Input):
    kind: PageKind | None = None
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


class LintInput(Input):
    page_ids: Annotated[list[Uuid], Field(max_length=100)] | None = None
    limit: Limit = 50
    offset: Offset = 0


class HistoryInput(Input):
    kind: Literal["page"] = "page"
    id: Uuid
    limit: Limit = 50
    offset: Offset = 0


class DeleteInput(Input):
    kind: RecordKind
    id: Uuid
    expected_revision: PositiveInt | None = None
    expected_content_hash: Hash
    request_key: Key | None = None
    impact_token: Hash | None = None
    dry_run: bool = True
    confirmed: bool = False

    @model_validator(mode="after")
    def confirmation_fields(self):
        if self.kind == "page" and self.expected_revision is None:
            raise ValueError("删除页面必须提供 expected_revision")
        if self.kind == "evidence" and self.expected_revision is not None:
            raise ValueError("证据删除使用所属页面哈希，不接受 expected_revision")
        if not self.dry_run and (not self.request_key or not self.impact_token):
            raise ValueError("正式删除必须提供 request_key 和预览的 impact_token")
        return self


class StoredEvidence(SourceInput):
    id: Uuid
    content_hash: Hash | None = None
    heading_path: ShortText | None = None
    page_number: PositiveInt | None = None
    created_at: Annotated[str, Field(min_length=1, max_length=80)]


class PageDocument(Input):
    format: Literal[1] = 1
    id: Uuid
    kind: PageKind
    title: Title
    summary: ShortText = ""
    body: Text = ""
    aliases: Aliases = Field(default_factory=list)
    status: Status = "active"
    revision: PositiveInt
    created_at: Annotated[str, Field(min_length=1, max_length=80)]
    updated_at: Annotated[str, Field(min_length=1, max_length=80)]
    operation_id: Uuid | None = None
    evidence: Annotated[list[StoredEvidence], Field(max_length=1000)] = Field(default_factory=list)

    @model_validator(mode="after")
    def unique_evidence(self):
        if len({item.id for item in self.evidence}) != len(self.evidence):
            raise ValueError("页面内证据 ID 必须唯一")
        for item in self.evidence:
            if item.expected_content_hash is not None:
                raise ValueError("文件证据快照不接受 expected_content_hash")
        return self

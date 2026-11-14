from datetime import date
from typing import Literal, Annotated
from pydantic import BaseModel, Field, ConfigDict, model_validator, field_validator
from .settings import MODEL_ALIAS

class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")

class Login(Strict):
    username: str = Field(max_length=100)
    password: str = Field(max_length=200)

class CaseInput(Strict):
    title: str = Field(min_length=1, max_length=160)
    position: str = Field(default="我方当事人代理律师", max_length=200)
    claims: str = Field(default="核查合同履行情况与争议诉求", max_length=2000)
    research_date: date = Field(default_factory=date.today)

class CaseSummary(CaseInput):
    id: str
    owner_id: str
    revision: int
    source_revision: int
    created_at: str
    updated_at: str
    material_count: int
    waiting_count: int
    similar_cases: dict
    work_status: Literal["needs_materials", "ready_to_research", "queued", "running", "waiting_input", "succeeded", "failed", "cancelled"] = Field(
        description="Research progress for the current case revision, not litigation status. Waiting input takes priority over running and queued work; otherwise use the newest task. With no current task, use ready_to_research when active materials exist, or needs_materials.")

class CaseDeletion(Strict):
    id: str
    status: Literal["deleted"] = "deleted"

class TextMaterial(Strict):
    name: str = Field(min_length=1, max_length=200)
    text: str = Field(min_length=1, max_length=60000)
    kind: str = Field(default="当事人提供材料", max_length=80)

class EvidenceReview(Strict):
    run_id: str = Field(min_length=1, max_length=100)
    doc_ids: list[str] = Field(min_length=1, max_length=5)

    @field_validator("doc_ids")
    @classmethod
    def no_duplicates(cls, values):
        if len(set(values)) != len(values):
            raise ValueError("每个类案只能选择一次")
        return values

class EvidenceSelection(Strict):
    similar_version: int = Field(ge=1)
    doc_ids: list[str] = Field(min_length=1, max_length=5)

    @field_validator("doc_ids")
    @classmethod
    def no_duplicates(cls, values):
        if len(set(values)) != len(values):
            raise ValueError("每个类案只能选择一次")
        return values

class ResearchSelection(EvidenceSelection):
    doc_ids: list[str] = Field(min_length=1, max_length=3)

class EvidenceReference(Strict):
    content_hash: str
    include_answers: bool = Field(default=False, description="Legacy compatibility field; evidence Word exports contain only materials, suggested evidence and verification questions.")
    save_to_materials: bool = Field(default=True, description="Save an immutable Word document in materials; false returns a DOCX download without saving.")
    timezone: str = Field(default="UTC", max_length=80)

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, value):
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError("无效时区")
        return value

class Goal(Strict):
    goal: str = Field(min_length=2, max_length=3000)
    material_ids: list[str] | None = Field(default=None, max_length=20)
    workflow: Literal["case.timeline", "evidence.gaps", "evidence.followup", "case.assess", "research.followup", "document.generate", "full"] | None = None
    document_type: Literal["client", "internal", "complaint", "opinion"] = "client"
    evidence_review: EvidenceReview | None = None
    evidence_selection: EvidenceSelection | None = None
    evidence_artifact_id: str | None = Field(default=None, min_length=1, max_length=100)
    research_artifact_id: str | None = Field(default=None, min_length=1, max_length=100)
    research_selection: ResearchSelection | None = None
    document_template_id: str | None = Field(default=None, min_length=1, max_length=100)
    document_artifact_id: str | None = Field(default=None, min_length=1, max_length=100)
    document_content_hash: str | None = Field(default=None, min_length=1, max_length=100)
    document_tasks: dict[str, Annotated[str, Field(max_length=600)]] = Field(default_factory=dict, max_length=20)

FilterKey = Literal["title", "case_number", "court", "doc_type", "case_type", "procedure",
                    "judgment_date", "published_at", "accepted_at", "case_level", "court_level",
                    "cause", "province", "city", "judgment_year", "outcome", "laws", "content"]
FilterValue = Annotated[str, Field(pattern=r"^(missing|[0-9a-f]{64})$")]

class FilterContext(Strict):
    filters: dict[FilterKey, Annotated[list[FilterValue], Field(max_length=50)]] = Field(default_factory=dict)
    province: str = Field(default="", max_length=100)
    court: str = Field(default="", max_length=100)
    cause: str = Field(default="", max_length=150)
    date_from: date | None = None
    date_to: date | None = None

    @field_validator("filters")
    @classmethod
    def unique_values(cls, value):
        return {k: list(dict.fromkeys(v)) for k, v in value.items() if v}

    @model_validator(mode="after")
    def ordered_dates(self):
        if self.date_from and self.date_to and self.date_from > self.date_to:
            raise ValueError("起始日期不得晚于结束日期")
        return self

class FacetInput(FilterContext):
    field: FilterKey | None = None
    text: str = Field(default="", max_length=200)
    offset: int = Field(default=0, ge=0, le=500000)
    limit: int = Field(default=50, ge=1, le=100)

class SearchInput(FilterContext):
    query: str = Field(min_length=2, max_length=1500)
    mode: Literal["semantic", "hybrid", "keyword", "case_number"] = "hybrid"
    limit: int = Field(default=8, ge=1, le=20)

class SimilarCaseOptions(FilterContext):
    query: str = Field(default="", max_length=1500)
    mode: Literal["semantic", "hybrid", "keyword"] = "semantic"
    limit: int = Field(default=10, ge=1, le=20)

    @field_validator("query")
    @classmethod
    def trimmed_query(cls, value):
        value = value.strip()
        if value and len(value) < 2:
            raise ValueError("检索描述至少需要两个字符，或留空使用案件资料")
        return value

class SimilarCaseInput(Strict):
    options: SimilarCaseOptions = Field(default_factory=SimilarCaseOptions)
    version: int = Field(default=0, ge=0)

class SimilarCaseRelevance(Strict):
    relevant: bool = Field(description="False marks a saved similar case as irrelevant to this case; true removes that exclusion.")
    version: int = Field(ge=1, description="Current similar-cases version, used to reject conflicting edits.")

class Selection(Strict):
    doc_ids: list[str] = Field(max_length=5)

    @field_validator("doc_ids")
    @classmethod
    def no_duplicates(cls, values):
        if len(set(values)) != len(values):
            raise ValueError("每个类案只能选择一次，最多选择 5 个类案")
        return values

class Resume(Strict):
    answer: dict

class EditArtifact(Strict):
    body: str = Field(min_length=1, max_length=50000)
    content_hash: str

class TimelineFactInput(Strict):
    id: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_-]+$")
    date_text: str = Field(default="", max_length=120)
    kind: str = Field(min_length=1, max_length=80)
    description: str = Field(min_length=1, max_length=5000)
    source_ref: str | None = Field(default=None, min_length=1, max_length=100)
    material_id: str | None = Field(default=None, min_length=1, max_length=100)

    @model_validator(mode="after")
    def material_required(self):
        if not self.material_id and not self.source_ref:
            raise ValueError("请选择材料中心中的材料")
        return self

class EditTimeline(Strict):
    content_hash: str
    facts: list[TimelineFactInput] = Field(min_length=1, max_length=500)

class Review(Strict):
    content_hash: str
    confirmed: bool

class WritingConfig(Strict):
    audience: Literal["客户", "内部律师"] = "客户"
    tone: str = Field(min_length=1, max_length=120)
    length: Literal["简洁", "标准", "详细"] = "标准"
    prompt: str = Field(min_length=1, max_length=3000)
    skill: str = Field(min_length=1, max_length=3000)
    opening: str = Field(max_length=300)
    model: Literal[MODEL_ALIAS] = MODEL_ALIAS

class PublishConfig(Strict):
    content: WritingConfig
    test_id: str
    base_version: int
    reviewed: bool

class Feedback(Strict):
    target_id: str = Field(max_length=100)
    rating: Literal["relevant", "irrelevant", "supported", "unsupported"]
    note: str = Field(default="", max_length=2000)

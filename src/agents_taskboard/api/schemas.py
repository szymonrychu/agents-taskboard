"""Request bodies of the tasks API and response models of the spec routes."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PlainSerializer,
    WithJsonSchema,
    field_validator,
    model_validator,
)

from agents_taskboard.labels import normalize_labels

Kind = Literal["idea", "task"]
Status = Literal["idea", "open", "in_progress", "done", "dropped"]
ListStatus = Literal["active", "done", "dropped", "all"]

Title = Field(min_length=1, max_length=200)
ProjectSlug = Field(default=None, pattern=r"^[a-z0-9][a-z0-9-]{0,39}$")
SpecKey = Field(default=None, pattern=r"^S-[1-9][0-9]{0,17}$")
Model = Literal["haiku", "sonnet", "opus", "fable"]
RefKind = Literal["pr", "commit", "branch", "tag", "url"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _Labelled(_Strict):
    @field_validator("labels", check_fields=False)
    @classmethod
    def _labels(cls, value: list[str] | None) -> list[str] | None:
        return None if value is None else normalize_labels(value)

    @model_validator(mode="after")
    def _one_assignee(self) -> Self:
        if getattr(self, "assignee_self", False) and getattr(self, "assignee_session", None):
            raise ValueError("assignee_self and assignee_session are mutually exclusive")
        return self


class TaskCreate(_Labelled):
    title: str = Title
    body: str = ""
    kind: Kind = "task"
    status: Status | None = None
    labels: list[str] = []
    assignee_session: str | None = None
    assignee_self: bool = False
    client_key: str | None = Field(default=None, min_length=1, max_length=200)
    project: str | None = ProjectSlug
    spec: str | None = SpecKey


class Ref(BaseModel):
    """What implements a plan step."""

    model_config = ConfigDict(extra="forbid")

    kind: RefKind
    value: str = Field(min_length=1, max_length=2000)


class TaskPatch(_Labelled):
    version: int
    title: str | None = Field(default=None, min_length=1, max_length=200)
    body: str | None = None
    status: Status | None = None
    labels: list[str] | None = None
    assignee_session: str | None = None
    assignee_self: bool = False
    project: str | None = ProjectSlug
    spec: str | None = SpecKey
    refs: list[Ref] | None = Field(default=None, max_length=20)
    model: Model | None = None


class DepsPut(_Strict):
    depends_on: list[str] = Field(max_length=200)


class CommentCreate(_Strict):
    body: str = Field(min_length=1)


TodoStatus = Literal["todo", "inprogress", "done"]
TodoText = Field(min_length=1, max_length=200, pattern=r"^[^\r\n]*$")


class TodoItemIn(_Strict):
    text: str = TodoText
    status: TodoStatus = "todo"


class TodoReplace(_Strict):
    items: list[TodoItemIn] = Field(max_length=100)


class TodoPatch(_Strict):
    text: str | None = Field(default=None, min_length=1, max_length=200, pattern=r"^[^\r\n]*$")
    status: TodoStatus | None = None

    @model_validator(mode="after")
    def _something(self) -> Self:
        if not self.model_fields_set:
            raise ValueError("no fields to change")
        if any(getattr(self, f) is None for f in self.model_fields_set):
            raise ValueError("text and status cannot be null")
        return self


class DecisionOption(_Strict):
    label: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    recommended: bool = False


class DecisionCreate(_Strict):
    question: str = Field(min_length=1, max_length=2000)
    context: str = ""
    options: list[DecisionOption] = Field(min_length=2, max_length=8)
    client_key: str | None = Field(default=None, min_length=1, max_length=200)

    @model_validator(mode="after")
    def _one_recommended(self) -> Self:
        if sum(o.recommended for o in self.options) != 1:
            raise ValueError("exactly one option must be recommended")
        return self


class DecisionAnswer(_Strict):
    choice: int | None = Field(default=None, ge=0)
    text: str | None = None
    note: str | None = None

    @model_validator(mode="after")
    def _one_answer(self) -> Self:
        if (self.choice is None) == (self.text is None or not self.text.strip()):
            raise ValueError("give exactly one of choice and a non-empty text")
        return self


class DecisionWithdraw(_Strict):
    reason: str = Field(min_length=1, max_length=2000)
    superseded_by: int | None = Field(default=None, ge=1)
    answered_elsewhere: str | None = Field(default=None, min_length=1, max_length=4000)

    @model_validator(mode="after")
    def _reason_and_one_outcome(self) -> Self:
        if not self.reason.strip():
            raise ValueError("reason must not be blank")
        if self.answered_elsewhere is not None and not self.answered_elsewhere.strip():
            raise ValueError("answered_elsewhere must not be blank")
        if (self.superseded_by is None) == (self.answered_elsewhere is None):
            raise ValueError("give exactly one of superseded_by and answered_elsewhere")
        return self


SpecStatus = Literal["draft", "review", "approved", "archived"]
SpecName = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,79}$")
Note = Field(default=None, max_length=500)
Sources = Field(default=[], max_length=20)
ClientKey = Field(default=None, min_length=1, max_length=200)


class DocumentIn(_Strict):
    path: str = Field(min_length=1, max_length=200)
    body: str
    note: str | None = Note
    sources: list[dict[str, Any]] = Sources


class SpecCreate(_Labelled):
    project: str | None = ProjectSlug
    name: str = SpecName
    title: str = Title
    labels: list[str] = []
    client_key: str | None = ClientKey
    docs: list[DocumentIn] = []


class SpecPatch(_Labelled):
    version: int
    title: str | None = Field(default=None, min_length=1, max_length=200)
    status: SpecStatus | None = None
    labels: list[str] | None = None
    project: str | None = ProjectSlug
    assignee_session: str | None = None
    assignee_self: bool = False


class DocumentPut(_Strict):
    body: str | None
    base_version: int = Field(ge=0)
    note: str | None = Note
    sources: list[dict[str, Any]] = Sources
    client_key: str | None = ClientKey


class SpecCommentCreate(_Strict):
    path: str | None = Field(default=None, min_length=1, max_length=200)
    version: int | None = Field(default=None, ge=1)
    anchor: str | None = Field(default=None, min_length=1, max_length=200)
    body: str = Field(min_length=1)
    suggestion: str | None = None
    attachments: list[dict[str, Any]] = Field(default=[], max_length=10)
    client_key: str | None = ClientKey


class SuggestionAccept(_Strict):
    version: int = Field(ge=0)


# Response models of the spec routes. They mirror `db/specs.py` exactly (no field is added,
# renamed or dropped) and forbid extras, so a drifting route fails loudly instead of
# silently widening the contract.
Timestamp = Annotated[
    datetime,
    PlainSerializer(lambda value: value.isoformat(), return_type=str, when_used="json"),
    WithJsonSchema({"type": "string", "format": "date-time"}),
]
SuggestionStatus = Literal["open", "accepted", "dismissed"]


class ActorRef(_Strict):
    kind: str
    session: str | None
    slug: str | None


class DocumentVersion(_Strict):
    """One stored version of a document, without its body. A removal has no hash or size."""

    path: str
    version: int
    sha256: str | None
    size: int | None
    note: str | None
    sources: list[dict[str, Any]]
    created_by: ActorRef | None
    created_at: Timestamp
    # `version_json` also leaks the raw columns behind `created_by`; kept so the contract is
    # exactly what the routes return.
    created_by_kind: str | None
    created_by_session: str | None
    created_by_slug: str | None


class DocumentVersionListed(DocumentVersion):
    """A version in a history listing; `removed` is true for a removal."""

    removed: bool


class DocumentBody(DocumentVersion):
    body: str


class DocumentList(_Strict):
    items: list[DocumentVersion]


class VersionPage(_Strict):
    items: list[DocumentVersionListed]
    next_cursor: int | None


class SpecDocument(_Strict):
    """A live document inside a single spec."""

    path: str
    version: int
    size: int | None
    sha256: str | None
    created_by: ActorRef | None
    updated_at: Timestamp


class SpecSummary(_Strict):
    key: str
    project: str
    name: str
    title: str
    status: SpecStatus
    labels: list[str]
    assignee_session: str | None
    assignee_slug: str | None
    created_by: ActorRef | None
    version: int
    created_at: Timestamp
    updated_at: Timestamp
    url: str


class SpecDetail(SpecSummary):
    documents: list[SpecDocument]
    tasks: list[str]
    open_comments: int


class SpecPage(_Strict):
    items: list[SpecSummary]
    next_cursor: str | None


class SpecComment(_Strict):
    id: int
    path: str | None
    version: int | None
    anchor: str | None
    body: str
    suggestion: str | None
    suggestion_status: SuggestionStatus | None
    accepted_version: int | None
    decided_at: Timestamp | None
    decided_by: ActorRef | None
    attachments: list[dict[str, Any]]
    resolved_at: Timestamp | None
    resolved_by: ActorRef | None
    created_by: ActorRef | None
    created_at: Timestamp


class CommentList(_Strict):
    items: list[SpecComment]


class SuggestionAccepted(_Strict):
    version: DocumentVersion
    comment: SpecComment


# Plan header and steps (base design 3.17). Caps are checked by `db/steps.py` (422).


class PlanPut(_Strict):
    version: int = Field(ge=0)
    goal: str = ""
    architecture: str = ""
    tech_stack: str = ""
    constraints: list[str] = []
    review_focus: list[str] = []


class Plan(_Strict):
    """The plan header; version 0 and empty fields before the first write."""

    goal: str
    architecture: str
    tech_stack: str
    constraints: list[str]
    review_focus: list[str]
    version: int
    updated_at: Timestamp | None
    updated_by: ActorRef | None


class StepIn(_Labelled):
    n: int = Field(ge=1)
    title: str = Title
    body: str = ""
    depends_on: list[int] = []
    model: Model | None = None
    labels: list[str] | None = None
    todo: list[str] | None = None


class StepsPut(_Strict):
    steps_version: int = Field(ge=0)
    steps: list[StepIn]


class StepInsert(_Strict):
    steps_version: int = Field(ge=0)
    after: int | None = Field(default=None, ge=0)
    title: str = Title
    body: str = ""
    depends_on: list[int] = []
    model: Model | None = None


class StepsOrder(_Strict):
    steps_version: int = Field(ge=0)
    order: list[str]


class NextStep(_Strict):
    claim: bool = False


class Step(_Strict):
    """A step as listed: no body; `deps` are task keys, `ready` when open and every dependency
    is done or dropped."""

    key: str
    step: int
    title: str
    status: Status
    assignee_session: str | None
    model: Model | None
    refs: list[Ref]
    version: int
    ready: bool
    todo_total: int
    todo_done: int
    deps: list[str]


class StepList(_Strict):
    items: list[Step]
    steps_version: int


class StepKeys(_Strict):
    keys: list[str]


class StepsVersion(_Strict):
    steps_version: int


class GateDecision(_Strict):
    id: int
    question: str
    status: Literal["open", "answered"]
    answer: Any


class GateOpenDecision(_Strict):
    task: str
    id: int
    question: str


class Gate(_Strict):
    key: str
    status: SpecStatus
    approved_versions: dict[str, int] | None
    gate_task: str | None
    decisions: list[GateDecision]
    gate: Literal["none", "waiting", "open"]
    approved: bool
    approvable: bool
    open_decisions: list[GateOpenDecision]


class Section(_Strict):
    path: str
    version: int
    heading: str
    body: str
    truncated: bool

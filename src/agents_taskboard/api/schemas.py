"""Request bodies of the tasks API."""

from __future__ import annotations

from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from agents_taskboard.labels import normalize_labels

Kind = Literal["idea", "task"]
Status = Literal["idea", "open", "in_progress", "done", "dropped"]
ListStatus = Literal["active", "done", "dropped", "all"]

Title = Field(min_length=1, max_length=200)
ProjectSlug = Field(default=None, pattern=r"^[a-z0-9][a-z0-9-]{0,39}$")
SpecKey = Field(default=None, pattern=r"^S-[1-9][0-9]{0,17}$")


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

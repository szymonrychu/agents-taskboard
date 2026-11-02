"""Request bodies of the tasks API."""

from __future__ import annotations

from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from agents_taskboard.labels import normalize_labels

Kind = Literal["idea", "task"]
Status = Literal["idea", "open", "in_progress", "done", "dropped"]
ListStatus = Literal["active", "done", "dropped", "all"]

Title = Field(min_length=1, max_length=200)


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


class TaskPatch(_Labelled):
    version: int
    title: str | None = Field(default=None, min_length=1, max_length=200)
    body: str | None = None
    status: Status | None = None
    labels: list[str] | None = None
    assignee_session: str | None = None
    assignee_self: bool = False


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

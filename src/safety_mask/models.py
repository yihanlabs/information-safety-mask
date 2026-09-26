from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

Category = Literal["person", "identity", "address", "phone"]
CATEGORIES = ["person", "identity", "address", "phone"]
CATEGORY_LABELS = {"person": "人名", "identity": "证件号码", "address": "详细住址", "phone": "电话号码"}


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class Rect(StrictModel):
    x: float = Field(ge=0, le=2_147_483_647)
    y: float = Field(ge=0, le=2_147_483_647)
    width: float = Field(gt=0, le=2_147_483_647)
    height: float = Field(gt=0, le=2_147_483_647)


class CustomField(StrictModel):
    id: str = Field(min_length=1, max_length=80)
    value: str = Field(min_length=1, max_length=500)
    enabled: bool = True

    @field_validator("value")
    @classmethod
    def not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("字段内容不能为空")
        return value


class RuleSet(StrictModel):
    categories: list[Category] = Field(default_factory=lambda: CATEGORIES.copy(), max_length=4)
    custom_fields: list[CustomField] = Field(default_factory=list, max_length=300)

    @field_validator("categories")
    @classmethod
    def unique_categories(cls, value):
        return list(dict.fromkeys(value))

    @field_validator("custom_fields")
    @classmethod
    def unique_ids(cls, value):
        if len({item.id for item in value}) != len(value):
            raise ValueError("字段编号重复")
        return value


class PerformanceSettings(StrictModel):
    mode: Literal["low_impact", "high_performance"]


class Word(StrictModel):
    start: int = Field(ge=0)
    end: int = Field(gt=0)
    box: Rect


class OcrLine(StrictModel):
    id: str
    text: str
    confidence: float = 1
    box: Rect
    words: list[Word] = Field(default_factory=list)


class Entity(StrictModel):
    line_id: str
    start: int
    end: int
    label: str


class Mask(StrictModel):
    id: str
    box: Rect
    source: Literal["auto", "manual"]
    reasons: list[str] = Field(default_factory=list)
    fallback: bool = False


class ManualMask(StrictModel):
    id: str = Field(min_length=1, max_length=80)
    box: Rect


class EditState(StrictModel):
    manual: list[ManualMask] = Field(default_factory=list, max_length=500)
    excluded: list[str] = Field(default_factory=list, max_length=2000)
    overrides: dict[str, Rect] = Field(default_factory=dict, max_length=2000)

    @field_validator("manual")
    @classmethod
    def unique_manual(cls, value):
        if len({item.id for item in value}) != len(value):
            raise ValueError("遮盖框编号重复")
        return value


class EditRequest(StrictModel):
    revision: int = Field(ge=1)
    edits: EditState


class ConfirmRequest(StrictModel):
    revision: int = Field(ge=1)
    reviewed: Literal[True]


class ExportItem(StrictModel):
    id: str
    revision: int = Field(ge=1)


class ExportRequest(StrictModel):
    images: list[ExportItem] = Field(min_length=1, max_length=1)
    format: Literal["png"] = "png"

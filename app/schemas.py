from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class BBox(BaseModel):
    """A word box in image pixel coordinates.

    Input accepts any of these equivalent shapes:

    * ``[x0, y0, x1, y1]``
    * ``{"x0": ..., "y0": ..., "x1": ..., "y1": ...}``
    * ``{"x": ..., "y": ..., "width": ..., "height": ...}``
    * ``{"left": ..., "top": ..., "right": ..., "bottom": ...}``

    Responses always use the explicit ``x0/y0/x1/y1`` object form.
    """

    x0: float
    y0: float
    x1: float
    y1: float

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {"x0": 175, "y0": 300, "x1": 315, "y1": 335}
            ]
        },
    )

    @model_validator(mode="before")
    @classmethod
    def _coerce_box(cls, value: Any) -> Any:
        if isinstance(value, (list, tuple)):
            if len(value) != 4:
                raise ValueError("bbox array must contain exactly [x0, y0, x1, y1]")
            return {
                "x0": value[0],
                "y0": value[1],
                "x1": value[2],
                "y1": value[3],
            }
        if isinstance(value, dict):
            keys = set(value)
            if {"x0", "y0", "x1", "y1"} <= keys:
                return value
            if {"x", "y", "width", "height"} <= keys:
                return {
                    "x0": value["x"],
                    "y0": value["y"],
                    "x1": value["x"] + value["width"],
                    "y1": value["y"] + value["height"],
                }
            if {"left", "top", "right", "bottom"} <= keys:
                return {
                    "x0": value["left"],
                    "y0": value["top"],
                    "x1": value["right"],
                    "y1": value["bottom"],
                }
        return value

    @model_validator(mode="after")
    def _check_order(self) -> "BBox":
        if self.x1 < self.x0 or self.y1 < self.y0:
            raise ValueError("bbox requires x1 >= x0 and y1 >= y0")
        return self


class OCRWordInput(BaseModel):
    text: str = Field(min_length=1, max_length=512, examples=["keeper"])
    bbox: BBox

    @field_validator("text")
    @classmethod
    def _nonblank_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("OCR word text must not be blank")
        return value


class OCRBlockInput(BaseModel):
    block_no: int = Field(ge=0, examples=[2])
    words: list[OCRWordInput]


class FlatOCRWordInput(OCRWordInput):
    block_no: int = Field(ge=0)


class OCRPageInput(BaseModel):
    """A complete OCR retransmission for one page.

    Blocks are the OCR export's layout blocks.  Word order inside each block is
    significant.  A flat ``words`` array with a ``block_no`` on every word is
    also accepted as a convenience for exports that do not nest blocks.
    """

    image_url: str | None = Field(default=None, examples=["/scans/page-002.svg"])
    width: float | None = Field(default=None, gt=0)
    height: float | None = Field(default=None, gt=0)
    blocks: list[OCRBlockInput] | None = None
    words: list[FlatOCRWordInput] | None = None

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "image_url": "/scans/page-002.svg",
                "width": 1600,
                "height": 2200,
                "blocks": [
                    {
                        "block_no": 1,
                        "words": [
                            {"text": "walked", "bbox": [100, 140, 220, 175]},
                            {"text": "the", "bbox": [230, 140, 295, 175]},
                            {"text": "tower", "bbox": [305, 140, 405, 175]},
                            {"text": "before", "bbox": [415, 140, 520, 175]},
                            {"text": "dawn", "bbox": [530, 140, 610, 175]},
                        ],
                    },
                    {
                        "block_no": 2,
                        "words": [
                            {"text": "only", "bbox": [100, 300, 175, 335]},
                            {"text": "in", "bbox": [185, 300, 220, 335]},
                            {"text": "the", "bbox": [230, 300, 295, 335]},
                            {"text": "archive", "bbox": [305, 300, 425, 335]},
                            {"text": "supplement", "bbox": [435, 300, 590, 335]},
                        ],
                    },
                ],
            }
        }
    )

    @model_validator(mode="after")
    def _check_forms(self) -> "OCRPageInput":
        blocks = self.blocks or []
        words = self.words or []
        if not blocks and not words:
            raise ValueError("provide either blocks[] or words[]")
        if blocks and words:
            raise ValueError("provide blocks[] or words[], not both")

        seen: set[int] = set()
        for block in blocks:
            if block.block_no in seen:
                raise ValueError(f"duplicate block_no {block.block_no}")
            seen.add(block.block_no)
        return self

    def grouped_blocks(self) -> list[tuple[int, list[OCRWordInput]]]:
        """Return ``(block_no, ordered words)`` pairs from either input form."""
        if self.blocks:
            return [(block.block_no, list(block.words)) for block in self.blocks]

        groups: dict[int, list[OCRWordInput]] = {}
        for word in self.words or []:
            groups.setdefault(word.block_no, []).append(word)
        return sorted(groups.items(), key=lambda item: item[0])


class CatalogBlockRef(BaseModel):
    page_id: str = Field(min_length=1, max_length=128, examples=["page-002"])
    block_no: int = Field(ge=0, examples=[1])

    @field_validator("page_id", mode="before")
    @classmethod
    def _page_id_to_text(cls, value: Any) -> Any:
        if isinstance(value, int):
            return str(value)
        if isinstance(value, str):
            return value.strip()
        return value


class CatalogArticleInput(BaseModel):
    """Catalog article text-block order, referencing OCR page and block numbers."""

    title: str | None = Field(default=None, max_length=512)
    blocks: list[CatalogBlockRef] = Field(min_length=1)

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "title": "A fourth local story",
                "blocks": [
                    {"page_id": "page-001", "block_no": 3},
                    {"page_id": "page-002", "block_no": 2},
                ],
            }
        }
    )

    @model_validator(mode="after")
    def _unique_refs(self) -> "CatalogArticleInput":
        seen: set[tuple[str, int]] = set()
        for ref in self.blocks:
            key = (ref.page_id, ref.block_no)
            if key in seen:
                raise ValueError(
                    f"duplicate catalog block reference {ref.page_id}/{ref.block_no}"
                )
            seen.add(key)
        return self


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------


class PageBlockInfo(BaseModel):
    block_no: int
    ordinal: int
    word_count: int


class PageInfo(BaseModel):
    page_id: str
    image_url: str | None
    width: float | None
    height: float | None
    revision: int
    updated_at: str
    blocks: list[PageBlockInfo]


class OCRUpdateResponse(BaseModel):
    page_id: str
    revision: int
    updated_at: str
    blocks_count: int
    words_count: int
    affected_articles: list[str]
    message: str


class ArticleInfo(BaseModel):
    article_id: str
    title: str | None
    revision: int
    updated_at: str
    status: str
    indexed_words: int
    blocks: list[CatalogBlockRef]


class WordLocation(BaseModel):
    seq: int
    page_id: str
    block_no: int
    word_no: int
    text: str
    bbox: BBox


class PhraseHit(BaseModel):
    article_id: str
    title: str | None
    start_seq: int
    pages: list[str]
    words: list[WordLocation]


class PhraseSearchResponse(BaseModel):
    query: str
    normalized_tokens: list[str]
    limit: int
    count: int
    truncated: bool
    hits: list[PhraseHit]


class HealthResponse(BaseModel):
    status: str
    database: str
    pages: int
    articles: int
    ocr_words: int
    indexed_words: int

"""Pydantic models exposed by the API.

These are intentionally simple — the goal is to give the API consumer
structured, JSON-serialisable records.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, HttpUrl


class Pin(BaseModel):
    """A single Pinterest pin."""

    id: str
    url: Optional[HttpUrl] = None
    title: Optional[str] = None
    description: Optional[str] = None
    image: Optional[Dict[str, Optional[str]]] = None  # {src, srcset, original}
    repin_count: Optional[int] = None
    like_count: Optional[int] = None
    source: Optional[str] = None  # "blueprint-selector" | "fallback" | "graphql"
    captured_at: datetime = Field(default_factory=datetime.utcnow)

    class Config:
        json_schema_extra = {
            "example": {
                "id": "123456789",
                "url": "https://www.pinterest.com/pin/123456789/",
                "title": "Modern kitchen design",
                "image": {
                    "src": "https://i.pinimg.com/236x/aa/bb/cc.jpg",
                    "srcset": "https://i.pinimg.com/236x/... 1x, "
                    "https://i.pinimg.com/originals/... 2x",
                    "original": "https://i.pinimg.com/originals/aa/bb/cc.jpg",
                },
                "source": "blueprint-selector",
            }
        }


class Board(BaseModel):
    """A Pinterest board (from a public profile)."""

    slug: str
    url: Optional[HttpUrl] = None
    title: Optional[str] = None
    pin_count: Optional[int] = None


class Profile(BaseModel):
    """A public Pinterest profile."""

    username: str
    title: Optional[str] = None
    boards: List[Board] = Field(default_factory=list)
    pins: List[Pin] = Field(default_factory=list)


class ScrapeResult(BaseModel):
    """Result of scraping a single URL."""

    url: HttpUrl
    title: Optional[str] = None
    pins: List[Pin] = Field(default_factory=list)
    graphql_responses: List[Dict[str, Any]] = Field(default_factory=list)
    mode: str = "auto"
    duration_ms: int = 0
    error: Optional[str] = None


class JobStatus(BaseModel):
    """Status payload for async background jobs."""

    job_id: str
    state: str  # "pending" | "running" | "done" | "error"
    progress: float = 0.0
    message: Optional[str] = None
    result: Optional[ScrapeResult] = None
    error: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


# ---- Request bodies for the FastAPI endpoints -----------------------


class ScrapePageRequest(BaseModel):
    url: HttpUrl
    mode: str = "auto"  # "auto" | "fast" | "full"
    max_scrolls: Optional[int] = None


class SearchRequest(BaseModel):
    query: str
    pages: int = 2
    mode: str = "auto"


class BoardRequest(BaseModel):
    username: str
    board_slug: str
    pages: int = 2
    mode: str = "auto"


class ProfileRequest(BaseModel):
    username: str
    mode: str = "auto"

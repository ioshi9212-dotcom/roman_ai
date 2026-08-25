from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field


class NovelTemplate(BaseModel):
    novel_id: str
    title: str
    version: int = 1
    novel: Dict[str, Any] = Field(default_factory=dict)
    characters: List[Dict[str, Any]] = Field(default_factory=list)
    lore: Dict[str, Any] = Field(default_factory=dict)


class NovelRawSave(BaseModel):
    template_json: str


class SessionCreate(BaseModel):
    novel_id: str


class SessionMeta(BaseModel):
    session_id: str
    source_novel_id: str
    source_novel_version: int
    turn_number: int = 0
    handoff_required: bool = False
    handoff_generation: int = 0


class TurnCommit(BaseModel):
    user_input: str
    scene_output: str
    extracted: Dict[str, Any] = Field(default_factory=dict)


class AuditCommit(BaseModel):
    start_turn: int
    end_turn: int
    repairs: Dict[str, Any] = Field(default_factory=dict)
    notes: List[str] = Field(default_factory=list)


class ResumeConfirm(BaseModel):
    resume_token: str


class SessionSnapshot(BaseModel):
    meta: SessionMeta
    source: Dict[str, Any] = Field(default_factory=dict)
    state: Dict[str, Any] = Field(default_factory=dict)
    chronology: List[Dict[str, Any]] = Field(default_factory=list)
    recent_turns: List[Dict[str, Any]] = Field(default_factory=list)

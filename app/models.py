from typing import Any, Dict, List, Optional
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .scene_format import validate_scene_output


class NovelTemplate(BaseModel):
    novel_id: str
    title: str
    version: int = 1
    novel: Dict[str, Any] = Field(default_factory=dict)
    characters: List[Dict[str, Any]] = Field(default_factory=list)
    lore: Dict[str, Any] = Field(default_factory=dict)


class NovelRawSave(BaseModel):
    template_json: str


class NovelDraftCreate(BaseModel):
    novel_id: str
    title: str
    version: int = 1


class NovelDraftSection(BaseModel):
    section_name: str
    section_json: str


class SessionCreate(BaseModel):
    novel_id: str


class TurnPrepare(BaseModel):
    user_input: str


class RollbackLastTurn(BaseModel):
    expected_turn_number: int = Field(ge=1)
    confirm: bool


class SessionMeta(BaseModel):
    session_id: str
    source_novel_id: str
    source_novel_version: int
    turn_number: int = 0
    handoff_required: bool = False
    handoff_generation: int = 0


class StoryThreadUpdate(BaseModel):
    model_config = ConfigDict(extra="allow")

    thread_id: str
    operation: Optional[str] = None
    title: Optional[str] = None
    summary: Optional[str] = None
    status: Optional[str] = None
    priority: Any = None
    premise: Optional[str] = None
    current_goal: Optional[str] = None
    current_phase: Optional[str] = None
    unresolved: Optional[List[Any]] = None
    end_conditions: Optional[List[Any]] = None
    possible_routes: Optional[List[Any]] = None
    anchor_facts: Optional[List[Any]] = None
    participants: Optional[List[str]] = None
    next_eligible_game_day: Optional[int] = Field(default=None, ge=0)
    progressed_now: Optional[bool] = None
    progress_summary: Optional[str] = None
    resolution: Optional[str] = None

    @field_validator("operation")
    @classmethod
    def operation_must_be_supported(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return value
        normalized = value.casefold().strip()
        if normalized not in {"upsert", "resolve", "abandon"}:
            raise ValueError("story thread operation must be upsert, resolve or abandon")
        return normalized


class TurnExtracted(BaseModel):
    model_config = ConfigDict(extra="allow")

    persistence_reviewed: bool
    chronology: List[Dict[str, Any]]
    knowledge_add: List[Dict[str, Any]]
    experiences_add: List[Dict[str, Any]]
    dialogue_memory_add: List[Dict[str, Any]]
    npc_intent_updates: List[Dict[str, Any]]
    story_thread_updates: List[StoryThreadUpdate]
    presence_updates: Optional[List[Dict[str, Any]]] = None
    relationship_updates: Optional[List[Dict[str, Any]]] = None
    state_patch: Dict[str, Any] = Field(default_factory=dict)
    character_upserts: Optional[List[Dict[str, Any]]] = None


class TurnCommit(BaseModel):
    user_input: str
    scene_output: str
    extracted: TurnExtracted

    @field_validator("scene_output")
    @classmethod
    def scene_output_must_match_builder(cls, value: str) -> str:
        return validate_scene_output(value)


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
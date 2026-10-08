from typing import Any, Dict, List, Literal, Optional
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator



class NovelTemplate(BaseModel):
    novel_id: str
    title: str
    version: int = 1
    novel: Dict[str, Any] = Field(default_factory=dict)
    characters: List[Dict[str, Any]] = Field(default_factory=list)
    lore: Dict[str, Any] = Field(default_factory=dict)
    locations: List[Dict[str, Any]] = Field(default_factory=list)
    canon_notes: List[Dict[str, Any]] = Field(default_factory=list)


class NovelRawSave(BaseModel):
    template_json: str


class NovelDraftCreate(BaseModel):
    novel_id: str
    title: str
    version: int = 1


class NovelDraftSection(BaseModel):
    section_name: str
    section_json: str
    expected_revision: Optional[int] = Field(default=None, ge=0)


class NovelDraftIntakeChunk(BaseModel):
    block_id: str = Field(min_length=1, max_length=120)
    stage: str = Field(min_length=1, max_length=120)
    chunk_index: int = Field(ge=0)
    raw_text: str = Field(min_length=1)
    is_last: bool = False


class NovelDraftIntakeMapping(BaseModel):
    fact_ids: List[str] = Field(default_factory=list)
    reviewed_against_raw: bool = False
    contains_no_facts: bool = False
    replace: bool = False
    expected_revision: Optional[int] = Field(default=None, ge=0)


class NovelDraftReconciliation(BaseModel):
    expected_revision: int = Field(ge=0)
    confirmed_against_raw: bool
    unresolved_conflicts: List[str] = Field(default_factory=list)
    notes: Optional[str] = None


class NovelDraftLaunchState(BaseModel):
    expected_finalized_revision: int = Field(ge=0)
    starting_state_json: str
    launch_hint: Optional[str] = None


class SessionCreate(BaseModel):
    novel_id: str


class TurnPrepare(BaseModel):
    user_input: str
    request_id: Optional[str] = Field(default=None, min_length=1, max_length=120)
    opening_scene: bool = False
    scene_archive_capable: bool = False
    replace_pending: bool = False


class SceneArchiveRead(BaseModel):
    scene_id: Optional[str] = Field(default=None, min_length=1)


class RollbackLastTurn(BaseModel):
    expected_turn_number: int = Field(ge=1)
    expected_turn_id: str = Field(min_length=1)
    confirm: bool


class SessionMeta(BaseModel):
    session_id: str
    source_novel_id: str
    source_novel_version: int
    turn_number: int = 0
    handoff_required: bool = False
    handoff_generation: int = 0


class RelationshipDimensionUpdate(BaseModel):
    label: str
    value: Optional[float] = None
    delta: Optional[float] = None


class RelationshipUpdate(BaseModel):
    character_id: str
    dimensions: Optional[List[RelationshipDimensionUpdate]] = None
    dynamic: Optional[str] = None
    reason: Optional[str] = None
    change_scale: Optional[str] = None


class RelationshipReviewItem(BaseModel):
    character_id: str
    changed: bool
    reason: str
    numeric_result: Literal["updated", "unchanged", "no_numeric_dimension_justified"]


class NPCRelationshipUpdate(BaseModel):
    owner_character_id: str
    target_character_id: str
    description: Optional[str] = None
    change_reason: Optional[str] = None


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
    pillar_ids: Optional[List[str]] = None
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


class SceneProgressionProof(BaseModel):
    target: str
    # Required by cast:independent so the validator can verify which NPC acted.
    character_id: Optional[str] = None
    kind: Literal[
        "new_information",
        "external_event",
        "npc_action",
        "thread_state_change",
        "relationship_shift",
        "new_constraint",
        "new_opportunity",
        "new_threat",
        "unfinished_action_specific",
    ]
    action: str
    end_state_change: str
    ending_kind: Literal[
        "new_fact",
        "intent_action",
        "incoming_contact",
        "relationship_shift",
        "conflict_change",
        "new_constraint",
        "new_opportunity",
        "new_threat",
        "concrete_next_pressure",
    ]
    ending_evidence_text: str


class TurnExtracted(BaseModel):
    model_config = ConfigDict(extra="allow")

    scene_builder_reviewed: bool = False
    persistence_reviewed: bool = False
    knowledge_reviewed: bool = False
    knowledge_journal_add: List[Dict[str, Any]] = Field(default_factory=list)
    chronology: List[Dict[str, Any]] = Field(default_factory=list)
    knowledge_add: List[Dict[str, Any]] = Field(default_factory=list)
    experiences_add: List[Dict[str, Any]] = Field(default_factory=list)
    dialogue_memory_add: List[Dict[str, Any]] = Field(default_factory=list)
    npc_intent_updates: List[Dict[str, Any]] = Field(default_factory=list)
    npc_relationship_updates: List[NPCRelationshipUpdate] = Field(default_factory=list)
    story_thread_updates: List[StoryThreadUpdate] = Field(default_factory=list)
    scene_progressed: Optional[bool] = None
    scene_progression: Optional[SceneProgressionProof] = None
    presence_updates: Optional[List[Dict[str, Any]]] = Field(default_factory=list)
    relationship_updates: Optional[List[RelationshipUpdate]] = Field(default_factory=list)
    relationship_review: Optional[List[RelationshipReviewItem]] = Field(
        default_factory=list,
        description="Mandatory one-row-per-physical-NPC relationship persistence review; review rows are validated then not persisted.",
    )
    state_patch: Dict[str, Any] = Field(default_factory=dict)
    character_upserts: Optional[List[Dict[str, Any]]] = Field(default_factory=list)

    @field_validator(
        "presence_updates",
        "relationship_updates",
        "relationship_review",
        "character_upserts",
        "knowledge_journal_add",
        "npc_relationship_updates",
        mode="before",
    )
    @classmethod
    def nullable_optional_lists_become_empty(cls, value: Any):
        return [] if value is None else value


class TurnCommit(BaseModel):
    packet_id: str = Field(min_length=1)
    user_input: str
    scene_output: str = Field(min_length=1)
    extracted: TurnExtracted


class AuditCommit(BaseModel):
    audit_id: str = Field(min_length=1)
    start_turn: int
    end_turn: int
    repairs: Dict[str, Any] = Field(default_factory=dict)
    notes: List[str] = Field(default_factory=list)


class CommitTurnRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    packet_id: Optional[str] = Field(default=None, min_length=1)
    user_input: Optional[str] = None
    scene_output: Optional[str] = Field(default=None, min_length=1)
    extracted: Optional[TurnExtracted] = None

    audit_id: Optional[str] = Field(default=None, min_length=1)
    start_turn: Optional[int] = Field(default=None, ge=1)
    end_turn: Optional[int] = Field(default=None, ge=1)
    repairs: Dict[str, Any] = Field(default_factory=dict)
    notes: List[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_commit_mode(self):
        audit_mode = self.audit_id is not None
        turn_fields = (self.packet_id, self.user_input, self.scene_output, self.extracted)

        if audit_mode:
            if self.start_turn is None or self.end_turn is None:
                raise ValueError("audit commit requires audit_id, start_turn and end_turn")
            if any(value is not None for value in turn_fields):
                raise ValueError("audit commit cannot include gameplay turn fields")
            return self

        if self.start_turn is not None or self.end_turn is not None:
            raise ValueError("gameplay turn cannot include audit turn range")
        if self.packet_id is None or self.user_input is None or self.scene_output is None or self.extracted is None:
            raise ValueError("gameplay turn requires packet_id, user_input, scene_output and extracted")
        return self


class ResumeConfirm(BaseModel):
    resume_token: str


class SessionSnapshot(BaseModel):
    meta: SessionMeta
    source: Dict[str, Any] = Field(default_factory=dict)
    state: Dict[str, Any] = Field(default_factory=dict)
    chronology: List[Dict[str, Any]] = Field(default_factory=list)
    recent_turns: List[Dict[str, Any]] = Field(default_factory=list)

class ContinuationBlockCommit(BaseModel):
    migration_id: str = Field(min_length=1)
    summary: Dict[str, Any] = Field(default_factory=dict)


class ContinuationFinalCommit(BaseModel):
    migration_id: str = Field(min_length=1)
    package: Dict[str, Any] = Field(default_factory=dict)

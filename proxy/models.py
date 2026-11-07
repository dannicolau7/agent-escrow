from __future__ import annotations

from typing import Any, Dict, Optional

from pydantic import BaseModel, Field


class ToolCallRequest(BaseModel):
    agent_id: str
    tool_name: str
    tool_input: Dict[str, Any] = Field(default_factory=dict)


class ToolCallResponse(BaseModel):
    call_id: str
    status: str  # pending | allowed
    is_mutating: bool
    message: str


class CallRecord(BaseModel):
    call_id: str
    agent_id: str
    tool_name: str
    tool_input: Dict[str, Any]
    status: str  # pending | approved | rejected | allowed
    is_mutating: bool
    policy_reason: Optional[str]
    created_at: str
    resolved_at: Optional[str]
    resolved_by: Optional[str]


class ReviewDecision(BaseModel):
    reviewer_id: str = "anonymous"
    reason: Optional[str] = None


class WaitResponse(BaseModel):
    call_id: str
    status: str
    timed_out: bool = False

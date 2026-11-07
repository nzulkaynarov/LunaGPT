"""Pydantic v2 schemas for API requests and responses."""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class PromptRequest(BaseModel):
    """Chat prompt execution request."""

    prompt: str = Field(..., min_length=1, description="User prompt text")
    user_id: str = Field(default="umarbek", description="User ID for Langfuse")
    session_id: str = Field(default="default-session", description="Conversation session ID")
    use_memory: bool = Field(default=True, description="Whether to query past lessons from pgvector")
    use_rag: bool = Field(default=True, description="Whether to query ingested documents")

    model_config = ConfigDict(extra="ignore")


class PromptResponse(BaseModel):
    """Chat prompt execution response."""

    success: bool
    model: str
    reason: str
    duration_sec: float
    response: str
    trace_id: str
    tokens: dict = Field(default_factory=dict)
    lessons_injected: int = 0
    rag_chunks_injected: int = 0


class FeedbackRequest(BaseModel):
    """User feedback submission request."""

    trace_id: str
    session_id: str
    user_prompt: str
    rejected_response: str | None = None
    corrected_response: str
    comment: str | None = None
    score: float = 0.0  # 0.0 thumbs down, 1.0 thumbs up
    model_used: str = "luna-coder"


class FeedbackResponse(BaseModel):
    """User feedback response."""

    success: bool
    memory_id: str
    message: str


class DocumentIngestResponse(BaseModel):
    """Response after ingesting a document."""

    document_id: str
    title: str
    file_type: str
    chunks_count: int
    duration_sec: float


class ChatMessage(BaseModel):
    """Standard OpenAI chat message schema supporting strings, lists, dicts, and tool calls."""

    role: str
    content: Any | None = None
    name: str | None = None
    tool_calls: list[Any] | None = None
    tool_call_id: str | None = None
    function_call: Any | None = None

    model_config = ConfigDict(extra="allow")


class ChatCompletionRequest(BaseModel):
    """Standard OpenAI /v1/chat/completions request schema."""

    model: str = "luna-auto"
    messages: list[ChatMessage]
    stream: bool = False
    temperature: float | None = None
    top_p: float | None = None
    max_tokens: int | None = None
    max_completion_tokens: int | None = None
    user: Any | None = None
    tools: list[Any] | None = None
    tool_choice: Any | None = None
    stop: Any | None = None
    seed: int | None = None

    model_config = ConfigDict(extra="allow")


class ModelItem(BaseModel):
    """Standard OpenAI model object."""

    id: str
    object: str = "model"
    created: int = 1700000000
    owned_by: str = "lunagpt"


class ModelListResponse(BaseModel):
    """Standard OpenAI /v1/models response."""

    object: str = "list"
    data: list[ModelItem]

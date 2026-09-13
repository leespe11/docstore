from __future__ import annotations

import json
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy import select

from ..agent.loop import run_chat
from ..db import SessionLocal
from ..models import ChatMessage, ChatSession
from ..schemas import ChatRequest

router = APIRouter(tags=["chat"])


def _sse(event: dict) -> str:
    return f"data: {json.dumps(event)}\n\n"


def _title_from(text: str, limit: int = 60) -> str:
    title = " ".join(text.strip().split())
    if not title:
        return "New chat"
    return title if len(title) <= limit else title[: limit - 1].rstrip() + "…"


@router.post("/chat")
async def chat_endpoint(payload: ChatRequest) -> StreamingResponse:
    if not payload.message.strip():
        raise HTTPException(422, "message must not be empty")

    async def event_stream():
        # Own session here (rather than a Depends-injected one) so it stays
        # open for the lifetime of the stream, not just the route handler.
        async with SessionLocal() as session:
            is_new = payload.chat_id is None

            if is_new:
                chat_session = ChatSession(title=_title_from(payload.message))
                session.add(chat_session)
                await session.flush()
            else:
                chat_session = await session.get(ChatSession, payload.chat_id)
                if chat_session is None:
                    yield _sse({"type": "error", "text": "chat not found"})
                    yield _sse({"type": "done"})
                    return

            prior = (
                await session.execute(
                    select(ChatMessage)
                    .where(ChatMessage.session_id == chat_session.id)
                    .order_by(ChatMessage.created_at)
                )
            ).scalars().all()
            history = [{"role": m.role, "content": m.content} for m in prior]
            history.append({"role": "user", "content": payload.message})

            session.add(ChatMessage(session_id=chat_session.id, role="user", content=payload.message))
            chat_session.updated_at = datetime.now(timezone.utc)
            await session.commit()

            if is_new:
                # Let the UI pick up the new session id (and refresh its
                # history list) before any tokens arrive.
                yield _sse({"type": "chat_id", "chat_id": str(chat_session.id)})

            full_text: list[str] = []
            sources: list[dict] = []
            async for event in run_chat(session, history):
                if event["type"] == "token":
                    full_text.append(event["text"])
                elif event["type"] == "sources":
                    sources = event["documents"]
                yield _sse(event)

            session.add(
                ChatMessage(
                    session_id=chat_session.id,
                    role="assistant",
                    content="".join(full_text),
                    sources=sources,
                )
            )
            chat_session.updated_at = datetime.now(timezone.utc)
            await session.commit()

    return StreamingResponse(event_stream(), media_type="text/event-stream")

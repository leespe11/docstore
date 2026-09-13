from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from ..db import get_session
from ..models import ChatSession
from ..schemas import ChatSessionDetail, ChatSessionOut

router = APIRouter(prefix="/chats", tags=["chats"])


@router.get("", response_model=list[ChatSessionOut])
async def list_chats(session: AsyncSession = Depends(get_session)) -> list[ChatSessionOut]:
    stmt = select(ChatSession).order_by(ChatSession.updated_at.desc()).limit(200)
    rows = (await session.execute(stmt)).scalars().all()
    return [ChatSessionOut.model_validate(r) for r in rows]


@router.get("/{chat_id}", response_model=ChatSessionDetail)
async def get_chat(chat_id: uuid.UUID, session: AsyncSession = Depends(get_session)) -> ChatSessionDetail:
    stmt = (
        select(ChatSession)
        .options(selectinload(ChatSession.messages))
        .where(ChatSession.id == chat_id)
    )
    chat = (await session.execute(stmt)).scalars().first()
    if chat is None:
        raise HTTPException(404, "chat not found")
    return ChatSessionDetail.model_validate(chat)


@router.delete("/{chat_id}", status_code=204, response_model=None)
async def delete_chat(chat_id: uuid.UUID, session: AsyncSession = Depends(get_session)) -> None:
    chat = await session.get(ChatSession, chat_id)
    if chat is None:
        raise HTTPException(404, "chat not found")
    await session.delete(chat)
    await session.commit()

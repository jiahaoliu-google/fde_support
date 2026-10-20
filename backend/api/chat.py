"""POST /api/chat. The pipeline (redact, classify, route, persist) lives in backend/services/chat.py."""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from backend.api.deps import get_caller
from backend.api.schemas import SupportQueryRequest, SupportQueryResponse
from backend.db.session import get_db
from backend.services import chat as chat_service

router = APIRouter()


@router.post("/api/chat", response_model=SupportQueryResponse)
async def process_support_query(request: SupportQueryRequest, db: Session = Depends(get_db), caller: str = Depends(get_caller)):
    if not request.message or not request.message.strip():
        raise HTTPException(status_code=400, detail="Message cannot be empty")
    return await chat_service.handle_support_query(db, caller, request.message, request.session_id)

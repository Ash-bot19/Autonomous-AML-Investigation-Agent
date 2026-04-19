"""POST /investigate — HTTP trigger for AML investigation agent."""
from __future__ import annotations

import structlog
from fastapi import APIRouter, BackgroundTasks
from pydantic import BaseModel

from agent.runner import run_investigation
from models.schemas import InvestigationPayload

log = structlog.get_logger()

router = APIRouter()


class InvestigateResponse(BaseModel):
    investigation_id: str
    status: str


@router.post("/investigate", response_model=InvestigateResponse)
async def post_investigate(
    payload: InvestigationPayload,
    background_tasks: BackgroundTasks,
) -> InvestigateResponse:
    """Accept an InvestigationPayload and dispatch a background investigation.

    Returns immediately with investigation_id and status='started'.
    """
    log.info(
        "api.investigate.received",
        investigation_id=payload.investigation_id,
        txn_id=payload.txn_id,
    )
    background_tasks.add_task(run_investigation, payload)
    return InvestigateResponse(investigation_id=payload.investigation_id, status="started")

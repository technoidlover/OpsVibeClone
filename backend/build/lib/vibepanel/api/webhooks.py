import hashlib
import hmac
import json

from fastapi import APIRouter, Header, HTTPException, Request, status

from ..i18n import t
from .. import github
from ..models import JobKind, JobTrigger, Service
from .deps import DB, Box, Queue

router = APIRouter(prefix="/api/webhooks", tags=["webhooks"])

MAX_WEBHOOK_BODY = 5 * 1024 * 1024


@router.post("/github/{service_id}")
async def github_webhook(
    service_id: int,
    request: Request,
    db: DB,
    box: Box,
    queue: Queue,
    x_github_event: str = Header(""),
    x_hub_signature_256: str = Header(""),
) -> dict:
    body = await request.body()
    if len(body) > MAX_WEBHOOK_BODY:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, t("webhook.payload_too_large"))

    service = db.get(Service, service_id)
    # Không phân biệt "không tồn tại" và "sai chữ ký" để tránh dò ID service
    expected = (
        "sha256=" + hmac.new(box.decrypt(service.webhook_secret_enc).encode(), body, hashlib.sha256).hexdigest()
        if service
        else ""
    )
    if not service or not hmac.compare_digest(expected, x_hub_signature_256):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, t("webhook.bad_signature"))

    if x_github_event == "ping":
        return {"status": "ok", "message": "pong"}
    if x_github_event != "push":
        return {"status": "ignored", "reason": t("webhook.event_ignored", event=repr(x_github_event))}

    try:
        payload = json.loads(body)
    except ValueError:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, t("webhook.not_json"))

    if service.source != "git":
        return {"status": "ignored", "reason": t("webhook.image_service")}
    if not service.auto_deploy:
        return {"status": "ignored", "reason": t("webhook.auto_deploy_off")}
    if payload.get("deleted"):
        return {"status": "ignored", "reason": t("webhook.branch_deleted")}
    if payload.get("ref") != f"refs/heads/{service.branch}":
        return {"status": "ignored", "reason": t("webhook.other_branch", branch=service.branch)}

    if not github.push_touches(payload, service.root_dir):
        return {"status": "ignored", "reason": t("webhook.root_untouched", path=service.root_dir)}

    job = queue.enqueue(db, service, JobKind.DEPLOY, JobTrigger.WEBHOOK)
    return {"status": "queued", "job_id": job.id}

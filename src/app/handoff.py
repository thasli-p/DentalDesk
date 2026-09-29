"""
Shared WhatsApp inbox: clinic staff can read any chat on the dashboard,
reply to the patient themselves, and hand the chat back to Sia.

How it works
------------
- Sending a reply (or pressing "Take over") pauses Sia for that patient's
  number. While paused, the patient's messages are still saved (so staff
  see them on the dashboard) but Sia doesn't answer.
- The pause ends when staff press "Return to Sia", or automatically
  HANDOFF_AUTO_RESUME_HOURS after the last staff action (default 4), so a
  chat is never left unanswered because someone forgot to hand it back.
- Staff replies are stored as sender='staff'. When Sia picks the chat up
  again, her history is rebuilt from the database, so she sees what staff
  said (and anything the patient wrote meanwhile) and doesn't contradict it.
- Meta allows free-text replies only within 24 h of the patient's last
  message. Outside that window the reply box is closed; the page says so.

The pause is stored per patient (patients.ai_paused_until), not per
conversation, so it survives a conversation timing out and reopening.
"""
import json
import logging
import os
from datetime import datetime, timedelta
from typing import Optional

import requests

from app.whatsapp import format_message_content
from shared import db

logger = logging.getLogger(__name__)

# Meta's customer-service window is 24 h; stop a few minutes short so a
# reply typed at the last second isn't rejected by Meta.
WINDOW = timedelta(hours=24)
WINDOW_MARGIN = timedelta(minutes=5)
MAX_REPLY_CHARS = 4000  # WhatsApp's text limit is 4096


def auto_resume_hours() -> float:
    try:
        return max(0.25, float(os.environ.get("HANDOFF_AUTO_RESUME_HOURS", "4")))
    except ValueError:
        return 4.0


def window_closes_at(phone_number: str) -> Optional[datetime]:
    """When the 24-hour reply window for this number closes, or None if the
    patient has never written on WhatsApp."""
    last_in = db.last_inbound_whatsapp_at(phone_number)
    if last_in is None:
        return None
    return last_in + WINDOW - WINDOW_MARGIN


def window_open(phone_number: str, now: Optional[datetime] = None) -> bool:
    closes = window_closes_at(phone_number)
    return closes is not None and (now or datetime.now()) < closes


def _forget_agent_thread(clinic: dict, conversation_id: int) -> None:
    """Drop Sia's in-memory copy of this conversation, so the next time she
    answers it her history is rebuilt from the database -- which includes
    staff replies and messages that arrived while she was paused."""
    try:
        from app.clinic_registry import clinic_workers
        worker = clinic_workers.get(clinic["id"])
        if worker is not None and worker.agent is not None and worker.agent.checkpointer is not None:
            worker.agent.checkpointer.delete_thread(str(conversation_id))
    except Exception:
        logger.exception("[%s] Could not reset Sia's thread for conversation %s", clinic.get("slug"), conversation_id)


def take_over(clinic: dict, conversation, by: str, now: Optional[datetime] = None) -> datetime:
    """Pause Sia for this conversation's patient. Returns when the pause ends."""
    now = now or datetime.now()
    until = now + timedelta(hours=auto_resume_hours())
    db.pause_ai(conversation.patient_id, until, by)
    _forget_agent_thread(clinic, conversation.id)
    return until


def hand_back(clinic: dict, conversation) -> None:
    db.resume_ai(conversation.patient_id)
    _forget_agent_thread(clinic, conversation.id)


class ReplyError(Exception):
    """A reply that couldn't be sent. .code is a short key the dashboard
    turns into a translated message."""

    def __init__(self, code: str, detail: str = ""):
        super().__init__(detail or code)
        self.code = code
        self.detail = detail


def _send_text(clinic: dict, to: str, text: str) -> None:
    api_version = os.environ.get("GRAPH_API_VERSION") or "v25.0"
    url = f"https://graph.facebook.com/{api_version}/{clinic['whatsapp_phone_number_id']}/messages"
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to,
        "type": "text",
        "text": {"preview_url": False, "body": format_message_content(text)},
    }
    try:
        r = requests.post(
            url,
            headers={"Authorization": f"Bearer {clinic['whatsapp_access_token']}",
                     "Content-Type": "application/json"},
            data=json.dumps(payload),
            timeout=20,
        )
    except requests.RequestException as e:
        logger.error("[%s] Staff reply to %s failed: %s", clinic.get("slug"), to, e)
        raise ReplyError("send_failed", str(e))
    if r.status_code >= 400:
        code = None
        try:
            code = r.json().get("error", {}).get("code")
        except ValueError:
            pass
        logger.error("[%s] Staff reply to %s rejected by Meta (%s): %s", clinic.get("slug"), to, r.status_code, r.text)
        if code == 131047:
            raise ReplyError("window_closed", r.text)
        raise ReplyError("send_failed", r.text)


def staff_reply(clinic: dict, conversation, text: str, by: str, now: Optional[datetime] = None):
    """Send a manual reply from the dashboard. Pauses Sia for this patient
    (or extends the pause), saves the message as sender='staff', and
    reopens the conversation if it had timed out. Raises ReplyError."""
    now = now or datetime.now()
    text = (text or "").strip()
    if not text:
        raise ReplyError("empty")
    if len(text) > MAX_REPLY_CHARS:
        raise ReplyError("too_long")
    if getattr(conversation, "channel", "whatsapp") != "whatsapp" or not conversation.patient_id:
        raise ReplyError("not_whatsapp")

    patient = db.get_patient(conversation.patient_id)
    if patient is None or not patient.phone_number:
        raise ReplyError("not_whatsapp")

    latest = db.get_latest_whatsapp_conversation(patient.id)
    if latest is not None and latest.id != conversation.id:
        raise ReplyError("not_latest")

    if not window_open(patient.phone_number, now):
        raise ReplyError("window_closed")

    # Pause first, so a patient message arriving while the send is in
    # flight isn't answered by Sia on top of the staff reply.
    take_over(clinic, conversation, by, now)
    _send_text(clinic, patient.phone_number, text)

    if conversation.status != "open":
        db.reopen_conversation(conversation.id)
    db.add_message(conversation_id=conversation.id, sender="staff", message=text)
    logger.info("[%s] Staff (%s) replied to %s in conversation %s", clinic.get("slug"), by, patient.phone_number, conversation.id)

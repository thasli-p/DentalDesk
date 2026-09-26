import logging
import json
import hmac
import hashlib
import os, getpass
import sys
import asyncio
from datetime import datetime, timedelta
from dotenv import load_dotenv
from shared.logger_config import setup_logging
from fastapi import FastAPI, Request, HTTPException, Depends
from fastapi.staticfiles import StaticFiles
from . import whatsapp as whatsapp
from . import admin
from . import dashboard
from . import voice
from .clinic_registry import launch_clinic_worker, get_worker_by_phone_number_id
from shared import clinics_store

load_dotenv()
setup_logging()

def _set_env(var: str):
    if os.environ.get(var):
        return
    if sys.stdin is not None and sys.stdin.isatty():
        # Running interactively (e.g. a developer's own terminal) with the
        # variable missing from .env — prompt for it, same as before.
        os.environ[var] = getpass.getpass(f"{var}: ")
    else:
        # Running non-interactively (a cloud host, a background process) —
        # there's no one to answer a prompt, so getpass would just hang
        # forever with no explanation. Fail fast with a clear error instead.
        raise RuntimeError(
            f"Required environment variable '{var}' is not set. Set it in your "
            "hosting platform's environment variables (or in .env for local runs)."
        )

# App-level environment variables -- shared by every clinic running in this
# one service, since they all go through the same Meta App. Per-clinic
# credentials (access token, phone number ID, WABA ID) are no longer
# environment variables at all -- they live in the clinics store (see
# shared/clinics_store.py) and are entered once, per clinic, at
# /admin/clinics. META_ACCESS_TOKEN / META_PHONE_NUMBER_ID are still read
# from the environment ONCE, by clinics_store.migrate_legacy_single_clinic_if_needed(),
# purely to carry over a deployment that predates this feature -- see that
# function for details.
_set_env("ANTHROPIC_API_KEY")
_set_env("META_APP_SECRET")
_set_env("GRAPH_API_VERSION")
_set_env("META_VERIFY_TOKEN")
_set_env("ADMIN_PASSWORD")

# Get a logger for this module
logger = logging.getLogger(__name__)

app = FastAPI()
app.include_router(admin.router)
app.include_router(dashboard.router)
app.include_router(voice.router)

# Serves files from the static/ folder (e.g. the offers/promotions brochure
# image) at public URLs like <your-app-url>/static/brochure.jpg -- this is
# what lets Sia send that image over WhatsApp, since WhatsApp needs a real,
# publicly reachable URL rather than a local file path. This mount is only
# used by the ORIGINAL clinic (migrated from before multi-clinic support);
# clinics added through /admin/clinics use the /clinic-static mount below
# instead, so their brochure files never collide by filename.
STATIC_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "static")
os.makedirs(STATIC_DIR, exist_ok=True)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

# Serves every clinic's own uploaded brochure image, one subfolder per
# clinic slug (data/clinics/<slug>/static/<file>), at
# /clinic-static/<slug>/static/<file> -- see clinic_config.py's
# CLINIC_STATIC_URL_PREFIX for how each clinic's own URL is built.
os.makedirs(clinics_store.DATA_ROOT, exist_ok=True)
app.mount("/clinic-static", StaticFiles(directory=clinics_store.DATA_ROOT), name="clinic_static")


@app.on_event("startup")
async def startup_event():
    clinics_store.init_control_db()
    clinics_store.migrate_legacy_single_clinic_if_needed()

    clinics = clinics_store.list_clinics()
    if not clinics:
        logger.warning(
            "No clinics are configured yet. Add the first one at /admin/clinics "
            "(username 'admin' by default, password from ADMIN_PASSWORD)."
        )

    for clinic in clinics:
        if not clinic["active"]:
            logger.info("Skipping inactive clinic id=%s slug=%s", clinic["id"], clinic["slug"])
            continue
        logger.info("Starting agent consumer process for clinic %s (%s)...", clinic["id"], clinic["name"])
        launch_clinic_worker(clinic)


def verify_signature(request: Request):
    logger.debug("Verifying request signature")

    signature = request.headers.get("X-Hub-Signature-256", "")[7:]

    async def get_body():
        return await request.body()

    body = asyncio.run(get_body())

    digest = hmac.new(
        bytes(os.environ.get("META_APP_SECRET"), "latin-1"),
        msg=body,
        digestmod=hashlib.sha256,
    ).hexdigest()

    if not hmac.compare_digest(digest, signature):
        logger.error("Signature verification failed!")
        raise HTTPException(status_code=403, detail="signature is not valid")
    return True


@app.get("/webhook")
async def verify_webhook(request: Request):
    logger.info("Received a GET request on /webhook for verification")

    mode = request.query_params.get("hub.mode")
    token = request.query_params.get("hub.verify_token")
    challenge = request.query_params.get("hub.challenge")

    if mode and token and challenge:
        if mode == "subscribe" and token == os.environ.get("META_VERIFY_TOKEN"):
            logger.info("whatsapp webhook verified successfully")
            return int(challenge)
        else:
            logger.error("whatsapp webhook verification failed - invalid token")
            raise HTTPException(status_code=403, detail="Forbidden")
    else:
        logger.error("whatsapp webhook verification failed - missing parameters")
        raise HTTPException(status_code=400, detail="Missing parameters for verification")


# Per-clinic subscription/trial cutoff. Configured per clinic via the
# subscription_plan / subscription_started_at fields on its clinics-store
# row (set when the clinic is added at /admin/clinics, editable directly
# in the database if a plan needs to change later). If either is missing
# for some reason, the clinic is always treated as active so it's never
# accidentally blocked.
SUBSCRIPTION_PLAN_DAYS = clinics_store.SUBSCRIPTION_PLAN_DAYS


def is_subscription_active(clinic: dict) -> bool:
    started_at_str = clinic.get("subscription_started_at")
    plan = clinic.get("subscription_plan")
    if not started_at_str or not plan:
        return True
    days = SUBSCRIPTION_PLAN_DAYS.get(plan)
    if not days:
        logger.error(f"Unknown subscription_plan '{plan}' for clinic {clinic.get('slug')} -- treating this clinic as active so it is never accidentally blocked.")
        return True
    started_at = datetime.strptime(started_at_str, "%Y-%m-%d")
    return datetime.now() < started_at + timedelta(days=days)


@app.post("/webhook")
async def receive_webhook(request: Request, signature_valid: bool = Depends(verify_signature)):
    logger.debug("Received a POST request on /webhook")

    try:
        body = await request.json()
    except json.JSONDecodeError:
        logger.error("Failed to decode JSON from request body")
        raise HTTPException(status_code=400, detail="Invalid JSON provided in request body")

    logger.debug(f"request body: {body}")

    # Respond to status updates (like message delivered, read etc.)
    if whatsapp.is_status_update(body):
        status = whatsapp.parse_status_update(body)
        logger.info(f"WhatsApp status update: {status}")
        return {"status": "ok"}

    # This one shared endpoint receives messages for EVERY clinic running
    # on this service. Every event says which WhatsApp number it arrived
    # on (phone_number_id) -- that's how we tell clinics apart and route
    # to the right one's own worker/database/credentials.
    receiving_phone_number_id = whatsapp.get_receiving_phone_number_id(body)
    worker = get_worker_by_phone_number_id(receiving_phone_number_id) if receiving_phone_number_id else None

    if whatsapp.is_valid_message(body) and worker is None:
        sender = whatsapp.get_message_sender(body)
        logger.error(
            f"Received a message for phone_number_id={receiving_phone_number_id}, which isn't a clinic "
            f"registered on this service (sender: {sender}). Was this number's clinic added at /admin/clinics?"
        )
        # Acknowledge safely either way -- Meta shouldn't see this as a
        # failed delivery, since that risks Meta throttling the webhook.
        return {"status": "ok"}

    if whatsapp.is_valid_message(body) and not is_subscription_active(worker.clinic):
        sender = whatsapp.get_message_sender(body)
        logger.info(f"[{worker.clinic['slug']}] Message from {sender} ignored -- this clinic's subscription/trial has ended.")
        if sender:
            try:
                worker.send(
                    sender,
                    "Sorry, this clinic's subscription has ended. Please contact us to renew. 🙏\n"
                    "عذرًا، انتهت فترة اشتراك هذه العيادة. يرجى التواصل معنا للتجديد.",
                )
            except Exception as e:
                logger.error(f"Failed to send subscription-expired notice to {sender}: {e}")
        return {"status": "ok"}

    try:
        if whatsapp.is_valid_message(body) and whatsapp.is_text_message(body):
            phone_number, message_body = whatsapp.parse_phone_and_message(body)
            logger.info(f"[{worker.clinic['slug']}] Incoming message from {phone_number}: {message_body}")
            await worker.enqueue_message(phone_number, message_body)

            return {"status": "ok"}
        elif whatsapp.is_valid_message(body):
            # A real message, but not plain text (voice note, image, sticker,
            # reaction, location, etc) -- Sia/the agent pipeline only handles
            # text today. Acknowledge safely and let the patient know in
            # their own chat why nothing happened, rather than silently
            # ignoring them or returning an error (repeated failed webhook
            # deliveries risk Meta throttling/disabling the subscription).
            msg_type = body["entry"][0]["changes"][0]["value"]["messages"][0].get("type")
            sender = whatsapp.get_message_sender(body)
            logger.info(f"[{worker.clinic['slug']}] Received unsupported message type '{msg_type}' from {sender} -- only text messages are handled today.")
            if sender:
                try:
                    worker.send(
                        sender,
                        "Sorry, I can only read text messages right now — could you type your message instead? 🙏\n"
                        "عذرًا، يمكنني حاليًا قراءة الرسائل النصية فقط، هل يمكنك كتابة رسالتك؟",
                    )
                except Exception as e:
                    logger.error(f"Failed to send unsupported-message-type notice to {sender}: {e}")
            return {"status": "ok"}
        else:
            # Not a text message we recognize (could be a reaction, a
            # template-quality/webhook-test ping, a read receipt shape we
            # don't otherwise catch, etc). Log the full body at INFO so we
            # can see exactly what it was, but respond 200 OK -- returning
            # an error here for event types we simply don't act on yet is
            # what was causing Meta to see repeated failed webhook
            # deliveries, which risks Meta throttling/disabling the
            # webhook subscription entirely. Always acknowledge safely.
            logger.info(f"Unhandled webhook event (not a text message or status update): {body}")
            return {"status": "ok"}

    except Exception as e:
        logger.error(f"Error processing message: {e}")
        raise HTTPException(status_code=400, detail="Error processing message")


def main():
    import uvicorn
    # Most cloud hosts (Railway, Render, Heroku, etc.) assign a port at
    # runtime via the PORT environment variable and require the app to
    # listen on it. Prefer that when present; otherwise fall back to
    # FAST_API_PORT (or 8000) for local development.
    port = int(os.environ.get("PORT") or os.environ.get("FAST_API_PORT", 8000))
    logger.info("Starting FastAPI server on port %s...", port)
    uvicorn.run(app, host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()

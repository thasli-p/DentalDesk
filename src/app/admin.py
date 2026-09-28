"""
The "Add Clinic" admin page.

This is the whole point of the multi-clinic feature: instead of a new
Railway service, a git branch, or a separate app for every clinic, adding
a clinic is filling in this one form and clicking Save. On submit, this:

  1. Saves the clinic's details (hours, address, services, dentists,
     offers brochure) and its WhatsApp credentials into the clinics store.
  2. Subscribes that clinic's WhatsApp Business Account to this same app,
     so Meta starts sending its messages to this same running service.
  3. Starts that clinic's own background worker immediately, in this same
     running process -- no restart, no redeploy, no new Railway service.

The one thing this page CANNOT do for you is the Meta-side setup that has
to happen in Meta's own dashboard first: verifying the clinic's phone
number and creating a System User access token for it. See
claude/how-to-add-remove-clinic-whatsapp-number.md for that part -- once
you have that phone number ID, WABA ID, and access token in hand, this
page does everything else.

Protected with a simple username/password (HTTP Basic Auth) set via the
ADMIN_USERNAME / ADMIN_PASSWORD environment variables, so this isn't left
open to the public internet.
"""
import os
import re
import html
import logging
import secrets as _secrets
from datetime import datetime
from typing import Optional

import requests
from fastapi import APIRouter, Depends, Form, UploadFile, File, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from shared import clinics_store, voice_usage
from app.clinic_registry import launch_clinic_worker, stop_clinic_worker

logger = logging.getLogger(__name__)

router = APIRouter()
security = HTTPBasic()

SUBSCRIPTION_PLAN_CHOICES = ["trial", "1_month", "3_months", "6_months", "12_months"]


def require_admin(credentials: HTTPBasicCredentials = Depends(security)):
    correct_username = os.environ.get("ADMIN_USERNAME", "admin")
    correct_password = os.environ.get("ADMIN_PASSWORD")
    if not correct_password:
        raise HTTPException(
            status_code=500,
            detail="ADMIN_PASSWORD is not set on this deployment -- set it in Railway's Variables tab before using /admin/clinics.",
        )
    username_ok = _secrets.compare_digest(credentials.username, correct_username)
    password_ok = _secrets.compare_digest(credentials.password, correct_password)
    if not (username_ok and password_ok):
        raise HTTPException(
            status_code=401,
            detail="Incorrect username or password",
            headers={"WWW-Authenticate": "Basic"},
        )
    return True


# -------------------------
# Small text-based parsers for the form's textarea fields. Keeping these as
# plain "one thing per line, parts separated by |" rather than a JSON blob
# is deliberate -- it's the format a non-developer can type directly, the
# same spirit as clinic_config.yaml's existing services/dentists lists.
# -------------------------
def _parse_services(text: str):
    services = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) < 2 or not parts[0]:
            continue
        name, price = parts[0], parts[1]
        try:
            price_val = int(price)
        except ValueError:
            try:
                price_val = float(price)
            except ValueError:
                price_val = price
        services.append({"name": name, "price_sar": price_val})
    return services


def _parse_dentists(text: str):
    dentists = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        parts = [p.strip() for p in line.split("|")]
        parts += [""] * (7 - len(parts))
        name, specialization, nationality, languages, qualifications, years, availability = parts[:7]
        if not name:
            continue
        dentists.append({
            "name": name,
            "specialization": specialization,
            "nationality": nationality or None,
            "languages_spoken": languages,
            "qualifications": qualifications,
            "years_experience": int(years) if years.isdigit() else None,
            "availability_schedule": availability,
        })
    return dentists


def _parse_holidays(text: str):
    return [line.strip() for line in (text or "").splitlines() if line.strip()]


def _subscribe_waba(waba_id: str, access_token: str) -> dict:
    """Tells Meta to start sending this WABA's WhatsApp messages to this
    app (i.e. to this same running service) -- the one Graph API call that
    replaces what used to require a whole new Railway service."""
    api_version = os.environ.get("GRAPH_API_VERSION", "v25.0")
    url = f"https://graph.facebook.com/{api_version}/{waba_id}/subscribed_apps"
    try:
        resp = requests.post(url, headers={"Authorization": f"Bearer {access_token}"}, timeout=15)
        try:
            body = resp.json()
        except ValueError:
            body = {"raw": resp.text}
        return {"ok": resp.ok, "status_code": resp.status_code, "body": body}
    except requests.RequestException as e:
        return {"ok": False, "status_code": None, "body": {"error": str(e)}}


# -------------------------
# HTML rendering (plain server-rendered HTML, no build step, no JS
# framework -- kept simple on purpose since this is an internal tool)
# -------------------------
PAGE_STYLE = """
<style>
  body { font-family: -apple-system, Segoe UI, Arial, sans-serif; max-width: 1150px; margin: 24px auto; padding: 0 16px; color: #1a1a1a; }
  .brand-row { display: flex; align-items: center; gap: 16px; }
  .brand-row img { width: 128px; height: 128px; border-radius: 24px; flex: none; }
  h1 { font-size: 22px; margin: 0; }
  h2 { font-size: 18px; margin-top: 36px; border-bottom: 1px solid #ddd; padding-bottom: 6px; }
  table { border-collapse: collapse; width: 100%; margin-top: 8px; }
  th, td { text-align: left; padding: 8px; border-bottom: 1px solid #eee; font-size: 14px; }
  th { background: #f7f7f7; }
  form { margin-top: 12px; }
  label { display: block; font-weight: 600; margin-top: 14px; font-size: 14px; }
  .hint { color: #666; font-weight: 400; font-size: 12px; display: block; margin-top: 2px; }
  input[type=text], input[type=date], select, textarea { width: 100%; padding: 8px; margin-top: 4px; box-sizing: border-box; font-size: 14px; border: 1px solid #ccc; border-radius: 4px; }
  textarea { font-family: monospace; }
  .row { display: flex; gap: 16px; }
  .row > div { flex: 1; }
  button { margin-top: 20px; padding: 10px 18px; font-size: 15px; background: #1a7f37; color: white; border: none; border-radius: 4px; cursor: pointer; }
  button:hover { background: #166a2e; }
  .flash-ok { background: #e6ffed; border: 1px solid #1a7f37; padding: 10px; border-radius: 4px; margin-top: 12px; }
  .flash-err { background: #ffeef0; border: 1px solid #cf222e; padding: 10px; border-radius: 4px; margin-top: 12px; white-space: pre-wrap; }
  .badge-active { color: #1a7f37; font-weight: 600; }
  .badge-inactive { color: #999; }
  .delete-form { margin: 0; }
  .delete-btn { margin: 0; padding: 5px 12px; font-size: 12px; background: #cf222e; }
  .delete-btn:hover { background: #a40e24; }
  .days-ok { color: #1a7f37; font-weight: 600; }
  .days-soon { display: inline-block; background: #fff4e5; color: #9a5b00; border: 1px solid #f0b35b; border-radius: 10px; padding: 1px 8px; font-weight: 600; }
  .days-expired { display: inline-block; background: #ffeef0; color: #cf222e; border: 1px solid #cf222e; border-radius: 10px; padding: 1px 8px; font-weight: 600; }
  .voice-on { color: #1a7f37; font-weight: 600; }
  .voice-off { color: #999; }
  .small-btn { margin: 4px 0 0; padding: 4px 10px; font-size: 12px; background: #555; }
  .small-btn:hover { background: #333; }
  .check-label { display: flex; align-items: flex-start; gap: 8px; font-weight: 600; }
  .check-label input { margin-top: 3px; }
  .voice-number-form input[type=text] { width: 150px; padding: 4px 6px; font-size: 12px; margin-top: 6px; }
  .usage { margin-top: 6px; font-size: 12px; }
  .usage-bar { height: 6px; background: #eee; border-radius: 3px; margin: 3px 0; width: 190px; }
  .usage-bar div { height: 6px; border-radius: 3px; background: #1a7f37; }
  .usage-warn .usage-bar div { background: #d4881a; }
  .usage-over .usage-bar div { background: #cf222e; }
  .usage-warn { color: #9a5b00; }
  .usage-over { color: #cf222e; }
</style>
"""


RENEWAL_WARNING_DAYS = 30


def _voice_plan_html(clinic: dict) -> str:
    """Read-only: the clinic's package + this month's minutes (warns at 80%,
    shows extra minutes and the SAR to add to the invoice) + last month's
    amount to invoice. The package itself is chosen only when the clinic is
    added -- this table is for monitoring, not for changing subscriptions."""
    if not clinic.get("voice_enabled"):
        return ""
    plan = clinics_store.voice_plan_of(clinic)
    picker = f'<div class="hint">Package: <b>{plan["label"]}</b> ({plan["minutes"]:,} min)</div>'
    now = datetime.now()
    u = voice_usage.usage_summary(clinic, now.year, now.month)
    pct = min(100, round(u["fraction"] * 100))
    if u["extra_minutes"]:
        cls, note = "usage-over", (f'⛔ {u["extra_minutes"]} extra min × SAR {plan["overage_sar"]:.2f} '
                                   f'= <b>SAR {u["extra_sar"]:,.2f}</b> to add')
    elif u["fraction"] >= voice_usage.WARN_AT_FRACTION:
        cls, note = "usage-warn", f'⚠️ {u["included"] - u["minutes"]} min left this month'
    else:
        cls, note = "usage-ok", f'{u["included"] - u["minutes"]} min left'
    py, pm = voice_usage.previous_month(now)
    last = voice_usage.usage_summary(clinic, py, pm)
    last_extra = f' (incl. SAR {last["extra_sar"]:,.2f} extra)' if last["extra_minutes"] else ""
    last_html = (f'<span class="hint">{datetime(py, pm, 1).strftime("%b %Y")}: {last["minutes"]} min, '
                 f'invoice SAR {last["total_sar"]:,.2f}{last_extra}</span>')
    return (picker +
            f'<div class="usage {cls}"><b>{now.strftime("%b")}: {u["minutes"]} / {u["included"]} min</b> '
            f'<span class="hint">({u["calls"]} calls)</span>'
            f'<div class="usage-bar"><div style="width:{pct}%"></div></div>{note}</div>{last_html}')


def _days_left_html(clinic: dict) -> str:
    """Days until this clinic's plan runs out, flagged when under a month."""
    left = clinics_store.subscription_days_left(clinic)
    if left is None:
        return '<span class="hint">unknown</span>'
    ends = clinics_store.subscription_ends_on(clinic).strftime("%Y-%m-%d")
    if left <= 0:
        return f'<span class="days-expired">⛔ Expired</span><br><span class="hint">ended {ends}</span>'
    label = f"{left} day{'s' if left != 1 else ''}"
    if left < RENEWAL_WARNING_DAYS:
        return f'<span class="days-soon">⚠️ {label}</span><br><span class="hint">renew by {ends}</span>'
    return f'<span class="days-ok">{label}</span><br><span class="hint">renews {ends}</span>'


def _renewal_summary_html(clinics: list) -> str:
    due = [c for c in clinics if (clinics_store.subscription_days_left(c) or 99999) < RENEWAL_WARNING_DAYS]
    if not due:
        return ""
    names = ", ".join(html.escape(c["name"]) for c in due)
    return (f'<div class="flash-err" style="white-space:normal">⚠️ {len(due)} clinic'
            f'{"s" if len(due) != 1 else ""} due for renewal within a month (or already expired): {names}</div>')


def _render_page(message_html: str = "") -> str:
    clinics = clinics_store.list_clinics()

    rows = ""
    if clinics:
        for c in clinics:
            rows += f"""
            <tr>
                <td>{html.escape(c['name'])}</td>
                <td>{html.escape(c['slug'])}</td>
                <td>{html.escape(c['whatsapp_phone_number_id'])}</td>
                <td>{html.escape(c['subscription_plan'].replace('_', ' '))}<br><span class="hint">since {html.escape(c['subscription_started_at'])}</span></td>
                <td>{_days_left_html(c)}</td>
                <td>
                    <span class="{'voice-on' if c['voice_enabled'] else 'voice-off'}">{'📞 On' if c['voice_enabled'] else 'Off'}</span>
                    <form class="delete-form voice-number-form" method="post" action="/admin/clinics/{c['id']}/voice-number">
                        <input type="text" name="voice_number" value="{('+' + html.escape(c['voice_number'])) if c.get('voice_number') else ''}" placeholder="Voice phone number">
                        <button type="submit" class="small-btn">Save number</button>
                    </form>
                    {_voice_plan_html(c)}
                </td>
                <td class="{'badge-active' if c['active'] else 'badge-inactive'}">{'active' if c['active'] else 'inactive'}</td>
                <td>
                    <a href="/clinic/{c['slug']}/dashboard" target="_blank">Open</a><br>
                    <span class="hint">user: {html.escape(c['slug'])}<br>pass: {html.escape(c.get('dashboard_password') or clinics_store.ensure_dashboard_password(c['id']) or '')}</span>
                </td>
                <td>
                    <form class="delete-form" method="post" action="/admin/clinics/{c['id']}/delete"
                          onsubmit="return confirm('Remove this clinic? This stops its WhatsApp number from replying and deletes its data (patients, appointments, config) from this app. This cannot be undone here -- you will still need to revoke its access token in Meta separately.');">
                        <button type="submit" class="delete-btn">Delete</button>
                    </form>
                </td>
            </tr>"""
    else:
        rows = "<tr><td colspan=9>No clinics yet -- add the first one below.</td></tr>"

    plan_options = "".join(
        f'<option value="{p}">{p.replace("_", " ")}</option>' for p in SUBSCRIPTION_PLAN_CHOICES
    )
    today = datetime.now().strftime("%Y-%m-%d")

    return f"""<!doctype html>
<html>
<head><meta charset="utf-8"><title>Mawaid — Clinics</title>{PAGE_STYLE}</head>
<body>
  <div class="brand-row"><img src="/static/logo.png" alt="Mawaid"><h1>🦷 Mawaid — Clinics running on this service</h1></div>
  {message_html}

  <h2>Current clinics</h2>
  {_renewal_summary_html(clinics)}
  <table>
    <tr><th>Name</th><th>Slug</th><th>WhatsApp Phone Number ID</th><th>Subscription</th><th>Days left before renewal</th><th>Voice calls</th><th>Status</th><th>Staff dashboard</th><th></th></tr>
    {rows}
  </table>

  <h2>Add a new clinic</h2>
  <p class="hint">
    Before filling this in, you need three things from Meta's dashboard for this clinic's WhatsApp number:
    the <b>WhatsApp Business Account (WABA) ID</b>, the <b>Phone Number ID</b>, and a <b>permanent access token</b>
    (a System User token) -- see claude/how-to-add-remove-clinic-whatsapp-number.md for exactly how to get those.
    Everything else below is filled in by you, once, right here.
  </p>
  <form method="post" action="/admin/clinics/add" enctype="multipart/form-data">

    <label>Clinic name
      <input type="text" name="name" required placeholder="Al Waha Dental Clinic">
    </label>

    <div class="row">
      <div>
        <label>WhatsApp Access Token (System User token)
          <input type="text" name="whatsapp_access_token" required>
        </label>
      </div>
      <div>
        <label>WhatsApp Phone Number ID
          <input type="text" name="whatsapp_phone_number_id" required>
        </label>
      </div>
      <div>
        <label>WhatsApp Business Account (WABA) ID
          <input type="text" name="whatsapp_waba_id" required>
        </label>
      </div>
    </div>

    <div class="row">
      <div>
        <label>General hours
          <input type="text" name="general_hours" placeholder="Saturday-Thursday 9:00 AM - 9:00 PM, Friday closed">
        </label>
      </div>
      <div>
        <label>Parking
          <input type="text" name="parking" placeholder="Free parking is available in front of the clinic.">
        </label>
      </div>
    </div>

    <div class="row">
      <div>
        <label>Clinic phone number
          <input type="text" name="phone" placeholder="+966 5X XXX XXXX">
        </label>
      </div>
      <div>
        <label>Google Maps link
          <input type="text" name="google_maps_link" placeholder="https://maps.google.com/?q=...">
        </label>
      </div>
    </div>

    <label>Address
      <input type="text" name="address" placeholder="Street, district, city, Saudi Arabia">
    </label>

    <label>Offers/promotions caption
      <span class="hint">Sent alongside the brochure image when a patient asks about offers.</span>
      <input type="text" name="offers_text" placeholder="Ask our staff about our current promotions!">
    </label>

    <label>Brochure/offers image (optional)
      <span class="hint">Upload the image to send when a patient asks about offers/promotions. Leave empty to add it later.</span>
      <input type="file" name="offers_image" accept="image/*">
    </label>

    <label>Holidays / closures
      <span class="hint">One date per line, format YYYY-MM-DD. Leave blank if none.</span>
      <textarea name="holidays" rows="2" placeholder="2026-09-23"></textarea>
    </label>

    <label>Services and prices
      <span class="hint">One per line: Name | Price in SAR. Example: Teeth Cleaning | 250</span>
      <textarea name="services" rows="6" placeholder="Consultation / Visit Fee | 300&#10;Regular Checkup | 150&#10;Teeth Cleaning | 250"></textarea>
    </label>

    <label>Dentists
      <span class="hint">One per line: Name | Specialization | Nationality | Languages | Qualifications | Years experience | Availability.
      Nationality can be left blank (just leave that spot empty between the | |).
      Example: Dr. Asha Rao | Orthodontist | Indian | English, Hindi | BDS, MDS | 12 | Mon-Fri 10:00-17:00</span>
      <textarea name="dentists" rows="6" placeholder="Dr. Asha Rao | Orthodontist | Indian | English, Hindi | BDS, MDS | 12 | Mon-Fri 10:00-17:00"></textarea>
    </label>

    <div class="row">
      <div>
        <label>Subscription plan
          <select name="subscription_plan">{plan_options}</select>
        </label>
      </div>
      <div>
        <label>Subscription/trial start date
          <input type="date" name="subscription_started_at" value="{today}">
        </label>
      </div>
    </div>

    <label class="check-label">
      <input type="checkbox" name="voice_enabled" value="1">
      <span>Voice call receptionist (phone calls tier)
        <span class="hint">Tick if this clinic has subscribed to the AI phone receptionist on top of WhatsApp.
        Its phone line still needs to be connected to the voice agent (see voice-receptionist notes).</span></span>
    </label>

    <label>Voice package
      <span class="hint">Only pick the size -- the price follows the WhatsApp subscription length chosen above.</span>
      <select name="voice_plan">{"".join(f'<option value="{k}">{v["label"]} ({v["minutes"]:,} min per month)</option>' for k, v in clinics_store.VOICE_PLANS.items())}</select>
    </label>

    <label>Voice phone number (optional)
      <span class="hint">The number patients call to reach this clinic's AI receptionist (the number bought from the
      SIP provider and added in ElevenLabs), e.g. +966 11 234 5678. Calls to this number are answered with this
      clinic's dentists, hours and bookings. Can be added later from the clinics table.</span>
      <input type="text" name="voice_number" placeholder="+966 11 234 5678">
    </label>

    <button type="submit">Save and bring this clinic online</button>
  </form>
</body>
</html>"""


@router.get("/admin/clinics", response_class=HTMLResponse)
async def admin_clinics_page(_: bool = Depends(require_admin)):
    return _render_page()


@router.post("/admin/clinics/add", response_class=HTMLResponse)
async def admin_add_clinic(
    _: bool = Depends(require_admin),
    name: str = Form(...),
    whatsapp_access_token: str = Form(...),
    whatsapp_phone_number_id: str = Form(...),
    whatsapp_waba_id: str = Form(...),
    general_hours: str = Form(""),
    parking: str = Form(""),
    phone: str = Form(""),
    address: str = Form(""),
    google_maps_link: str = Form(""),
    offers_text: str = Form(""),
    holidays: str = Form(""),
    services: str = Form(""),
    dentists: str = Form(""),
    subscription_plan: str = Form("trial"),
    subscription_started_at: str = Form(""),
    voice_enabled: str = Form(""),
    voice_number: str = Form(""),
    voice_plan: str = Form(clinics_store.DEFAULT_VOICE_PLAN),
    offers_image: Optional[UploadFile] = File(None),
):
    if voice_number.strip():
        taken = clinics_store.get_clinic_by_voice_number(voice_number)
        if taken:
            return HTMLResponse(_render_page(
                f'<div class="flash-err">Voice number {html.escape(voice_number)} already belongs to '
                f'{html.escape(taken["name"])}. Nothing was added.</div>'))
        if not clinics_store.normalize_phone_digits(voice_number):
            return HTMLResponse(_render_page(
                f'<div class="flash-err">"{html.escape(voice_number)}" doesn\'t look like a phone number. Nothing was added.</div>'))

    existing = clinics_store.get_clinic_by_phone_number_id(whatsapp_phone_number_id.strip())
    if existing:
        message = f'<div class="flash-err">A clinic is already registered with WhatsApp Phone Number ID {html.escape(whatsapp_phone_number_id)} (clinic: {html.escape(existing["name"])}). Nothing was added.</div>'
        return HTMLResponse(_render_page(message))

    clinic = clinics_store.add_clinic(
        name=name.strip(),
        whatsapp_access_token=whatsapp_access_token.strip(),
        whatsapp_phone_number_id=whatsapp_phone_number_id.strip(),
        whatsapp_waba_id=whatsapp_waba_id.strip(),
        general_hours=general_hours.strip(),
        parking=parking.strip(),
        phone=phone.strip(),
        address=address.strip(),
        google_maps_link=google_maps_link.strip(),
        offers_text=offers_text.strip(),
        holidays=_parse_holidays(holidays),
        services=_parse_services(services),
        dentists=_parse_dentists(dentists),
        subscription_plan=subscription_plan,
        subscription_started_at=subscription_started_at or None,
        voice_enabled=voice_enabled == "1",
        voice_plan=voice_plan,
    )
    if voice_number.strip():
        clinic["voice_number"] = clinics_store.set_voice_number(clinic["id"], voice_number)

    # Save the uploaded brochure image (if any) into this clinic's own
    # static folder, then re-write its clinic_config.yaml so the filename
    # is recorded -- same file the MCP tool subprocess already knows how
    # to serve via get_offers_image_url().
    if offers_image is not None and offers_image.filename:
        safe_ext = re.sub(r"[^a-zA-Z0-9.]", "", os.path.splitext(offers_image.filename)[1]) or ".jpg"
        dest_filename = f"brochure{safe_ext}"
        dest_path = os.path.join(clinic["static_dir"], dest_filename)
        contents = await offers_image.read()
        with open(dest_path, "wb") as f:
            f.write(contents)
        clinic["offers_image_filename"] = dest_filename
        clinics_store.update_offers_image_filename(clinic["id"], dest_filename)
        clinics_store.write_clinic_config_yaml(clinic)

    subscribe_result = _subscribe_waba(clinic["whatsapp_waba_id"], clinic["whatsapp_access_token"])

    launch_clinic_worker(clinic)

    dashboard_note = (
        f'Staff dashboard: <a href="/clinic/{clinic["slug"]}/dashboard" target="_blank">/clinic/{clinic["slug"]}/dashboard</a> '
        f'-- login user: <b>{clinic["slug"]}</b>, password: <b>{html.escape(clinic["dashboard_password"])}</b> '
        f'(give these to this clinic\'s reception; also always visible in the table above).'
    )

    if subscribe_result["ok"]:
        message = (
            f'<div class="flash-ok">✅ {html.escape(clinic["name"])} was added and is now LIVE on this service '
            f'-- its WhatsApp number will start replying immediately. No restart or redeploy needed.<br><br>{dashboard_note}</div>'
        )
    else:
        message = (
            f'<div class="flash-err">⚠️ {html.escape(clinic["name"])} was added and its worker was started, '
            f"but subscribing its WABA to this app failed (this usually means the access token or WABA ID is "
            f"wrong, or the token doesn't have the whatsapp_business_management permission on it):\n"
            f"{html.escape(str(subscribe_result['body']))}\n\n"
            f"Fix the token/WABA ID and re-subscribe manually (see check_subscription.py), or remove and re-add "
            f"this clinic once you have the right credentials.<br><br>{dashboard_note}</div>"
        )

    return HTMLResponse(_render_page(message))


@router.post("/admin/clinics/{clinic_id}/voice-number", response_class=HTMLResponse)
async def admin_set_voice_number(clinic_id: int, voice_number: str = Form(""), _: bool = Depends(require_admin)):
    clinic = clinics_store.get_clinic(clinic_id)
    if not clinic:
        return HTMLResponse(_render_page('<div class="flash-err">No clinic with that id. Nothing changed.</div>'))
    if voice_number.strip() and not clinics_store.normalize_phone_digits(voice_number):
        return HTMLResponse(_render_page(
            f'<div class="flash-err">"{html.escape(voice_number)}" doesn\'t look like a phone number. Nothing changed.</div>'))
    try:
        saved = clinics_store.set_voice_number(clinic_id, voice_number.strip() or None)
    except ValueError as e:
        return HTMLResponse(_render_page(f'<div class="flash-err">{html.escape(str(e))} Nothing changed.</div>'))
    if saved:
        msg = f'📞 Calls to +{saved} will now be answered as {html.escape(clinic["name"])}.'
        if not clinic["voice_enabled"]:
            msg += ' Note: this clinic is not subscribed to voice calls, so calls to it will not be answered.'
    else:
        msg = f'Voice number removed for {html.escape(clinic["name"])}.'
    return HTMLResponse(_render_page(f'<div class="flash-ok">{msg}</div>'))


@router.post("/admin/clinics/{clinic_id}/delete", response_class=HTMLResponse)
async def admin_delete_clinic(clinic_id: int, _: bool = Depends(require_admin)):
    clinic = clinics_store.get_clinic(clinic_id)
    if not clinic:
        message = '<div class="flash-err">No clinic with that id (maybe already removed?). Nothing changed.</div>'
        return HTMLResponse(_render_page(message))

    stop_clinic_worker(clinic_id)
    clinics_store.remove_clinic(clinic_id)

    message = (
        f'<div class="flash-ok">🗑️ {html.escape(clinic["name"])} was removed from this app -- its worker was '
        f"stopped and its data (patients, appointments, config) was deleted. Its WhatsApp access token and phone "
        f"number are untouched on Meta's side -- revoke/disconnect those yourself in Meta's Business Settings if "
        f"this clinic is gone for good (see claude/how-to-add-remove-clinic-whatsapp-number.md).</div>"
    )
    return HTMLResponse(_render_page(message))

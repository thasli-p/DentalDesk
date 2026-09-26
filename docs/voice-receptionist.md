# Voice receptionist — Mawaid ↔ ElevenLabs

The phone-call agent runs on ElevenLabs. During a call it calls these Mawaid
endpoints ("server tools") to read real clinic data and make real bookings in
the same database Sia uses on WhatsApp. Code: `src/app/voice.py`.

## One-time setup on Railway
Add a variable `VOICE_API_KEY` = a long random secret (e.g. 40 random
letters/numbers). Without it, all `/voice/...` endpoints stay switched off.

## Endpoints
Base: `https://dentaldesk-production-6ade.up.railway.app/voice/<clinic-slug>`
Every request needs the header `X-Voice-Key: <VOICE_API_KEY>`.

| Tool name (ElevenLabs) | Method + path | Body (JSON) |
|---|---|---|
| get_clinic_context | GET `/context?caller_phone=<caller ID>` | — |
| check_slot | POST `/check-slot` | `appointment_time`, `dentist_id` (optional: empty = any free dentist) |
| book_appointment | POST `/book` | `caller_phone`, `patient_name`, `dentist_id`, `appointment_time`, `service_name`, `patient_gender`, `call_id` (last three optional) |
| find_appointments | POST `/appointments` | `caller_phone` |
| cancel_appointment | POST `/cancel` | `caller_phone`, `appointment_id` |
| reschedule_appointment | POST `/reschedule` | `caller_phone`, `appointment_id`, `new_appointment_time` |
| flag_for_staff | POST `/flag` | `caller_phone`, `reason`, `patient_name` (optional) |

Times are ISO 8601 in Riyadh time, e.g. `2026-09-27T16:00:00`.

## Built-in safety checks
- Phone numbers are normalised to WhatsApp format (`9665XXXXXXXX`), so a phone
  booking and a WhatsApp chat from the same person share one patient record.
  Anything that isn't a valid 10-digit `05…` number is rejected with
  `invalid_phone` so the agent asks again.
- No booking in the past, on a clinic holiday, outside the clinic's hours,
  outside the dentist's own working days/hours, or in a slot already taken.
  Hours are read from the free text in the admin page ("Mon-Fri 10:00-17:00",
  "Saturday-Thursday 9:00 AM - 9:00 PM, Friday closed", "السبت-الخميس 4م-10م").
  Text the server can't read is not enforced. When a time doesn't work, the
  reply includes the real reason and the next free times to offer.
- One phone can belong to a whole family: each person gets their own patient
  record under the same number (matched by name), so a wife booking from her
  husband's phone is booked under her own name. Anyone on the number can see,
  cancel or reschedule that number's appointments.
- `get_clinic_context` receives the caller ID and returns who is registered on
  that number (name + male/female) and their upcoming appointments, so the
  agent can greet returning callers properly (after confirming who is speaking).
- A caller can only see, cancel or reschedule appointments under their own number.
- Flags appear in the staff dashboard's "Needs attention" list, marked 📞.

## Which clinic a call belongs to
One shared ElevenLabs agent serves every clinic. Each tool URL has the number
the patient DIALLED where the clinic goes:
`/voice/{called_number}/<tool>`, with the path parameter `called_number` filled
from the dynamic variable `system__called_number`. Mawaid matches it against
each clinic's **Voice phone number** (set in `/admin/clinics`). A clinic slug
also works in that spot. No dialled number (e.g. the web test link) or an
unregistered number falls back to `VOICE_DEFAULT_CLINIC_SLUG`. The post-call
log is routed the same way. Voice must also be turned ON for the clinic.

### Adding voice for a new clinic
1. SIP provider: buy the number, assign it to the existing SIP connection that
   points to `sip.rtc.elevenlabs.io`.
2. ElevenLabs: Phone Numbers → Import (SIP trunk) → assign to the shared agent.
3. `/admin/clinics`: turn Voice on for the clinic and save its voice number.
4. Optional: the clinic forwards its usual landline to the new number.

## Calls on the staff dashboard
ElevenLabs sends every finished call to `POST /voice/post-call` (a workspace
"post-call webhook"). Mawaid stores it as a 📞 conversation with the full
transcript, linked to the caller's patient record, so it shows up in "Recent
conversations" (booked / flagged badges work the same as WhatsApp).
- Railway variables: `VOICE_WEBHOOK_SECRET` (the webhook's HMAC secret from
  ElevenLabs) and `VOICE_DEFAULT_CLINIC_SLUG` (which clinic calls belong to
  during the pilot).
- `book_appointment` and `flag_for_staff` also receive the call's ID
  (`system__conversation_id`), so a booking or flag made during a call lands
  in that same 📞 conversation.

## Phone line
Pilot: Telnyx US number +1 641 461 0793 → SIP trunk → ElevenLabs (caller ID
arrives as `system__caller_id`). A Saudi number needs the CR first.

## Not done yet
- Routing by dialled number (called number → clinic slug) for several clinics.

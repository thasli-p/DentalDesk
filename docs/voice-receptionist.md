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
| get_clinic_context | GET `/context` | — |
| check_slot | POST `/check-slot` | `dentist_id`, `appointment_time` |
| book_appointment | POST `/book` | `caller_phone`, `patient_name`, `dentist_id`, `appointment_time`, `service_name` (optional) |
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
- No booking in the past, on a clinic holiday, or in a slot already taken.
- A caller can only see, cancel or reschedule appointments under their own number.
- Flags appear in the staff dashboard's "Needs attention" list, marked 📞.

## Which clinic a call belongs to
Each endpoint URL contains the clinic's slug. For the pilot, the agent's tool
URLs use one clinic's slug directly. When real phone lines are connected, the
slug will come from the number the patient dialled (one shared agent for all
clinics).

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

## Not done yet
- Real phone line (SIP / Saudi number) and using caller ID for `caller_phone`.

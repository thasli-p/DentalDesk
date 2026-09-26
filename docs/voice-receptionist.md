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

## Not done yet
- Calls don't appear in the dashboard's conversation list yet (only their
  bookings and flags do). Next: ElevenLabs post-call webhook → store transcript.
- Real phone line (SIP / Saudi number) and using caller ID for `caller_phone`.

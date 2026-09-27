# Clinic types & departments

Mawaid is no longer dental-only. Each clinic has a **type** and can have **extra departments** on the same WhatsApp number.

## The rule

- **One WhatsApp number = one clinic = one base subscription.** The base subscription covers the clinic's main department (its type). When it ends, the whole number stops answering (unchanged).
- **Each extra department has its own plan and start date.** When a department's plan ends, Sia stops booking that department and tells patients to call the clinic; everything else keeps working.
- An owner with several numbers has several clinics and several subscriptions. To pay once, they move to one number.

## Types

`dental`, `dermatology`, `laser`, `beauty` (see `src/shared/clinic_types.py`). Each type has a short rule pack for Sia: how patients word services, what to check before booking, and which symptoms are emergencies. Sia never decides medical suitability; anything that needs a clinician goes to `flag_for_staff`.

A plain dental clinic (every clinic that existed before this) gets exactly the same system prompt, tool output, dashboard and voice replies as before.

## Admin

- **Add clinic** form: clinic type, "main department serves women only", and an optional extra-departments box:
  `Name | Type | Plan | Start date | yes` (the last part = women only).
- Services take an optional third part: `Laser Full Body | 900 | Laser`.
- Practitioners take two optional extra parts: `... | Availability | Department | Gender`.
- **Edit type, departments & staff** (`/admin/clinics/<id>/setup`, linked from the clinics table) changes an existing clinic: type, departments (also how a department is renewed), services and practitioners. Saving restarts that clinic's Sia (a few seconds, no redeploy). Removed practitioners are marked inactive, not deleted, so past appointments keep their name.
- Editing the original carried-over clinic moves its config file from the app folder to its own folder on the persistent volume (hours, address, offers are kept), so its settings survive redeploys.

## What Sia does differently (non-dental or multi-department clinics only)

- Works out which department the patient needs and uses `list_dentists(department=...)`.
- `book_appointment` refuses: a department whose plan has ended (`department_unavailable`), and a women-only department for a male patient (`women_only_department`).
- Honours "I want a female doctor" using practitioner gender.

## Dashboard

For clinics with several departments: a department selector (filters bookings, schedule and booking charts; conversation numbers always cover the whole clinic), a department column in the schedule, and a "Bookings by department" chart. Non-dental clinics say "practitioner" instead of "dentist". API: `&dept=<key>`.

## Voice

`/voice/.../context` lists departments and each practitioner's department; booking a practitioner whose department plan ended returns `department_unavailable`. The ElevenLabs agent prompt itself (outside this repo) is still dental; update it before selling voice to a non-dental clinic.

## Not built yet

Machines/rooms as a booking limit, service durations, session packages, per-department hours, per-department pricing in the P&L model.

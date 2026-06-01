# List Build Policy (v8, neutral template)

You are generating candidate companies for JP B2B outreach.

## Goal
Only include a company when you have verified a **B2B inquiry form URL** that:

- is intended for business/sales/partnership/media contact
- contains a free-form inquiry textarea (message body)
- is not a non-contact flow (recruit/IR/B2C support/reservation/login/download gate)

## form_url Rules

- `form_url` must be a verified URL (opened and checked), never guessed.
- Add `form_url_verified: true` only after opening the page and checking:
  - textarea exists and is a message/inquiry field
  - page context is B2B contact, not excluded categories
- If verification fails, leave `form_url` empty and set `category` to a non-B2B reason.

## Explicit Exclusions (do not use as form_url)

- Recruit/career/job entry pages
- IR/investor pages
- B2C support/help/repair/returns pages
- Reservation/booking pages
- Download gate / newsletter / member signup / login pages

## URL Heuristics

Prefer paths like:
- `/contact`, `/inquiry`, `/toiawase`, `/otoiawase`, `/company/contact`, `/business/contact`, `/form`

Avoid paths like:
- `/recruit`, `/career`, `/entry`, `/ir`, `/support`, `/faq`, `/reserve`, `/yoyaku`

## Output discipline

- No invented company names or URLs.
- Keep evidence concise and factual.
- If uncertain, mark as non-B2B category rather than forcing `form_url`.

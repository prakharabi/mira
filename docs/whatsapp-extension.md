# WhatsApp extension ↔ Mira contract

Mira decides **what** to send and **when**. The extension, running on WhatsApp
Web, only does the sending and reports what happened. Every rule lives on
Mira's side: sending hours, daily cap, the gap between messages, who gets
what, and stopping a sequence when someone replies. The extension never needs
to know about campaigns.

Base URL: `http://localhost:11200` (the Mira daemon).

## Authentication

Every request carries the token from **Mira → Outreach → Setup → WhatsApp →
Extension token**:

```
X-Mira-Token: <token>
```

A missing or wrong token gets `401`. "New token" in Setup revokes the old one.

The daemon sends no CORS headers, so make these calls from the extension's
**background service worker** with `host_permissions` for
`http://localhost:11200/*`, not from a content script.

## 1. Ask for the next message

```
GET /outreach/whatsapp/next
```

```json
{ "message": { "id": "3f2a…", "phone": "919845012345", "text": "Hi Smile Dental team, …",
               "business": "Smile Dental", "test": false } }
```

or, when there's nothing to send right now:

```json
{ "message": null, "reason": "waiting between messages", "retry_after": 112 }
```

`reason` is one of: `WhatsApp outreach is off in Mira`, `outreach sending is
switched off`, `outside sending hours`, `daily limit reached`, `waiting between
messages` (with `retry_after` in seconds), or `nothing due`.

- `phone` is digits only, in international form without `+`, and ready for
  `https://web.whatsapp.com/send?phone=<phone>&text=<urlencoded text>`.
- Poll every 30–60 seconds, or after `retry_after`.
- A handed-out message is reserved for 10 minutes. If you never report back
  (for example the browser was closed), it goes back in the queue.
- Test messages (`"test": true`) go to the user's own number and skip the
  hours, caps and gaps.

## 2. Report the result

```
POST /outreach/whatsapp/sent
Content-Type: application/json

{ "id": "3f2a…", "ok": true }
```

On failure:

```json
{ "id": "3f2a…", "ok": false, "error": "send button not found" }
```

If the number isn't on WhatsApp (WhatsApp Web shows "Phone number shared via
url is invalid"), say so. Mira then marks the lead and moves its sequence on
instead of retrying:

```json
{ "id": "3f2a…", "ok": false, "not_on_whatsapp": true, "error": "Phone number shared via url is invalid" }
```

Other failures are retried up to 3 times.

## 3. Forward incoming messages

```
POST /outreach/whatsapp/incoming
Content-Type: application/json

{ "phone": "+91 98450 12345", "text": "Yes, send me the details", "id": "<whatsapp message id>", "name": "Smile Dental" }
```

```json
{ "matched": true, "business": "Smile Dental", "label": "interested", "log_id": "…" }
```

- `phone` in any format: matching uses the last 10 digits.
- `id` (optional but recommended): the WhatsApp message id. Posting the same
  id twice is ignored, so it's safe to re-send after a reconnect.
- It's fine to forward every incoming message. Anything from a number that
  isn't a lead in a campaign returns `{"matched": false}` and isn't logged.
- Mira classifies the reply (interested, question, not_interested, unsubscribe,
  auto_reply, other), stops that business's sequence, logs it in the same
  timeline as their emails, and tells the user.

## Suggested extension loop

```
every 30–60s (or after retry_after):
  next = GET /next
  if next.message:
      open chat for next.message.phone with next.message.text
      press send, wait for the tick
      POST /sent {id, ok: true}      (or ok: false with the error / not_on_whatsapp)

on every new incoming message in any chat:
  POST /incoming {phone, text, id, name}
```

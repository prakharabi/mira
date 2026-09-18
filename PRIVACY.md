# Privacy Policy

Mira is a local-first personal assistant. This page exists mainly to satisfy
Google's OAuth consent screen requirements for the Google integration
(Gmail, Calendar, Drive) — it describes what that integration actually does,
in plain terms.

## Who this covers

Mira is open-source software you run yourself, on your own Mac, using your
own Google Cloud OAuth client (Settings → Google). There is no shared or
centrally hosted Mira service, no Mira-operated backend, and no Mira company
collecting data from anyone. Each user's own Google Cloud project is the
one requesting their own data, on their own behalf.

## What Google data Mira accesses

With your explicit consent via Google's own OAuth screen, Mira can:

- **Read Gmail** (`gmail.readonly`) — to search your mail and show you results.
- **Create Gmail drafts** (`gmail.compose`) — to prepare emails for your review.
  Mira never sends email on your behalf; it only ever creates drafts.
- **Read and manage Calendar** (`calendar`) — to list, create, update, and
  cancel events (e.g. scheduling a meeting with a Google Meet link).
- **Read Drive file metadata** (`drive.readonly`) — to search and reference
  your files.
- **Read your account email address** (`userinfo.email`) — to identify which
  account is connected.

## Where this data goes

- OAuth tokens are stored in a single local file on your own Mac
  (`daemon/google_tokens.json`), excluded from version control, and never
  transmitted anywhere except directly to Google's own token endpoint to
  refresh access.
- Data fetched from Gmail/Calendar/Drive is used locally to answer your
  requests and is not sent to any third party by default.
- If you've separately configured a cloud AI provider (e.g. Groq) or a web
  search provider (e.g. Tavily) in Settings, content relevant to a given
  request may be sent to that provider to generate a response — the same
  way it would be if you used their service directly. This is optional and
  under your control via Settings; Mira can run entirely on local models
  with nothing sent anywhere.
- Nothing is sold, shared with advertisers, or used for anything other than
  fulfilling your own requests back to you.

## Revoking access

You can disconnect Google access at any time from Mira's Settings → Google
→ Disconnect, or directly via your [Google Account's connected apps
page](https://myaccount.google.com/permissions).

## Source

Mira's full source, including exactly how this data is used, is public:
https://github.com/prakharabi/mira

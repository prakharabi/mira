# Connecting a Google account

Mira talks to Gmail, Calendar and Drive through **your own** OAuth client, not a
shared one. Nothing is routed through the project, and the tokens never leave
your machine — they sit in `daemon/google_tokens.json`, which is gitignored.

The cost of that is about ten minutes of Google Cloud setup, once.

## 1. Create a Google Cloud project

Go to [console.cloud.google.com](https://console.cloud.google.com) and create a
project (or reuse one). The name is only ever shown to you.

## 2. Enable the three APIs

**APIs & Services → Library**, then search for and enable each of:

- **Gmail API**
- **Google Calendar API**
- **Google Drive API**

Enabling is per-project and takes a few seconds each. If you skip one, that
feature returns a "has not been used in project" error later; the other two
still work.

## 3. Configure the consent screen

**APIs & Services → OAuth consent screen** (newer consoles call this
**Google Auth Platform → Branding**).

- User type: **External**. "Internal" only exists if you have Google Workspace,
  and External is correct for a personal account.
- App name and support email: anything. You are the only user.
- **Add yourself as a Test user.** This is the step people miss. While the app
  is in *Testing* status, only listed test users can authorize it — everyone
  else gets "Access blocked: has not completed the Google verification process".

You do **not** need to submit anything for verification.

### The seven-day catch

An app left in **Testing** status gets refresh tokens that **expire after seven
days**. Mira will simply show as disconnected and you reconnect. If that annoys
you, set the publishing status to **In production**. You will then see an
"Google hasn't verified this app" warning on the consent screen once — click
**Advanced → Go to \<app name\> (unsafe)** — and after that tokens stop
expiring. That warning is expected for any unverified personal OAuth client.

Publishing asks you to fill in **Branding** first. Only three fields are
actually required:

| Field | What to put |
|---|---|
| App name | `Mira` |
| User support email | your own address (pick it from the dropdown) |
| Developer contact information | the same address |

**Leave the app logo empty.** Uploading one is what turns a quiet unverified
app into one that needs Google's brand verification review — a multi-week
process with nothing to gain when you are the only user. The consent screen
just shows your app name instead of an icon.

Leave **Application home page**, **Privacy policy** and **Terms of service**
blank too. They are optional while unverified, and filling them in forces you
to add Authorized domains, which means proving domain ownership in Search
Console. There is no reason to do that for a client only you will ever use.

After saving, go back to the audience/publishing page and press **Publish app**.
Google will warn that verification is required for sensitive scopes — that is
expected. Your own account keeps working; unverified apps are simply capped at
100 users, which is 99 more than you need.

If none of this appeals, staying in Testing is a perfectly reasonable choice.
The only cost is reconnecting on the Google tab about once a week.

## 4. Create the OAuth client

**APIs & Services → Credentials → Create credentials → OAuth client ID**

Application type: **Desktop app**. This matters and is not interchangeable.

Mira runs a one-shot local web server on a random free port and uses
`http://127.0.0.1:<port>/callback` as the redirect URI. Google permits *any*
loopback port for Desktop clients without registering it in advance. A **Web
application** client requires every redirect URI to be registered exactly, and
since the port changes each time, the sign-in will fail with `redirect_uri_mismatch`.

There is no redirect URI field to fill in for a Desktop client. That is correct.

Copy the **Client ID** and **Client secret**.

## 5. Enter them in Mira

Settings → Connected Services → Google account. Paste both, press
**Save credentials**.

## 6. Connect

Go to the **Google** tab and press Connect. Your browser opens Google's consent
screen; approve it, and the tab should flip to connected with your address
shown.

## What Mira asks for

| Scope | Why |
|---|---|
| `gmail.readonly` | reading and summarizing mail |
| `gmail.compose` | preparing drafts |
| `calendar` | reading events, creating them on request |
| `drive.readonly` | finding and reading your files |
| `userinfo.email` | showing which account is connected |

Note what is absent: **`gmail.send`**. Mira can write a draft but cannot send
mail on your behalf. An assistant that can silently send mail as you is a much
larger trust ask, and the draft lands in Gmail for you to review and send.

## If it goes wrong

**"Access blocked … verification process"** — you are not on the test user list,
or the consent screen was never configured. Back to step 3.

**`redirect_uri_mismatch`** — the client is a Web application, not a Desktop
app. Create a new Desktop client; you cannot change the type of an existing one.

**"Google did not return a refresh token"** — rare, and usually a half-finished
earlier authorization. Disconnect in the Google tab and connect again; Mira
forces the consent screen every time specifically to avoid this.

**Connected, but a feature errors with "API has not been used"** — that API was
not enabled in step 2.

**Disconnects roughly weekly** — the seven-day Testing expiry above.

## Disconnecting

The Google tab's Disconnect revokes the refresh token with Google and deletes
the local copy. To go further, delete the OAuth client in the Cloud console.

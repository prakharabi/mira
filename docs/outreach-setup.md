# Setting up outreach

Outreach turns a lead list (from "find dentists in Indiranagar, Bangalore") into
a sequence of emails and WhatsApp messages, and logs every reply. Everything is
in **Mira → Outreach**.

## 1. The outreach mailbox

Use a **separate Gmail account with your brand name**, not your personal one.
Cold email that gets marked as spam hurts the reputation of the address it came
from; keeping it on its own account keeps your real inbox safe.

In that Gmail account:

1. **Turn on 2-Step Verification**: Google Account → Security.
2. **Create an app password**: Google Account → Security → App passwords.
   Name it "Mira" and copy the 16-character password.
3. **Turn on IMAP**: Gmail → Settings → See all settings → Forwarding and
   POP/IMAP → Enable IMAP. This is how Mira reads replies.

Then in **Mira → Outreach → Setup → Outreach mailbox**, enter the Gmail
address and app password, your **From name** (used as `{my_name}`), a
signature, and **Send test emails to** (your own address). Press **Check
connection**.

Mira sends over SMTP and reads the inbox over IMAP. It opens the inbox
read-only, so replies still show as unread in Gmail. Sent campaign emails
appear in that account's Sent folder like any other mail.

## 2. WhatsApp (optional)

WhatsApp messages are sent by a browser extension on WhatsApp Web, which asks
Mira what to send next and reports replies back. See
[whatsapp-extension.md](whatsapp-extension.md) for how it talks to Mira.

In **Setup → WhatsApp**: switch it on, add your own number under **Send test
messages to**, check the country code (91 by default, used to turn numbers like
`080 2222 3333` into `918022223333`), and copy the **extension token** into the
extension.

If WhatsApp is off, WhatsApp steps are skipped.

## 3. Limits and sending hours

- **Emails per day / WhatsApp per day**: hard caps.
- **Warm up a new mailbox**: starts at 10 emails a day and adds 5 a day until
  it reaches your cap. Leave this on for a new address.
- **Sending hours and days**: nothing goes out outside them (10:00–18:00,
  Monday–Saturday by default).
- **Gap between sends**: a random wait between messages (90–240 seconds by
  default).

## 4. A campaign

**Outreach → Campaigns → New campaign**:

- **Lead list** and which leads to include (all, only with email, only with a
  phone, only without a website, minimum rating).
- **Your offer**, **proof point** and **call to action**: written once, used
  in the messages as `{offer}`, `{proof}` and `{cta}`. They can use the
  per-business placeholders too, e.g. "3 reel ideas for {business}".
- **Sequence**: the default is the one you asked for:

  | Step | Day | Channel | |
  |---|---|---|---|
  | 1 | 0 | Email | first touch, with a subject |
  | 2 | 1 | WhatsApp | |
  | 3 | 4 | Email | no subject, so it's a reply in the same thread |
  | 4 | 9 | Email | same thread, last message |

  Days count from the previous step actually going out. A lead with no email
  gets its first WhatsApp straight away instead of waiting a day.
- **Personalise each message**: Mira rewrites your template for each business
  using its name, rating, reviews and area. It always keeps your offer, links
  and name. If the rewrite drops any of them, or the model isn't available, your
  template is sent as written.
- **Placeholders**: `{business}`, `{business_type}`, `{category}`, `{area}`,
  `{rating}`, `{reviews}`, `{website}`, `{my_name}`, `{offer}`, `{proof}`,
  `{cta}`. Click one in the editor to insert it. Mira refuses to save a
  placeholder it doesn't know, so something like `{first_name}` never goes
  out literally.

Press **Preview** to see every step written for one real lead (nothing is sent).

## 5. Test, then go automatic

1. **Send test to me**: every step, written for two real leads, goes to *your*
   test email and WhatsApp. The subject says `[TEST · business · step]`.
   Nothing goes to any business.
2. **Start**: a campaign can't start until it has been tested (the button asks
   first if you really want to skip). Set it to **Automatically** or to **Only
   after I approve each message**. Approved messages wait in the **Review** tab.
3. Switch **sending on** with the toggle at the top of the Outreach view.

You can also run all of this by voice, in chat or on Telegram: "send a test of
the dentists campaign", "start the dentists campaign", "pause all outreach",
"show me the replies", "mark Smile Dental as won".

## What happens to replies

Every reply, by email or WhatsApp, is logged against the business and
classified:

| Reply | What Mira does |
|---|---|
| Interested / question / other | stops their sequence, stage → *interested* or *replied*, tells you (Telegram or voice) |
| Not interested | stops their sequence, stage → *lost* |
| STOP / unsubscribe | stops everything, adds them to the do-not-contact list for every future campaign |
| Out of office | pushes their next step back 3 days |
| Bounce | marks that address bad; other channels continue |

The **Replies** tab lists them. **Conversation** shows the full timeline for a
business across both channels, and lets you set its stage (e.g. *won*).

## Where the data lives

`daemon/outreach/`: config, campaigns, the message log and the do-not-contact
list, all gitignored like the rest of your personal data.

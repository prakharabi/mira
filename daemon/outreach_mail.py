"""The outreach mailbox -- the separate address campaigns send from, and whose
inbox is watched for replies.

Deliberately not google_integration.py. That connection is the user's own
account, is single-account by design, and can only create drafts -- an
assistant that silently sends from your personal address is the trust ask it
was built to avoid. Outreach is the opposite case: a second, brand-name
mailbox that exists to send, where a bad week of cold email can only ever hurt
that address's reputation, not the user's real one.

So this speaks plain SMTP and IMAP with an app password. That works with a
Gmail account (Google Account > Security > 2-Step Verification > App
passwords) and with any other provider, and it needs no OAuth client, no
consent screen, and none of the seven-day token expiry an unpublished Google
app is subject to.
"""

import email
import imaplib
import re
import smtplib
import ssl
from email.header import decode_header, make_header
from email.message import EmailMessage
from email.utils import formataddr, formatdate, getaddresses, make_msgid, parseaddr

TIMEOUT = 30
MAX_FETCH = 50          # new messages read per poll; the rest wait for the next one
MAX_BODY_CHARS = 4000


class MailboxError(Exception):
    pass


class BadRecipient(MailboxError):
    """The address itself is bad -- retrying won't help, skipping it will."""


def _require(cfg: dict):
    if not cfg.get("email_address") or not cfg.get("email_app_password"):
        raise MailboxError("The outreach mailbox isn't set up. Add its address and app "
                           "password in Outreach > Setup.")


def _smtp(cfg: dict):
    _require(cfg)
    host, port = cfg.get("smtp_host") or "smtp.gmail.com", int(cfg.get("smtp_port") or 465)
    ctx = ssl.create_default_context()
    try:
        if port == 465:
            server = smtplib.SMTP_SSL(host, port, timeout=TIMEOUT, context=ctx)
        else:
            server = smtplib.SMTP(host, port, timeout=TIMEOUT)
            server.starttls(context=ctx)
        # Google shows app passwords as four groups of four; pasted with the
        # spaces, they fail to authenticate with no hint why.
        server.login(cfg["email_address"], cfg["email_app_password"].replace(" ", ""))
        return server
    except smtplib.SMTPAuthenticationError as e:
        raise MailboxError("The mailbox rejected the app password. Make sure it's an app "
                           "password (not the account password) and that IMAP is enabled "
                           "in Gmail settings.") from e
    except (smtplib.SMTPException, OSError) as e:
        raise MailboxError(f"Could not connect to {host}:{port}: {e}") from e


def _imap(cfg: dict):
    _require(cfg)
    host, port = cfg.get("imap_host") or "imap.gmail.com", int(cfg.get("imap_port") or 993)
    try:
        conn = imaplib.IMAP4_SSL(host, port, ssl_context=ssl.create_default_context(),
                                 timeout=TIMEOUT)
        conn.login(cfg["email_address"], cfg["email_app_password"].replace(" ", ""))
        return conn
    except imaplib.IMAP4.error as e:
        raise MailboxError(f"IMAP login failed: {e}. In Gmail, IMAP must be enabled under "
                           "Settings > Forwarding and POP/IMAP.") from e
    except OSError as e:
        raise MailboxError(f"Could not connect to {host}:{port}: {e}") from e


def check(cfg: dict) -> dict:
    """Log in to both sides, so a wrong password shows up in Setup rather than
    as the first campaign silently sending nothing."""
    out = {"smtp": "ok", "imap": "ok"}
    try:
        _smtp(cfg).quit()
    except MailboxError as e:
        out["smtp"] = str(e)
    try:
        conn = _imap(cfg)
        conn.logout()
    except MailboxError as e:
        out["imap"] = str(e)
    out["ok"] = out["smtp"] == "ok" and out["imap"] == "ok"
    return out


def send(cfg: dict, to: str, subject: str, body: str,
         in_reply_to: str = "", references: list = None) -> str:
    """Send one plain-text email and return its Message-ID -- the handle a reply
    is matched back to this lead by."""
    domain = cfg["email_address"].split("@")[-1] if "@" in cfg.get("email_address", "") else None
    msg = EmailMessage()
    msg["From"] = formataddr((cfg.get("from_name") or "", cfg.get("email_address", "")))
    msg["To"] = to
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=domain)
    # A one-click way out makes a complaint less likely than a spam button
    # press, and mailbox providers look for it on anything that reads as bulk.
    msg["List-Unsubscribe"] = f"<mailto:{cfg.get('email_address', '')}?subject=unsubscribe>"
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = " ".join(references or [in_reply_to])
    msg.set_content(body)

    server = _smtp(cfg)
    try:
        server.send_message(msg)
    except smtplib.SMTPRecipientsRefused as e:
        raise BadRecipient(f"{to} was refused: {e}") from e
    except (smtplib.SMTPException, OSError) as e:
        raise MailboxError(f"Sending failed: {e}") from e
    finally:
        try:
            server.quit()
        except (smtplib.SMTPException, OSError):
            pass
    return msg["Message-ID"]


# ---------- reading replies ----------

def _decode(value) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except (UnicodeDecodeError, LookupError, ValueError):
        return str(value)


_QUOTE_MARKERS = re.compile(
    r"^(On .{5,200} wrote:|-----\s*Original Message\s*-----|From: .+|Sent from my .+|"
    r"________________________________)\s*$", re.M | re.I)


def _strip_quoted(text: str) -> str:
    """Keep what the person actually wrote, not our own message quoted back
    underneath it -- that's what gets classified and shown."""
    m = _QUOTE_MARKERS.search(text)
    if m:
        text = text[:m.start()]
    lines = [l for l in text.splitlines() if not l.lstrip().startswith(">")]
    return "\n".join(lines).strip()


def _body_text(msg) -> str:
    plain, html = "", ""
    for part in msg.walk():
        if part.is_multipart() or part.get_content_disposition() == "attachment":
            continue
        ctype = part.get_content_type()
        try:
            payload = part.get_payload(decode=True) or b""
            text = payload.decode(part.get_content_charset() or "utf-8", "replace")
        except (LookupError, AttributeError):
            continue
        if ctype == "text/plain" and not plain:
            plain = text
        elif ctype == "text/html" and not html:
            html = text
    if not plain and html:
        plain = re.sub(r"<(br|/p|/div)[^>]*>", "\n", html, flags=re.I)
        plain = re.sub(r"<[^>]+>", "", plain)
        plain = re.sub(r"&nbsp;", " ", plain)
    return plain


def _ids(value: str) -> list:
    return re.findall(r"<[^>]+>", value or "")


def _bounce_info(msg) -> tuple:
    """(is_bounce, [failed recipients], [original message ids])."""
    sender = parseaddr(msg.get("From", ""))[1].lower()
    ctype = msg.get_content_type()
    is_bounce = (sender.startswith(("mailer-daemon@", "postmaster@"))
                 or (ctype == "multipart/report"
                     and "delivery-status" in msg.get_param("report-type", "")))
    if not is_bounce:
        return False, [], []
    failed = [a.strip().lower() for a in (msg.get("X-Failed-Recipients") or "").split(",") if a.strip()]
    original_ids = []
    for part in msg.walk():
        ptype = part.get_content_type()
        if ptype == "message/delivery-status":
            for block in part.get_payload() or []:
                rcpt = block.get("Final-Recipient") or block.get("Original-Recipient") or ""
                if ";" in rcpt:
                    failed.append(rcpt.split(";", 1)[1].strip().lower())
        elif ptype in ("message/rfc822", "text/rfc822-headers"):
            raw = part.get_payload()
            inner = raw[0] if isinstance(raw, list) and raw else None
            if inner is not None and hasattr(inner, "get"):
                original_ids += _ids(inner.get("Message-ID", ""))
            elif isinstance(raw, str):
                m = re.search(r"^Message-ID:\s*(<[^>]+>)", raw, re.M | re.I)
                if m:
                    original_ids.append(m.group(1))
    return True, list(dict.fromkeys(failed)), original_ids


def _is_auto_reply(msg) -> bool:
    auto = (msg.get("Auto-Submitted") or "").lower()
    if auto and auto != "no":
        return True
    if msg.get("X-Autoreply") or msg.get("X-Autorespond"):
        return True
    if (msg.get("Precedence") or "").lower() in ("auto_reply", "bulk", "junk"):
        return True
    subject = _decode(msg.get("Subject", "")).lower()
    return subject.startswith(("automatic reply", "auto:", "out of office", "autoreply"))


def fetch_new(cfg: dict, cursor: dict) -> tuple:
    """Messages that arrived in INBOX since `cursor` ({uidvalidity, last_uid}).

    Opened read-only and fetched with BODY.PEEK, so nothing Mira reads is
    marked as read -- the user still sees replies as new in Gmail. On the very
    first poll it only records where the inbox currently ends: mail that was
    there before outreach started is not replies to it.

    Returns (messages, new_cursor)."""
    conn = _imap(cfg)
    try:
        typ, _ = conn.select("INBOX", readonly=True)
        if typ != "OK":
            raise MailboxError("could not open INBOX")
        uidvalidity = (conn.response("UIDVALIDITY")[1] or [None])[0]
        uidvalidity = uidvalidity.decode() if isinstance(uidvalidity, bytes) else str(uidvalidity)

        typ, data = conn.uid("search", None, "ALL")
        all_uids = [int(u) for u in (data[0] or b"").split()] if typ == "OK" else []
        top = max(all_uids) if all_uids else 0

        if not cursor or cursor.get("uidvalidity") != uidvalidity:
            return [], {"uidvalidity": uidvalidity, "last_uid": top}

        last = int(cursor.get("last_uid") or 0)
        new_uids = sorted(u for u in all_uids if u > last)[:MAX_FETCH]
        own = cfg.get("email_address", "").lower()
        out = []
        for uid in new_uids:
            typ, parts = conn.uid("fetch", str(uid), "(BODY.PEEK[])")
            raw = next((p[1] for p in parts or [] if isinstance(p, tuple)), None)
            if typ != "OK" or raw is None:
                continue
            msg = email.message_from_bytes(raw)
            from_name, from_addr = parseaddr(_decode(msg.get("From", "")))
            if from_addr.lower() == own:
                continue  # a test send to ourselves, not a reply
            is_bounce, failed, original_ids = _bounce_info(msg)
            out.append({
                "uid": uid,
                "from_addr": from_addr.lower(),
                "from_name": from_name,
                "to": [a.lower() for _, a in getaddresses(msg.get_all("To", []))],
                "subject": _decode(msg.get("Subject", "")),
                "date": msg.get("Date", ""),
                "message_id": (_ids(msg.get("Message-ID", "")) or [""])[0],
                "in_reply_to": _ids(msg.get("In-Reply-To", "")),
                "references": _ids(msg.get("References", "")),
                "text": _strip_quoted(_body_text(msg))[:MAX_BODY_CHARS],
                "auto_reply": _is_auto_reply(msg),
                "bounce": is_bounce,
                "bounced_recipients": failed,
                "bounced_message_ids": original_ids,
            })
        new_last = max(new_uids) if new_uids else last
        return out, {"uidvalidity": uidvalidity, "last_uid": new_last}
    finally:
        try:
            conn.logout()
        except (imaplib.IMAP4.error, OSError):
            pass

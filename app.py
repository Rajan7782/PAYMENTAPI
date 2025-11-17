import imaplib
import email
from email.header import decode_header
import re
import os
from flask import Flask, request, jsonify
from dotenv import load_dotenv

# ---------- ENV LOAD ----------
load_dotenv()

EMAIL_HOST = os.getenv("EMAIL_HOST", "imap.gmail.com")
EMAIL_USER = os.getenv("EMAIL_USER")
EMAIL_PASS = os.getenv("EMAIL_PASS")

SEARCH_KEYWORDS = [
    k.strip().lower()
    for k in os.getenv(
        "SEARCH_KEYWORDS",
        "payment received,paytm for business,upi,credited"
    ).split(",")
]

app = Flask(__name__)


# ---------- HELPERS ----------

def parse_amount(text: str):
    """Amount detect kare: ₹ 1, Rs. 1.00, INR 150"""
    patterns = [
        r"₹\s*([0-9,]+\.?\d*)",
        r"rs\.?\s*([0-9,]+\.?\d*)",
        r"inr\s*([0-9,]+\.?\d*)",
    ]
    for p in patterns:
        m = re.search(p, text, re.IGNORECASE)
        if m:
            return m.group(1)
    return None


def parse_sender(text: str):
    """Sender / VPA detect kare"""
    # vpa: abc@upi
    m = re.search(r"vpa[:\s]+([a-z0-9@._-]+)", text, re.IGNORECASE)
    if m:
        return m.group(1).strip()
    # from xyz
    m = re.search(r"from\s+([a-z0-9 @._\-]+)", text, re.IGNORECASE)
    if m:
        return m.group(1).strip()
    return None


def connect_imap():
    if not EMAIL_USER or not EMAIL_PASS:
        raise RuntimeError("EMAIL_USER / EMAIL_PASS env vars set nahi hain.")
    mail = imaplib.IMAP4_SSL(EMAIL_HOST)
    mail.login(EMAIL_USER, EMAIL_PASS)
    mail.select("INBOX")
    return mail


def fetch_transaction(tx_id: str):
    """Gmail se given transaction/order ID ke liye payment email dhundho"""
    mail = connect_imap()

    # subject+body sab me tx_id search
    status, messages = mail.search(None, f'TEXT "{tx_id}"')
    if status != "OK":
        mail.logout()
        raise Exception("IMAP search error")

    ids = messages[0].split()

    for msg_id in ids:
        _, msg_data = mail.fetch(msg_id, "(RFC822)")
        msg = email.message_from_bytes(msg_data[0][1])

        # subject decode
        subject_raw, encoding = decode_header(msg.get("Subject"))[0]
        if isinstance(subject_raw, bytes):
            subject = subject_raw.decode(encoding or "utf-8", errors="ignore")
        else:
            subject = subject_raw or ""

        body = ""
        if msg.is_multipart():
            for part in msg.walk():
                ctype = part.get_content_type()
                if ctype in ("text/plain", "text/html"):
                    body_part = part.get_payload(decode=True)
                    if body_part:
                        body += body_part.decode("utf-8", errors="ignore")
        else:
            body_bytes = msg.get_payload(decode=True)
            if body_bytes:
                body += body_bytes.decode("utf-8", errors="ignore")

        combined = (subject + "\n" + body).lower()

        # payment-type email hona chahiye
        if not any(k in combined for k in SEARCH_KEYWORDS):
            continue

        # tx_id present hona chahiye
        if tx_id.lower() not in combined:
            continue

        amount = parse_amount(combined)
        sender = parse_sender(combined)
        email_time = msg.get("Date", "")

        mail.logout()
        return {
            "tx_id": tx_id,
            "amount": amount,
            "sender": sender,
            "subject": subject,
            "time": email_time,
        }

    mail.logout()
    return None


# ---------- API HELPERS ----------

def get_tx_id_from_query():
    q = request.args
    keys = [
        "tx_id",
        "txn_id",
        "trx",
        "id",
        "transaction_id",
        "transection_id",  # spelling wali bhi support
    ]
    for key in keys:
        val = q.get(key)
        if val and val.strip():
            return val.strip()
    return None


# ---------- ROUTES ----------

@app.get("/trx")
def trx_api():
    """
    Example:
      /trx?tx_id=YOUR_TX_ID
      /trx?transection_id=YOUR_TX_ID
    """
    tx_id = get_tx_id_from_query()

    if not tx_id:
        return (
            jsonify(
                {
                    "ok": False,
                    "error": "Missing tx_id. Use /trx?tx_id=...",
                }
            ),
            400,
        )

    try:
        result = fetch_transaction(tx_id)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

    if not result:
        return jsonify(
            {
                "ok": True,
                "found": False,
                "message": "No payment email found for this ID.",
            }
        )

    return jsonify({"ok": True, "found": True, "transaction": result})


@app.get("/health")
def health():
    return {"ok": True}


if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)

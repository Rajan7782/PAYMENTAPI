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

# sirf in senders se aaya mail valid hoga
ALLOWED_FROM = [
    s.strip().lower()
    for s in os.getenv("ALLOWED_FROM", "no-reply@paytm.com").split(",")
]

app = Flask(__name__)


# ---------- HELPERS ----------

def parse_amount(text: str):
    """Amount detect kare: ₹ 5, Rs. 5.00, INR 5"""
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


def parse_sender(body: str):
    """
    Sender (VPA / UPI ID) nikalne ke liye powerful parser.
    Paytm, PhonePe, GPay wagairah ke formats handle karega.
    """

    # Format 1: direct UPI ID (most common) xyz@upi / xyz@ybl / xyz@phonepe
    m = re.search(r"\b([a-z0-9._-]+@[a-z]{2,15})\b", body, re.IGNORECASE)
    if m:
        return m.group(1).strip()

    # Format 2: "BHIM UPI xyz@upi"
    m = re.search(r"bhim\s+upi\s+([a-z0-9._-]+@[a-z]{2,15})", body, re.IGNORECASE)
    if m:
        return m.group(1).strip()

    # Format 3: "VPA: xyz@okaxis"
    m = re.search(r"vpa[:\s]+([a-z0-9._-]+@[a-z]{2,15})", body, re.IGNORECASE)
    if m:
        return m.group(1).strip()

    # Format 4: "From xyz@okaxis"
    m = re.search(r"from[:\s]+([a-z0-9._-]+@[a-z]{2,15})", body, re.IGNORECASE)
    if m:
        return m.group(1).strip()

    return None


def parse_order_id(text: str):
    """
    Order ID detect kare:
    'Order ID: T2511172214347118346964'
    """
    m = re.search(r"order id[:\s]+([a-z0-9]+)", text, re.IGNORECASE)
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
    """
    Sirf tab match karega jab:
    - sender allowed ho (ALLOWED_FROM me ho)
    - email payment-type ho (SEARCH_KEYWORDS)
    - email ke andar 'Order ID:' line mile
    - us line ka ID exactly tx_id ke barabar ho
    """
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

        # ----- sender check (fake email filter) -----
        from_header = msg.get("From", "") or ""
        from_lower = from_header.lower()
        if not any(allowed in from_lower for allowed in ALLOWED_FROM):
            continue  # not Paytm → skip

        # ----- subject -----
        subject_raw, encoding = decode_header(msg.get("Subject"))[0]
        if isinstance(subject_raw, bytes):
            subject = subject_raw.decode(encoding or "utf-8", errors="ignore")
        else:
            subject = subject_raw or ""

        # ----- body -----
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

        combined = subject + "\n" + body
        combined_lower = combined.lower()

        # ----- payment-type email check -----
        if not any(k in combined_lower for k in SEARCH_KEYWORDS):
            continue

        # ----- ORDER ID MUST MATCH -----
        order_id_in_mail = parse_order_id(combined_lower)
        if not order_id_in_mail:
            continue  # koi order id hi nahi → skip

        # yahan main condition: order id == tx_id
        if order_id_in_mail.lower() != tx_id.lower():
            continue  # amount ya kuch aur match hua hoga → skip

        # ----- ab safe hai: amount/time/sender nikaalo -----
        amount = parse_amount(combined_lower)
        sender = parse_sender(combined_lower)
        email_time = msg.get("Date", "")

        mail.logout()
        return {
            "tx_id": tx_id,
            "order_id": order_id_in_mail,
            "amount": amount,
            "sender": sender,
            "subject": subject,
            "time": email_time,
            "from": from_header,
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
      /trx?tx_id=ORDER_ID
      /trx?transection_id=ORDER_ID
    """
    tx_id = get_tx_id_from_query()

    if not tx_id:
        return (
            jsonify(
                {
                    "ok": False,
                    "error": "Missing tx_id. Use /trx?tx_id=ORDER_ID",
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
                "message": "No valid Paytm payment email found for this Order ID.",
            }
        )

    return jsonify({"ok": True, "found": True, "transaction": result})


@app.get("/health")
def health():
    return {"ok": True, "allowed_from": ALLOWED_FROM}


if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)

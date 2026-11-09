"""One-off: send a real test email to Vidhya Jhaa via your own Lilly Outlook
account over SMTP, simulating the "new access request to approve" notification
that backend/app/notify.py sends via SES in the real app.

Usage:
    export LILLY_SMTP_USER="your.name@lilly.com"
    export LILLY_SMTP_PASSWORD="..."          # your Outlook password or app password
    python3 send_test_email.py

Lilly's Office 365 tenant may require an app password or block basic SMTP auth
entirely if modern auth / MFA is enforced. If this script fails with an auth
error, that's almost certainly why — ask IT about SMTP AUTH / app passwords,
or send via Outlook's own "send as" / API instead.
"""
import os
import smtplib
import sys
from email.mime.text import MIMEText

SMTP_HOST = "smtp.office365.com"
SMTP_PORT = 587
TO_ADDR = "vidhya.jhaa@lilly.com"

user = os.environ.get("LILLY_SMTP_USER")
password = os.environ.get("LILLY_SMTP_PASSWORD")
if not user or not password:
    sys.exit("Set LILLY_SMTP_USER and LILLY_SMTP_PASSWORD environment variables first.")

subject = "Reltio access request AR-DEMO-0001 needs your approval"
body = (
    "You (requester) requested Reltio access.\n\n"
    "Request:      AR-DEMO-0001\n"
    "Requested by: You (requester)\n"
    "Use case:     Basic Search and Find\n"
    "Tenants:      DEV B0 (b0e8OhkxuytO3n3)\n\n"
    "This is a one-off test email sent from a demo script, not the deployed app."
)

msg = MIMEText(body, "plain", "utf-8")
msg["Subject"] = subject
msg["From"] = user
msg["To"] = TO_ADDR

with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
    server.starttls()
    server.login(user, password)
    server.sendmail(user, [TO_ADDR], msg.as_string())

print(f"Sent to {TO_ADDR} from {user}.")

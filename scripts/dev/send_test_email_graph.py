"""Send a real email to Vidhya Jhaa via Microsoft Graph API using device-code
flow.  This avoids SMTP entirely — you sign in via your browser with MFA as
normal, and the script sends the email through Graph's /sendMail endpoint.

Usage:
    pip3 install msal requests
    python3 send_test_email_graph.py

It will print a URL and a code. Open the URL in your browser, enter the code,
sign in with your Lilly account (MFA and all), and the script sends the email
automatically once auth completes.
"""

import json
import sys

try:
    import msal
    import requests
except ImportError:
    sys.exit("Install dependencies first:  pip3 install msal requests")

# Microsoft's well-known "Microsoft Office" public client app ID.
# This is a first-party Microsoft app that already has Mail.Send delegated
# permission in every O365 tenant — no app registration needed.
CLIENT_ID = "d3590ed6-52b1-4102-aeff-aad2292ab01c"
AUTHORITY = "https://login.microsoftonline.com/organizations"
SCOPES = ["Mail.Send"]

TO_ADDR = "vidhya.jhaa@lilly.com"
SUBJECT = "Reltio access request AR-DEMO-0001 needs your approval"
BODY_TEXT = (
    "You (requester) requested Reltio access.\n\n"
    "Request:      AR-DEMO-0001\n"
    "Requested by: You (requester)\n"
    "Use case:     Basic Search and Find\n"
    "Tenants:      DEV B0 (b0e8OhkxuytO3n3)\n\n"
    "This is a one-off test email sent from a demo script, not the deployed app."
)

app = msal.PublicClientApplication(CLIENT_ID, authority=AUTHORITY)

flow = app.initiate_device_flow(scopes=SCOPES)
if "user_code" not in flow:
    sys.exit(f"Device flow failed: {json.dumps(flow, indent=2)}")

print(f"\n  Open this URL in your browser:  {flow['verification_uri']}")
print(f"  Enter this code:                {flow['user_code']}\n")
print("Waiting for you to sign in...")

result = app.acquire_token_by_device_flow(flow)

if "access_token" not in result:
    sys.exit(f"Auth failed: {result.get('error_description', result)}")

token = result["access_token"]
print(f"Signed in as {result.get('id_token_claims', {}).get('preferred_username', '(unknown)')}")

resp = requests.post(
    "https://graph.microsoft.com/v1.0/me/sendMail",
    headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    json={
        "message": {
            "subject": SUBJECT,
            "body": {"contentType": "Text", "content": BODY_TEXT},
            "toRecipients": [{"emailAddress": {"address": TO_ADDR}}],
        }
    },
)

if resp.status_code == 202:
    print(f"\nSent to {TO_ADDR} successfully.")
else:
    print(f"\nGraph API error {resp.status_code}: {resp.text}")
    sys.exit(1)

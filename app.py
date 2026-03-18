def send_email(subject: str, body: str) -> None:
    required = {
        "RESEND_API_KEY": RESEND_API_KEY,
        "RESEND_FROM_EMAIL": RESEND_FROM_EMAIL,
        "DIGEST_TO": DIGEST_TO,
    }
    missing = [name for name, value in required.items() if not value]
    if missing:
        raise RuntimeError(f"Missing Resend configuration: {', '.join(missing)}")

    payload = json.dumps(
        {
            "from": RESEND_FROM_EMAIL,
            "to": [DIGEST_TO],
            "subject": subject,
            "text": body,
        }
    ).encode("utf-8")

    request = Request(
        "https://api.resend.com/emails",
        data=payload,
        headers={
            "Authorization": f"Bearer {RESEND_API_KEY}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    with urlopen(request, timeout=30) as response:
        if response.status >= 400:
            raise RuntimeError(f"Resend API returned status {response.status}")

"""Send OTP emails via SMTP (aiosmtplib)."""
from __future__ import annotations

import logging

import aiosmtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

log = logging.getLogger(__name__)


async def send_otp_email(
    to_email: str,
    otp_code: str,
    smtp_host: str,
    smtp_port: int,
    smtp_user: str,
    smtp_password: str,
    email_from: str,
) -> None:
    """Send a 6-digit OTP to *to_email* via STARTTLS SMTP."""

    subject = "Your SurveyFlow login code"

    html_body = f"""\
<!DOCTYPE html>
<html>
<head><meta charset="UTF-8"></head>
<body style="margin:0;padding:0;background:#0f1117;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif">
  <table width="100%" cellpadding="0" cellspacing="0" style="background:#0f1117;padding:40px 0">
    <tr><td align="center">
      <table width="400" cellpadding="0" cellspacing="0"
             style="background:#1a1d27;border:1px solid #2d3148;border-radius:12px;padding:40px 36px">
        <tr><td align="center" style="padding-bottom:8px">
          <span style="font-size:1.6rem;font-weight:700;color:#7c83fd;letter-spacing:-0.5px">SurveyFlow</span>
        </td></tr>
        <tr><td align="center" style="color:#64748b;font-size:0.85rem;padding-bottom:28px">
          Your one-time login code
        </td></tr>
        <tr><td align="center" style="padding-bottom:24px">
          <span style="display:inline-block;background:#0f1117;border:1px solid #2d3148;border-radius:8px;
                       padding:16px 32px;font-size:2rem;font-weight:700;color:#e2e8f0;letter-spacing:8px">
            {otp_code}
          </span>
        </td></tr>
        <tr><td align="center" style="color:#64748b;font-size:0.8rem;line-height:1.6">
          This code expires in <strong style="color:#94a3b8">10 minutes</strong>.<br>
          If you did not request this, please ignore this email.
        </td></tr>
      </table>
    </td></tr>
  </table>
</body>
</html>"""

    text_body = f"Your SurveyFlow login code is: {otp_code}\nThis code expires in 10 minutes."

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = email_from or smtp_user
    msg["To"] = to_email
    msg.attach(MIMEText(text_body, "plain"))
    msg.attach(MIMEText(html_body, "html"))

    await aiosmtplib.send(
        msg,
        hostname=smtp_host,
        port=smtp_port,
        username=smtp_user,
        password=smtp_password,
        start_tls=True,
    )
    log.info("OTP email sent to %s", to_email)

import base64
import json
import urllib.error
import urllib.request

from django.conf import settings
from django.core.mail.backends.base import BaseEmailBackend
from django.utils.html import strip_tags


class MailjetAPIBackend(BaseEmailBackend):
    api_url = "https://api.mailjet.com/v3.1/send"

    def send_messages(self, email_messages):
        if not email_messages:
            return 0

        api_key = getattr(settings, "MAILJET_API_KEY", "").strip()
        secret_key = getattr(settings, "MAILJET_SECRET_KEY", "").strip()
        if not api_key or not secret_key:
            if self.fail_silently:
                return 0
            raise RuntimeError("MAILJET_API_KEY and MAILJET_SECRET_KEY must be configured")

        sent_count = 0
        for message in email_messages:
            try:
                if self._send_message(api_key, secret_key, message):
                    sent_count += 1
            except Exception:
                if not self.fail_silently:
                    raise
        return sent_count

    def _send_message(self, api_key: str, secret_key: str, message) -> bool:
        to_list = self._to_recipients(message.to)
        cc_list = self._to_recipients(message.cc)
        bcc_list = self._to_recipients(message.bcc)
        if not (to_list or cc_list or bcc_list):
            return False

        text_part = message.body or ""
        html_part = ""
        for alt_body, mime in getattr(message, "alternatives", []):
            if mime == "text/html":
                html_part = alt_body
                break
        if html_part and not text_part:
            text_part = strip_tags(html_part)

        from_email = (message.from_email or settings.DEFAULT_FROM_EMAIL or "").strip()
        if not from_email:
            raise RuntimeError("MAILJET_FROM_EMAIL/DEFAULT_FROM_EMAIL must be configured")

        payload_message = {
            "From": {
                "Email": from_email,
                "Name": getattr(settings, "MAILJET_FROM_NAME", "SmartTeX").strip() or "SmartTeX",
            },
            "Subject": message.subject or "",
            "TextPart": text_part,
        }
        if html_part:
            payload_message["HTMLPart"] = html_part
        if to_list:
            payload_message["To"] = to_list
        if cc_list:
            payload_message["Cc"] = cc_list
        if bcc_list:
            payload_message["Bcc"] = bcc_list

        payload = json.dumps({"Messages": [payload_message]}).encode("utf-8")
        auth_raw = f"{api_key}:{secret_key}".encode("utf-8")
        auth_header = base64.b64encode(auth_raw).decode("ascii")

        request = urllib.request.Request(
            self.api_url,
            data=payload,
            method="POST",
            headers={
                "Authorization": f"Basic {auth_header}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        timeout = int(getattr(settings, "EMAIL_TIMEOUT", 15))
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                status = response.getcode()
                response_body = response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Mailjet API HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Mailjet API request failed: {exc}") from exc

        if status < 200 or status >= 300:
            raise RuntimeError(f"Mailjet API returned unexpected status {status}: {response_body}")

        try:
            data = json.loads(response_body)
        except json.JSONDecodeError:
            return True

        msg_entries = data.get("Messages") or []
        first = msg_entries[0] if msg_entries else {}
        if first.get("Status") == "success":
            return True
        errors = first.get("Errors") or []
        raise RuntimeError(f"Mailjet API send failed: {errors}")

    @staticmethod
    def _to_recipients(recipients):
        result = []
        for email in recipients or []:
            clean = (email or "").strip()
            if clean:
                result.append({"Email": clean})
        return result

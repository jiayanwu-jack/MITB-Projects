from __future__ import annotations

from email.message import EmailMessage
import base64
import json
import re
import smtplib
import ssl
from pathlib import Path

from .config import Settings
from .schemas import EmailDraft


class EmailDrafter:
    def draft(self, candidate: dict) -> EmailDraft:
        raise NotImplementedError


class MockEmailDrafter(EmailDrafter):
    def __init__(self, settings: Settings):
        self.settings = settings

    def draft(self, candidate: dict) -> EmailDraft:
        startup = candidate.get("startup_name", "your startup")
        feature = candidate.get("description", "").split(".")[0].strip()
        subject = f"Invitation to {self.settings.competition_name} — {startup}"
        body = (
            f"Dear {startup} team,\n\n"
            f"I came across {startup} while researching university-founded ventures. "
            f"{feature + '.' if feature else ''}\n\n"
            f"We would like to invite your team to consider applying for {self.settings.competition_name}. "
            f"The competition is designed for startups founded by current university or college students, "
            f"or founders who graduated within the last five years.\n\n"
            f"Please let us know if you would like the application details.\n\n"
            f"Best regards,\n{self.settings.organizer_signature}"
        )
        return EmailDraft(subject=subject, body=body)


def _draft_facts(candidate: dict) -> dict:
    return {
        "startup_name": candidate.get("startup_name", ""),
        "description": candidate.get("description", ""),
        "industry": candidate.get("industry", ""),
        "source_org": candidate.get("source_org", ""),
        "eligibility_reason": candidate.get("eligibility_reason", ""),
    }


def _draft_system_prompt(settings: Settings) -> str:
    return f"""Write a concise, professional invitation email for {settings.competition_name}.
Personalize with only the supplied facts. Do not claim awards, customers, partnerships, funding, traction, or founder biographies that are not supplied.
Do not say the startup is definitely eligible; invite them to verify eligibility during application.
Keep the body under 180 words. Plain text only. Sign as {settings.organizer_signature}."""


def _extract_json_payload(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text)
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        return text[start:end + 1]
    return text


def _draft_schema_instruction() -> str:
    return (
        "Return only valid JSON matching this schema exactly:\n"
        f"{json.dumps(EmailDraft.model_json_schema(), ensure_ascii=False)}"
    )


class OpenAIEmailDrafter(EmailDrafter):
    def __init__(self, settings: Settings):
        if not settings.openai_api_key:
            raise ValueError("OPENAI_API_KEY is required when llm_backend=openai.")
        from openai import OpenAI

        self.client = OpenAI(api_key=settings.openai_api_key)
        self.model = settings.openai_model
        self.settings = settings

    def draft(self, candidate: dict) -> EmailDraft:
        response = self.client.responses.parse(
            model=self.model,
            input=[
                {"role": "system", "content": _draft_system_prompt(self.settings)},
                {"role": "user", "content": str(_draft_facts(candidate))},
            ],
            text_format=EmailDraft,
        )
        return response.output_parsed


class DeepSeekEmailDrafter(EmailDrafter):
    """Draft emails through DeepSeek's OpenAI-compatible chat API (JSON mode)."""

    def __init__(self, settings: Settings, model: str | None = None):
        if not settings.deepseek_api_key:
            raise ValueError("DEEPSEEK_API_KEY is required when llm_backend=deepseek.")
        from openai import OpenAI

        self.settings = settings
        self.model = model or settings.deepseek_model
        self.client = OpenAI(
            api_key=settings.deepseek_api_key,
            base_url="https://api.deepseek.com/v1",
        )

    def draft(self, candidate: dict) -> EmailDraft:
        user = (
            f"{_draft_schema_instruction()}\n\n"
            f"FACTS:\n{json.dumps(_draft_facts(candidate), ensure_ascii=False)}"
        )
        completion = self.client.chat.completions.create(
            model=self.model,
            temperature=0,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": _draft_system_prompt(self.settings)},
                {"role": "user", "content": user},
            ],
        )
        content = completion.choices[0].message.content or ""
        return EmailDraft.model_validate_json(_extract_json_payload(content))


class GoogleEmailDrafter(EmailDrafter):
    """Draft emails through the Google AI Studio Gemini API."""

    API_BASE = "https://generativelanguage.googleapis.com/v1beta"

    def __init__(self, settings: Settings, model: str | None = None):
        if not settings.google_api_key:
            raise ValueError("GOOGLE_API_KEY is required when llm_backend=google.")
        self.settings = settings
        self.api_key = settings.google_api_key
        self.model = model or settings.google_model

    @staticmethod
    def _model_path(model: str) -> str:
        return model if model.startswith("models/") else f"models/{model}"

    def draft(self, candidate: dict) -> EmailDraft:
        import requests

        user = (
            f"{_draft_schema_instruction()}\n\n"
            f"FACTS:\n{json.dumps(_draft_facts(candidate), ensure_ascii=False)}"
        )
        response = requests.post(
            f"{self.API_BASE}/{self._model_path(self.model)}:generateContent",
            params={"key": self.api_key},
            json={
                "systemInstruction": {
                    "parts": [{"text": _draft_system_prompt(self.settings)}],
                },
                "contents": [
                    {"role": "user", "parts": [{"text": user}]},
                ],
                "generationConfig": {
                    "temperature": 0,
                    "responseMimeType": "application/json",
                },
            },
            timeout=60,
        )
        response.raise_for_status()
        payload = response.json()
        parts = (
            payload.get("candidates", [{}])[0]
            .get("content", {})
            .get("parts", [])
        )
        text = "\n".join(str(part.get("text", "")) for part in parts if part.get("text"))
        return EmailDraft.model_validate_json(_extract_json_payload(text))


class ClaudeEmailDrafter(EmailDrafter):
    """Draft emails through Anthropic Claude's Messages API."""

    API_URL = "https://api.anthropic.com/v1/messages"

    def __init__(self, settings: Settings, model: str | None = None):
        if not settings.anthropic_api_key:
            raise ValueError("ANTHROPIC_API_KEY is required when llm_backend=claude.")
        self.settings = settings
        self.api_key = settings.anthropic_api_key
        self.model = model or settings.claude_model

    def draft(self, candidate: dict) -> EmailDraft:
        import requests

        user = (
            f"{_draft_schema_instruction()}\n\n"
            f"FACTS:\n{json.dumps(_draft_facts(candidate), ensure_ascii=False)}"
        )
        response = requests.post(
            self.API_URL,
            headers={
                "x-api-key": self.api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": self.model,
                "max_tokens": 1024,
                "temperature": 0,
                "system": _draft_system_prompt(self.settings),
                "messages": [{"role": "user", "content": user}],
            },
            timeout=60,
        )
        response.raise_for_status()
        payload = response.json()
        text = "\n".join(
            str(block.get("text", ""))
            for block in payload.get("content", [])
            if block.get("type") == "text" and block.get("text")
        )
        return EmailDraft.model_validate_json(_extract_json_payload(text))


class OllamaEmailDrafter(EmailDrafter):
    def __init__(self, settings: Settings, model: str | None = None):
        from ollama import Client

        self.settings = settings
        self.model = model or settings.ollama_model
        self.client = Client(host=settings.ollama_host)

    def draft(self, candidate: dict) -> EmailDraft:
        schema = EmailDraft.model_json_schema()
        user = (
            f"Return JSON matching this schema exactly:\n{json.dumps(schema)}\n\n"
            f"FACTS:\n{json.dumps(_draft_facts(candidate), ensure_ascii=False)}"
        )
        options: dict[str, int | float] = {"temperature": 0}
        if self.settings.ollama_num_ctx > 0:
            options["num_ctx"] = self.settings.ollama_num_ctx
        response = self.client.chat(
            model=self.model,
            messages=[
                {"role": "system", "content": _draft_system_prompt(self.settings)},
                {"role": "user", "content": user},
            ],
            format=schema,
            options=options,
            think=False,
            stream=False,
        )
        return EmailDraft.model_validate_json(response.message.content)


class GmailClient:
    SCOPES = ["https://www.googleapis.com/auth/gmail.compose", "https://www.googleapis.com/auth/gmail.send"]

    def __init__(self, settings: Settings):
        self.settings = settings
        self.service = self._authenticate()

    def _authenticate(self):
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build

        creds = None
        token_path = self.settings.gmail_token_path
        credentials_path = self.settings.gmail_credentials_path
        if token_path.exists():
            creds = Credentials.from_authorized_user_file(str(token_path), self.SCOPES)
        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                if not credentials_path.exists():
                    raise FileNotFoundError(
                        f"Missing Gmail OAuth client file: {credentials_path}"
                    )
                flow = InstalledAppFlow.from_client_secrets_file(str(credentials_path), self.SCOPES)
                creds = flow.run_local_server(port=0)
            token_path.write_text(creds.to_json(), encoding="utf-8")
        return build("gmail", "v1", credentials=creds)

    @staticmethod
    def _raw_message(to: str, subject: str, body: str) -> dict:
        message = EmailMessage()
        message["To"] = to
        message["Subject"] = subject
        message.set_content(body)
        encoded = base64.urlsafe_b64encode(message.as_bytes()).decode()
        return {"raw": encoded}

    def create_draft(self, to: str, subject: str, body: str) -> dict:
        payload = {"message": self._raw_message(to, subject, body)}
        return self.service.users().drafts().create(userId="me", body=payload).execute()

    def send(self, to: str, subject: str, body: str) -> dict:
        if not self.settings.allow_live_send:
            raise PermissionError(
                "Live sending is disabled. Set ALLOW_LIVE_SEND=true only after reviewing the outreach queue."
            )
        payload = self._raw_message(to, subject, body)
        return self.service.users().messages().send(userId="me", body=payload).execute()


class SmtpClient:
    """Send email through a standard SMTP server with a username + app password.

    This needs NO OAuth consent screen, API approval, or Azure app registration - it is
    the simplest way to send automatically when the Gmail API / Outlook Graph routes are
    blocked. Use a personal Gmail App Password (host smtp.gmail.com, port 587) or any SMTP
    relay you have credentials for.

    Use as a context manager to reuse one connection for a batch (recommended for bulk):
        with SmtpClient(settings) as client:
            for row in rows:
                client.send(row["email"], subject, body)
    """

    def __init__(self, settings: Settings):
        self.settings = settings
        if not settings.smtp_user or not settings.smtp_password:
            raise ValueError(
                "SMTP_USER (your email address) and SMTP_PASSWORD (app password) are "
                "required for SMTP sending. Add them to .env."
            )
        self.host = settings.smtp_host
        self.port = settings.smtp_port
        self.user = settings.smtp_user
        self.password = settings.smtp_password
        self.sender = settings.smtp_from or settings.smtp_user
        self.use_ssl = settings.smtp_use_ssl or self.port == 465
        self._server: smtplib.SMTP | None = None

    # -- connection handling -------------------------------------------------
    def _connect(self) -> smtplib.SMTP:
        context = ssl.create_default_context()
        if self.use_ssl:
            server = smtplib.SMTP_SSL(self.host, self.port, timeout=30, context=context)
        else:
            server = smtplib.SMTP(self.host, self.port, timeout=30)
            server.ehlo()
            server.starttls(context=context)   # upgrade the plaintext connection to TLS
            server.ehlo()
        server.login(self.user, self.password)
        return server

    def __enter__(self) -> "SmtpClient":
        self._server = self._connect()
        return self

    def __exit__(self, *exc) -> None:
        if self._server is not None:
            try:
                self._server.quit()
            except Exception:
                pass
            finally:
                self._server = None

    def verify(self) -> None:
        """Open and close a connection to confirm the host/credentials work."""
        self._connect().quit()

    # -- sending -------------------------------------------------------------
    def _build(self, to: str, subject: str, body: str) -> EmailMessage:
        message = EmailMessage()
        message["From"] = self.sender
        message["To"] = to
        message["Subject"] = subject
        message.set_content(body)
        return message

    def send(self, to: str, subject: str, body: str) -> dict:
        if not self.settings.allow_live_send:
            raise PermissionError(
                "Live sending is disabled. Set ALLOW_LIVE_SEND=true only after reviewing "
                "the outreach queue."
            )
        message = self._build(to, subject, body)
        if self._server is not None:                 # reuse the batch connection
            self._server.send_message(message)
        else:                                        # one-shot: connect, send, disconnect
            with self._connect() as server:
                server.send_message(message)
        return {"status": "sent", "to": to}


class OutlookGraphClient:
    SCOPES = ["Mail.Send", "User.Read", "offline_access"]
    GRAPH_SEND_URL = "https://graph.microsoft.com/v1.0/me/sendMail"

    def __init__(self, settings: Settings):
        if not settings.outlook_client_id:
            raise ValueError("OUTLOOK_CLIENT_ID is required to send through Outlook.")
        import msal

        self.settings = settings
        self.msal = msal
        self.cache = msal.SerializableTokenCache()
        self.token_path = Path(settings.outlook_token_path)
        if self.token_path.exists():
            self.cache.deserialize(self.token_path.read_text(encoding="utf-8"))
        self.app = msal.PublicClientApplication(
            client_id=settings.outlook_client_id,
            authority=f"https://login.microsoftonline.com/{settings.outlook_tenant_id}",
            token_cache=self.cache,
        )

    def _save_cache(self) -> None:
        if self.cache.has_state_changed:
            self.token_path.parent.mkdir(parents=True, exist_ok=True)
            self.token_path.write_text(self.cache.serialize(), encoding="utf-8")

    def start_device_flow(self) -> dict:
        flow = self.app.initiate_device_flow(scopes=self.SCOPES)
        if "user_code" not in flow:
            raise RuntimeError(f"Could not start Outlook sign-in: {flow}")
        return flow

    def complete_device_flow(self, flow: dict) -> dict:
        result = self.app.acquire_token_by_device_flow(flow)
        if "access_token" not in result:
            raise RuntimeError(result.get("error_description") or str(result))
        self._save_cache()
        return result

    def acquire_token(self) -> str | None:
        accounts = self.app.get_accounts()
        if not accounts:
            return None
        result = self.app.acquire_token_silent(self.SCOPES, account=accounts[0])
        self._save_cache()
        return result.get("access_token") if result else None

    def is_signed_in(self) -> bool:
        return bool(self.acquire_token())

    def send(self, to: str, subject: str, body: str) -> dict:
        if not self.settings.allow_live_send:
            raise PermissionError(
                "Live sending is disabled. Set ALLOW_LIVE_SEND=true only after reviewing the outreach queue."
            )
        token = self.acquire_token()
        if not token:
            raise PermissionError("Outlook is not signed in. Complete Outlook sign-in first.")
        import requests

        payload = {
            "message": {
                "subject": subject,
                "body": {"contentType": "Text", "content": body},
                "toRecipients": [
                    {"emailAddress": {"address": to}},
                ],
            },
            "saveToSentItems": True,
        }
        response = requests.post(
            self.GRAPH_SEND_URL,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json=payload,
            timeout=30,
        )
        if response.status_code >= 400:
            raise RuntimeError(f"Microsoft Graph send failed: {response.status_code} {response.text}")
        return {"status_code": response.status_code}

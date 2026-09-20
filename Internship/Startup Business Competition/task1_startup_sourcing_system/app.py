from __future__ import annotations

import base64
import hashlib
import importlib.util
import io
import json
import re
import threading
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

import pandas as pd
import streamlit as st
import streamlit.components.v1 as components
from st_aggrid import AgGrid, GridOptionsBuilder, JsCode

from startup_sourcing import advisor
from startup_sourcing.config import settings
from startup_sourcing.founder_records import build_founder_rows
from startup_sourcing.outreach import GmailClient, OutlookGraphClient, SmtpClient
from startup_sourcing.pipeline import (
    CRAWLER_BACKENDS,
    EMAIL_PROVIDERS,
    LLM_BACKENDS,
    PEOPLE_PROVIDERS,
    SEARCH_BACKENDS,
    enrich_candidates_with_contacts,
    normalize_candidate_row,
    run_pipeline,
)

ROOT = Path(__file__).resolve().parent
SEED_CSV = ROOT / "data" / "seed_sources.csv"
OUTPUT_ROOT = ROOT / "outputs"
LATEST_CANDIDATES_PATH = OUTPUT_ROOT / "candidates_ranked.csv"
LATEST_FOUNDERS_PATH = OUTPUT_ROOT / "founders.csv"
UPLOAD_OUTPUT_DIR = OUTPUT_ROOT / "uploaded_contact_waterfall"
FOUNDER_OUTPUT_DIR = OUTPUT_ROOT / "founder_contact_waterfall"
EMAIL_TEMPLATE_PATH = OUTPUT_ROOT / "email_template.json"
LINKEDIN_LOGO_PATH = ROOT / "assets" / "linkedin-logo.png"

DEFAULT_EMAIL_TEMPLATE = {
    "subject": "Invitation to the 13th Lee Kuan Yew Global Business Plan Competition — {startup_name}",
    "body": (
        """Dear {founder_first},

I'm writing from the Institute of Innovation & Entrepreneurship at Singapore Management University. We've been following {startup_name}'s work in {industry}, and it stood out as exactly the kind of venture we hope to see on our stage.

I'd like to invite {startup_name} to take part in the 13th Lee Kuan Yew Global Business Plan Competition, taking place in 2027 — one of Asia's most globally representative deep-tech startup competitions. The most recent edition drew over 1,500 applications from 91 countries, and participating founders gain:

• A share of a prize pool of more than S$2.5 million
• 1:1 mentorship from C-suite leaders and the chance to pitch to 200+ VC funds and family offices in Singapore
• A global peer network of founders from the world's top 100 universities

You can find the full details here: https://lkygbpc.smu.edu.sg/

Would you be open to a short conversation about applying? I'd be glad to answer any questions and point you to the next steps.

Warm regards,
[Your name]
Institute of Innovation & Entrepreneurship
Singapore Management University"""
    ),
}


def _is_installed(package: str) -> bool:
    return importlib.util.find_spec(package) is not None


class _SafeFormatDict(dict):
    def __missing__(self, key):
        return "{" + key + "}"


@st.cache_data
def _linkedin_icon_data_uri() -> str:
    try:
        image_bytes = LINKEDIN_LOGO_PATH.read_bytes()
    except OSError:
        return ""
    return "data:image/png;base64," + base64.b64encode(image_bytes).decode("ascii")


def _outreach_grid_renderers() -> tuple[JsCode, JsCode, JsCode]:
    text_link = JsCode(
        """
        function(params) {
            const value = String(params.value || '');
            const linkField = params.colDef.cellRendererParams.linkField;
            const url = params.data && params.data[linkField];
            if (!url) return value;
            return {
                $$typeof: Symbol.for('react.element'), type: 'a', key: null, ref: null,
                props: {href: url, target: '_blank', rel: 'noopener noreferrer', children: value},
                _owner: null
            };
        }
        """
    )
    email_link = JsCode(
        """
        function(params) {
            if (!params.value) return '';
            return {
                $$typeof: Symbol.for('react.element'), type: 'a', key: null, ref: null,
                props: {href: params.value, children: '✉ Email'}, _owner: null
            };
        }
        """
    )
    linkedin_link = JsCode(
        """
        function(params) {
            if (!params.value) return '';
            const iconUrl = params.colDef.cellRendererParams.iconUrl;
            const iconStyle = {
                display: 'inline-block', width: '20px', height: '20px', verticalAlign: 'middle',
                backgroundImage: 'url(' + iconUrl + ')', backgroundSize: '400% 100%',
                backgroundPosition: 'right center', backgroundRepeat: 'no-repeat'
            };
            let fallback = null;
            if (!iconUrl) {
                fallback = 'in';
                iconStyle.background = '#0a78b5';
                iconStyle.color = '#fff';
                iconStyle.fontWeight = '700';
                iconStyle.textAlign = 'center';
                iconStyle.lineHeight = '20px';
            }
            return {
                $$typeof: Symbol.for('react.element'), type: 'a', key: null, ref: null,
                props: {
                    href: params.value,
                    target: '_blank',
                    rel: 'noopener noreferrer',
                    title: 'Open LinkedIn profile',
                    children: {
                        $$typeof: Symbol.for('react.element'), type: 'span', key: null, ref: null,
                        props: {'aria-label': 'LinkedIn', style: iconStyle, children: fallback},
                        _owner: null
                    }
                },
                _owner: null
            };
        }
        """
    )
    return text_link, email_link, linkedin_link


def _load_email_template() -> dict:
    if not EMAIL_TEMPLATE_PATH.exists():
        return DEFAULT_EMAIL_TEMPLATE.copy()
    try:
        data = json.loads(EMAIL_TEMPLATE_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return DEFAULT_EMAIL_TEMPLATE.copy()
    return {
        "subject": str(data.get("subject") or DEFAULT_EMAIL_TEMPLATE["subject"]),
        "body": str(data.get("body") or DEFAULT_EMAIL_TEMPLATE["body"]),
    }


def _save_email_template(subject: str, body: str) -> None:
    EMAIL_TEMPLATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    EMAIL_TEMPLATE_PATH.write_text(
        json.dumps({"subject": subject, "body": body}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _render_template(template: str, row: dict) -> str:
    _fn = str(row.get("founder_name", "") or "").strip()
    values = _SafeFormatDict({
        **row,
        "founder_first": (_fn.split()[0] if _fn else "there"),
        "competition_name": settings.competition_name,
        "competition_year": settings.competition_year,
        "organizer_name": settings.organizer_name,
        "organizer_signature": settings.organizer_signature,
        "competition_url": settings.competition_url,
    })
    return template.format_map(values)


def _result_to_csv(result: dict) -> str:
    """Serialize a pipeline/waterfall result's candidates to the candidates_ranked.csv
    shape the embedded outreach app expects (list columns re-encoded as JSON)."""
    cands = (result or {}).get("candidates") or []
    if not cands:
        return ""
    import csv as _csv
    import io as _io
    rows = []
    for c in cands:
        r = dict(c)
        for k in ("founders", "contact_evidence"):
            if isinstance(r.get(k), (list, dict)):
                r[k] = json.dumps(r[k], ensure_ascii=False)
        rows.append(r)
    cols: list[str] = []
    for r in rows:
        for k in r:
            if k not in cols:
                cols.append(k)
    buf = _io.StringIO()
    w = _csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore")
    w.writeheader()
    for r in rows:
        w.writerow({k: ("" if r.get(k) is None else r.get(k)) for k in cols})
    return buf.getvalue()


def _outreach_app_html(csv_text: str) -> str:
    """Embed send_email_app.html with the current CSV injected so it auto-loads —
    the same reference outreach UI the Shiny app uses."""
    tpl = ""
    for p in (ROOT / "shiny_app" / "send_email_app.html", ROOT / "send_email_app.html"):
        try:
            tpl = p.read_text(encoding="utf-8")
            break
        except Exception:
            continue
    if not tpl:
        return "<p style='font:14px sans-serif;padding:20px'>send_email_app.html was not found.</p>"
    preload = (
        "<script>window.PRELOADED_CSV = " + json.dumps(csv_text)
        + "; window.SMTP_INFO = "
        + json.dumps({"allow_live_send": False, "user": "", "host": "", "can_send": False})
        + ";</script>"
    )
    runner = (
        "<script>(function(){if(!window.PRELOADED_CSV)return;try{"
        "var p=toObjects(parseCSV(window.PRELOADED_CSV));"
        "HEADERS=p.headers;ROWS=normalizeRows(p.headers,p.rows);selected.clear();"
        "if(HEADERS.includes(COL.name)&&ROWS.length){initFilters();"
        "document.getElementById('controls').style.display='block';"
        "var s=document.querySelector('#drop p strong');"
        "if(s)s.textContent='Loaded the current data \\u2014 drop another CSV to replace';"
        "render();}}catch(e){console.error(e);}})();</script>"
    )
    html = tpl.replace("<body>", "<body>\n" + preload, 1)
    html = html.replace("</body>", runner + "\n</body>", 1)
    return html


# --- Bidirectional outreach component (enables server-side SMTP from the app) ---
_OUTREACH_COMPONENT_DIR = ROOT / "outputs" / "_outreach_component"


def _ensure_outreach_component() -> bool:
    idx = _OUTREACH_COMPONENT_DIR / "index.html"
    src = None
    for p in (ROOT / "shiny_app" / "send_email_app.html", ROOT / "send_email_app.html"):
        if p.exists():
            src = p
            break
    if src is None:
        return False
    try:
        content = src.read_text(encoding="utf-8")
        current = idx.read_text(encoding="utf-8") if idx.exists() else None
        if current != content:
            _OUTREACH_COMPONENT_DIR.mkdir(parents=True, exist_ok=True)
            idx.write_text(content, encoding="utf-8")
        return True
    except Exception:
        return False


# NOTE: the bidirectional custom component (declare_component) enabled server-side
# SMTP send from inside the iframe, but its componentReady handshake proved
# unreliable in some environments (the iframe could stay at 0 height -> blank tab).
# We fall back to the reliable components.html embed below (mailto sending). Set
# this back to the declare_component() call to re-enable in-iframe server-side send.
_OUTREACH_COMPONENT = None


def _smtp_send_messages(messages: list) -> dict:
    """Send composed {to,subject,body} messages via the tested SmtpClient (server-side)."""
    if not messages:
        return {"error": "No messages to send."}
    out = []
    try:
        with SmtpClient(settings) as client:
            for m in messages:
                try:
                    client.send(m.get("to"), m.get("subject"), m.get("body"))
                    out.append({"to": m.get("to"), "status": "sent"})
                except Exception as exc:
                    out.append({"to": m.get("to"), "status": "error", "error": str(exc)[:200]})
    except Exception as exc:
        return {"error": "SMTP connection failed: " + str(exc)[:300]}
    return {"results": out}


def _best_founder_email(
    candidate: dict, founder_name: str, founder_index: int
) -> tuple[str, str]:
    founder_key = founder_name.strip().lower()
    evidence = [
        row for row in candidate.get("contact_evidence", []) or []
        if isinstance(row, dict) and row.get("email")
    ]
    matched = [
        row for row in evidence
        if str(row.get("founder_match", "")).strip().lower() == founder_key
    ]
    if matched:
        matched.sort(
            key=lambda row: (
                1 if row.get("verification_status") == "valid" else 0,
                float(row.get("confidence", 0) or 0),
                float(row.get("verification_score", 0) or 0),
            ),
            reverse=True,
        )
        return (
            str(matched[0].get("email", "")).strip(),
            str(matched[0].get("email_type", "")).strip(),
        )
    if founder_index == 0:
        founder_email = str(candidate.get("founder_email") or "").strip()
        if founder_email:
            return founder_email, str(candidate.get("founder_email_type") or "").strip()
        contact_email = str(candidate.get("contact_email") or "").strip()
        if contact_email:
            return contact_email, str(candidate.get("contact_email_type") or "").strip()
    return "", ""


def _email_type_for(candidate: dict, email: str, fallback: str = "") -> str:
    value = str(email or "").strip().lower()
    if not value:
        return ""
    evidence = [
        row for row in candidate.get("contact_evidence", []) or []
        if isinstance(row, dict)
        and str(row.get("email", "")).strip().lower() == value
    ]
    evidence.sort(
        key=lambda row: (
            1 if row.get("verification_status") == "valid" else 0,
            float(row.get("confidence", 0) or 0),
            float(row.get("verification_score", 0) or 0),
        ),
        reverse=True,
    )
    if evidence:
        return str(evidence[0].get("email_type", "")).strip()
    return str(fallback or "").strip()


def _founder_linkedin(candidate: dict, founder: dict) -> str:
    return str(
        founder.get("linkedin_url")
        or candidate.get("founder_linkedin_url")
        or ""
    ).strip()


def _outreach_rows(candidates: list[dict]) -> list[dict]:
    rows: list[dict] = []
    for candidate_index, raw in enumerate(candidates):
        candidate = normalize_candidate_row(raw)
        founders = candidate.get("founders") or [{}]
        for founder_index, founder in enumerate(founders):
            if not isinstance(founder, dict):
                continue
            founder_name = str(founder.get("name", "")).strip()
            email, email_type = _best_founder_email(
                candidate, founder_name, founder_index
            )
            linkedin = _founder_linkedin(candidate, founder)
            source_url = _founder_source_url(candidate, founder)
            company_email = str(candidate.get("company_email", "")).strip()
            rows.append({
                "row_id": f"{candidate_index}-{founder_index}",
                "startup_name": str(candidate.get("startup_name", "")).strip(),
                "startup_url": _company_website_url(candidate),
                "industry": str(candidate.get("industry", "")).strip(),
                "country": str(candidate.get("country", "")).strip(),
                "founder_name": founder_name,
                "founder_email": email,
                "founder_email_type": email_type,
                "company_email": company_email,
                "company_email_type": _email_type_for(
                    candidate,
                    company_email,
                    str(
                        candidate.get("company_email_type")
                        or candidate.get("contact_email_type")
                        or ""
                    ),
                ),
                "founder_linkedin": linkedin,
                "founder_source_url": source_url,
                "description": str(candidate.get("description", "")).strip(),
                "eligibility_reason": str(candidate.get("eligibility_reason", "")).strip(),
                "source_org": str(candidate.get("source_org", "")).strip(),
            })
    return rows


def _recipient_email(row: dict) -> str:
    return str(row.get("recipient_email") or row.get("founder_email") or "").strip()


def _with_recipient(row: dict, recipient_mode: str) -> dict:
    company_mode = recipient_mode == "Company email"
    recipient_email = (
        row.get("company_email", "") if company_mode else row.get("founder_email", "")
    )
    updated = dict(row)
    updated["recipient_email"] = str(recipient_email or "").strip()
    email_type_field = "company_email_type" if company_mode else "founder_email_type"
    updated["recipient_email_type"] = str(row.get(email_type_field, "") or "").strip()
    return updated


def _send_or_draft(row: dict, subject_template: str, body_template: str, action: str) -> str:
    to_email = _recipient_email(row)
    if not to_email:
        return "missing_email"
    subject = _render_template(subject_template, row)
    body = _render_template(body_template, row)
    gmail = GmailClient(settings)
    if action == "send":
        gmail.send(to_email, subject, body)
        return "sent"
    gmail.create_draft(to_email, subject, body)
    return "draft_created"


def _send_smtp(row: dict, subject_template: str, body_template: str, client: SmtpClient) -> str:
    to_email = _recipient_email(row)
    if not to_email:
        return "missing_email"
    client.send(
        to_email,
        _render_template(subject_template, row),
        _render_template(body_template, row),
    )
    return "sent"


def _smtp_client_panel() -> SmtpClient | None:
    """Show SMTP readiness and return a usable client, or None with guidance."""
    if not settings.smtp_user or not settings.smtp_password:
        st.warning(
            "Set SMTP_USER (your email address) and SMTP_PASSWORD (app password) in .env "
            "to send automatically over SMTP. For a personal Gmail, create an App Password "
            "at https://myaccount.google.com/apppasswords (needs 2-Step Verification on)."
        )
        return None
    try:
        client = SmtpClient(settings)
    except Exception as exc:
        st.exception(exc)
        return None
    st.caption(f"SMTP: {settings.smtp_user} via {settings.smtp_host}:{settings.smtp_port}")
    if st.button("Test SMTP connection"):
        try:
            client.verify()
            st.success("SMTP login succeeded - ready to send.")
        except Exception as exc:
            st.error(f"SMTP login failed: {exc}")
    return client


def _send_outlook_api(row: dict, subject_template: str, body_template: str, client: OutlookGraphClient) -> str:
    to_email = _recipient_email(row)
    if not to_email:
        return "missing_email"
    client.send(
        to_email,
        _render_template(subject_template, row),
        _render_template(body_template, row),
    )
    return "sent"


def _outlook_compose_url(row: dict, subject_template: str, body_template: str) -> str:
    to_email = _recipient_email(row)
    return (
        "https://outlook.office.com/mail/deeplink/compose"
        f"?to={quote(to_email)}"
        f"&subject={quote(_render_template(subject_template, row))}"
        f"&body={quote(_render_template(body_template, row))}"
    )


def _outlook_bcc_url(rows: list[dict], subject_template: str, body_template: str) -> str:
    emails = ",".join(_recipient_email(row) for row in rows if _recipient_email(row))
    neutral = rows[0].copy() if rows else {}
    neutral.update({
        "founder_name": "there",
        "startup_name": "your startup",
        "industry": "your field",
        "country": "",
    })
    return (
        "https://outlook.office.com/mail/deeplink/compose"
        f"?bcc={quote(emails)}"
        f"&subject={quote(_render_template(subject_template, neutral))}"
        f"&body={quote(_render_template(body_template, neutral))}"
    )


def _outlook_api_client_panel() -> OutlookGraphClient | None:
    if not settings.outlook_client_id:
        st.warning("Set OUTLOOK_CLIENT_ID in .env to enable automatic Outlook sending.")
        return None
    try:
        client = OutlookGraphClient(settings)
    except ImportError:
        st.error("Install Microsoft auth dependencies with `py -m pip install -r requirements.txt`.")
        return None
    except Exception as exc:
        st.exception(exc)
        return None

    if client.is_signed_in():
        st.success("Outlook API is signed in.")
        return client

    st.info("Sign in to Outlook once before automatic sending.")
    if st.button("Start Outlook sign-in"):
        try:
            st.session_state["outlook_device_flow"] = client.start_device_flow()
        except Exception as exc:
            st.exception(exc)

    flow = st.session_state.get("outlook_device_flow")
    if flow:
        st.markdown(f"Open [{flow.get('verification_uri')}]({flow.get('verification_uri')})")
        st.code(flow.get("user_code", ""), language="text")
        st.caption(flow.get("message", ""))
        if st.button("I completed Outlook sign-in"):
            try:
                client.complete_device_flow(flow)
                st.session_state.pop("outlook_device_flow", None)
                st.success("Outlook sign-in complete.")
                return client
            except Exception as exc:
                st.exception(exc)
    return None


st.set_page_config(page_title="University Startup Sourcing Agent v3", layout="wide")

# Pin the browser tab title. Streamlit briefly resets it to "Streamlit" on every rerun,
# which reads as a flicker; a persistent observer in the parent window reverts any change
# back to our title immediately. Installed once (guarded), from a 0-height component.
components.html(
    """
    <script>
    (function(){
      try{
        var p = window.parent;
        if (p.__smuTitlePinned) return;
        p.__smuTitlePinned = true;
        var TITLE = "University Startup Sourcing Agent v3";
        var pin = function(){ if (p.document.title !== TITLE) p.document.title = TITLE; };
        var el = p.document.querySelector("title");
        if (el) new p.MutationObserver(pin).observe(
            el, {childList:true, characterData:true, subtree:true});
        p.setInterval(pin, 200);
        pin();
      }catch(e){}
    })();
    </script>
    """,
    height=0,
)

# Theme bridge (same approach as the task 2 app): Streamlit exposes no stable
# attribute for the active theme — only the applied background changes. This
# observer reads whether that background is dark and stamps
# data-smu-theme="dark|light" on the root, and the brand CSS below keys its
# colours off that attribute — so the top-right System / Light / Dark toggle
# restyles the custom elements too, not just Streamlit's own chrome.
components.html(
    """
    <script>
    (function(){
      try{
        var p = window.parent, pd = p.document;
        if (p.__smuThemeBridge) return; p.__smuThemeBridge = true;
        var root = pd.documentElement;
        function isDark(){
          var app = pd.querySelector('[data-testid="stApp"]') ||
                    pd.querySelector('.stApp');
          if (!app) return false;
          var m = (p.getComputedStyle(app).backgroundColor || '').match(/\\d+/g);
          if (!m) return false;
          return (0.299*m[0] + 0.587*m[1] + 0.114*m[2]) < 128;
        }
        function apply(){
          root.setAttribute('data-smu-theme', isDark() ? 'dark' : 'light');
        }
        apply();
        var app = pd.querySelector('[data-testid="stApp"]');
        if (app) new p.MutationObserver(apply).observe(
            app, {attributes:true, attributeFilter:['class','style']});
        p.setInterval(apply, 400);
      }catch(e){}
    })();
    </script>
    """,
    height=0,
)

st.markdown(
    """
    <style>
      /* ---- Brand palette as variables. Light is the default; dark applies
         when the OS prefers it (unless the user forced Light) and whenever
         the theme bridge above stamps data-smu-theme="dark". ---- */
      :root {
        --brand-heading:#12406b; --heading:#14293a; --muted:#6b7686; --link:#0b7285;
        --card-bg:#ffffff; --card-border:#e3e7ee; --card-shadow:0 1px 3px rgba(20,30,45,.06);
        --row-hover:#f7fafd; --chip-bg:#eef2f7; --chip-fg:#3a4658;
        --table-head-bg:#fafbfc;
        --accent:#12406b; --accent-hover:#0f3557; --on-accent:#ffffff;
        --sidebar-bg:#f6f9fc;
      }
      @media (prefers-color-scheme: dark) {
        :root:not([data-smu-theme="light"]) {
          --brand-heading:#8bb8e8; --heading:#e7edf5; --muted:#9aa7b8; --link:#4dd0e1;
          --card-bg:#1b2432; --card-border:#2c3a4e; --card-shadow:0 1px 3px rgba(0,0,0,.45);
          --row-hover:#223047; --chip-bg:#243247; --chip-fg:#c3cede;
          --table-head-bg:#161f2d;
          --accent:#2f6fb0; --accent-hover:#3b7cc0; --on-accent:#ffffff;
          --sidebar-bg:#131a26;
        }
      }
      :root[data-smu-theme="dark"] {
        --brand-heading:#8bb8e8; --heading:#e7edf5; --muted:#9aa7b8; --link:#4dd0e1;
        --card-bg:#1b2432; --card-border:#2c3a4e; --card-shadow:0 1px 3px rgba(0,0,0,.45);
        --row-hover:#223047; --chip-bg:#243247; --chip-fg:#c3cede;
        --table-head-bg:#161f2d;
        --accent:#2f6fb0; --accent-hover:#3b7cc0; --on-accent:#ffffff;
        --sidebar-bg:#131a26;
      }

      .outreach-header {
        background: #12406b;
        color: #fff;
        padding: 14px 20px;
        border-radius: 8px;
        margin-bottom: 16px;
        display: flex;
        align-items: baseline;
        gap: 14px;
        flex-wrap: wrap;
      }
      .outreach-header h2 { font-size: 17px; margin: 0; font-weight: 600; }
      .outreach-header span { opacity: .82; font-size: 12px; }
      .outreach-drop {
        padding: 22px;
        text-align: center;
        border: 2px dashed var(--card-border);
        border-radius: 10px;
        background: var(--card-bg);
        margin-bottom: 16px;
      }
      .outreach-drop strong { color: var(--brand-heading); }
      .outreach-toolbar {
        background: var(--card-bg);
        border: 1px solid var(--card-border);
        border-radius: 10px;
        padding: 12px;
        margin-bottom: 10px;
      }
      .outreach-count { color: var(--muted); font-size: 13px; margin: 6px 0 10px; }
      .outreach-table-head {
        background: var(--table-head-bg);
        border: 1px solid var(--card-border);
        border-radius: 10px 10px 0 0;
        color: var(--muted);
        font-size: 12px;
        text-transform: uppercase;
        letter-spacing: .03em;
        padding: 9px 12px;
      }
      .outreach-row {
        border-left: 1px solid var(--card-border);
        border-right: 1px solid var(--card-border);
        border-bottom: 1px solid var(--card-border);
        padding: 8px 12px;
        background: var(--card-bg);
      }
      .outreach-row:hover { background: var(--row-hover); }
      .outreach-chip {
        display: inline-block;
        background: var(--chip-bg);
        border-radius: 999px;
        padding: 1px 9px;
        font-size: 12px;
        color: var(--chip-fg);
      }
      .outreach-contact {
        font-family: ui-monospace, Menlo, Consolas, monospace;
        font-size: 12.5px;
      }
      .outreach-muted { color: var(--muted); }
      .outreach-card {
        background: var(--card-bg);
        border: 1px solid var(--card-border);
        border-radius: 10px;
        padding: 14px;
        margin-top: 12px;
      }

      /* ===== Brand theme to match the Shiny app (SMU navy, Segoe UI) ===== */
      html, body, .stApp, .stApp p, .stApp label, .stApp div, .stApp span,
      [data-testid="stSidebar"] * , .stMarkdown, button {
        font-family: 'Segoe UI', -apple-system, BlinkMacSystemFont, Roboto,
                     'Helvetica Neue', Arial, sans-serif;
      }
      /* Restore Streamlit's Material icons (upload button, sidebar collapse arrow,
         tooltip & toolbar icons) — the font rule above must not turn glyphs into
         literal ligature text. */
      [data-testid="stIconMaterial"], span[class*="material-symbols"],
      .material-symbols-rounded, .material-symbols-outlined, .material-icons {
        font-family: 'Material Symbols Rounded' !important;
      }
      .block-container { padding-top: 2.2rem; }

      /* Hero banner (mirrors the Shiny .app-hero) */
      .app-hero {
        background: linear-gradient(120deg, #0f2f4c 0%, #12406b 55%, #17537f 100%);
        color: #fff; padding: 22px 26px; border-radius: 14px; margin: 0 0 20px;
        box-shadow: 0 4px 14px rgba(15,47,76,.18);
      }
      .app-hero-title { font-size: 1.7rem; font-weight: 800; line-height: 1.15; }
      .app-hero-sub { color: #d7e6f3; margin-top: 6px; font-size: 1rem; }
      .app-hero-sub a { color: #bfe0ff; text-decoration: underline; }
      /* simple text title in the sidebar (replaces the hero banner) */
      .sidebar-title { font-size: 0.95rem; font-weight: 800; color: var(--brand-heading); line-height: 1.2; white-space: nowrap; }
      .sidebar-tagline { font-size: .8rem; color: var(--muted); margin: 3px 0 12px; }
      .sidebar-tagline a { color: var(--link); }

      /* Value-box style metrics */
      [data-testid="stMetric"] {
        background: var(--card-bg); border: 1px solid var(--card-border); border-radius: 14px;
        padding: 14px 18px; box-shadow: var(--card-shadow);
      }
      [data-testid="stMetricValue"] { color: var(--brand-heading); font-weight: 800; }
      [data-testid="stMetricLabel"] p {
        color: var(--muted); text-transform: uppercase; letter-spacing: .03em;
        font-size: .8rem; font-weight: 600;
      }

      /* Buttons: rounded, navy primary */
      .stButton > button, .stDownloadButton > button { border-radius: 10px; font-weight: 600; }
      .stButton > button[kind="primary"] { background: var(--accent); border-color: var(--accent); color: var(--on-accent); }
      .stButton > button[kind="primary"]:hover { background: var(--accent-hover); border-color: var(--accent-hover); }

      /* Sidebar: tinted panel, brand headings */
      [data-testid="stSidebar"] { background: var(--sidebar-bg); border-right: 1px solid var(--card-border); }
      [data-testid="stSidebar"] h1, [data-testid="stSidebar"] h2, [data-testid="stSidebar"] h3 {
        color: var(--brand-heading); font-weight: 700;
      }
      [data-testid="stSidebar"] div[role="radiogroup"] { gap: 7px; }
      [data-testid="stSidebar"] div[role="radiogroup"] > label {
        width: 100%; min-height: 44px; padding: 10px 12px;
        border: 1px solid #b8cdec; border-radius: 9px;
        background: #f8fbff; color: #173d6b;
        box-shadow: 0 1px 1px rgba(15,56,104,.04);
        transition: background .15s, border-color .15s, transform .15s;
        cursor: pointer;
      }
      [data-testid="stSidebar"] div[role="radiogroup"] > label > div:first-child {
        width: 0 !important; min-width: 0 !important; margin: 0 !important;
        opacity: 0; overflow: hidden; pointer-events: none;
      }
      [data-testid="stSidebar"] div[role="radiogroup"] > label p {
        color: #173d6b; font-size: 14px; font-weight: 600; line-height: 1.25;
      }
      [data-testid="stSidebar"] div[role="radiogroup"] > label:hover {
        background: #e2edff; border-color: #7ca8e6;
      }
      [data-testid="stSidebar"] div[role="radiogroup"] > label:has(input:checked) {
        background: #2563eb; border-color: #2563eb;
        box-shadow: 0 3px 8px rgba(37,99,235,.24);
      }
      [data-testid="stSidebar"] div[role="radiogroup"] > label:has(input:checked) p {
        color: #fff !important; font-weight: 700;
      }
      :root[data-smu-theme="dark"] [data-testid="stSidebar"] div[role="radiogroup"] > label {
        background: #111f34; color: #d9e7fb; border-color: #355476; box-shadow: none;
      }
      :root[data-smu-theme="dark"] [data-testid="stSidebar"] div[role="radiogroup"] > label p {
        color: #d9e7fb;
      }
      :root[data-smu-theme="dark"] [data-testid="stSidebar"] div[role="radiogroup"] > label:hover {
        background: #172a45; border-color: #5480ad;
      }
      :root[data-smu-theme="dark"] [data-testid="stSidebar"] div[role="radiogroup"] > label:has(input:checked) {
        background: #3b82f6; border-color: #3b82f6;
      }

      /* Data tables: rounded bordered container + navy headers where stylable */
      [data-testid="stDataFrame"], [data-testid="stDataFrameResizable"] {
        border: 1px solid var(--card-border); border-radius: 10px;
      }
      [data-testid="stTable"] thead th {
        background: var(--accent); color: var(--on-accent); font-weight: 600;
      }
      h1, h2, h3, h4 { color: var(--heading); letter-spacing: -0.01em; }
    </style>
    """,
    unsafe_allow_html=True,
)
# --- Run presets -----------------------------------------------------------------------
# Selecting a SUBSET seed list (e.g. seed_sources_new.csv) auto-applies a "high-value"
# preset: few, high-signal sources -> maximise recall per candidate (live + Firecrawl +
# Brave + Hunter + Apollo, paid always-on). The full seed_sources.csv keeps the economical
# defaults. Every control stays editable; edits persist per preset.
_STD_PRESET = {
    "crawler": settings.crawler_backend, "max_sources": 5, "max_pages": 2,
    "enrich": False, "contact_pages": settings.contact_max_pages,
    "search": settings.search_backend, "results": settings.search_max_results,
    "search_pages": settings.search_max_pages_per_candidate,
    "queries": settings.search_queries_per_founder,
    "founder_search": settings.enable_founder_search, "pdf": settings.enable_pdf_search,
    "email_provider": settings.email_provider, "people_provider": settings.people_provider,
    "verify": settings.verify_emails, "paid_fallback": settings.paid_fallback_only,
    "apollo_personal": settings.apollo_reveal_personal_emails,
    "apollo_waterfall": settings.apollo_run_waterfall_email,
}
_HIGH_VALUE_PRESET = {
    **_STD_PRESET,
    "crawler": "firecrawl", "max_sources": 200, "max_pages": 3,
    "enrich": True, "contact_pages": 8,
    "search": "brave", "results": 10, "search_pages": 8, "queries": 6,
    "founder_search": True, "pdf": True,
    "email_provider": "hunter", "people_provider": "apollo",
    "verify": True, "paid_fallback": False,
    "apollo_personal": True, "apollo_waterfall": True,
}

_EMAIL_RE = re.compile(
    r"^[A-Za-z0-9_%+\-]+(?:\.[A-Za-z0-9_%+\-]+)*@"
    r"[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)+$"
)
_SHARED_EMAIL_LOCALS = {
    "admin", "careers", "communications", "contact", "contactus",
    "enquiries", "enquiry", "feedback", "hello", "help", "hr", "info",
    "mail", "marketing", "media", "office", "press", "reception", "sales",
    "secretariat", "support", "team",
}


def _is_non_personal_email(email: str, email_type: str = "") -> bool:
    value = str(email or "").strip()
    if not value:
        return False
    local = value.split("@", 1)[0].lower()
    normalized_type = str(email_type or "").strip().lower()
    return (
        normalized_type in {"generic", "org_generic"}
        or normalized_type.startswith("company_")
        or local in _SHARED_EMAIL_LOCALS
    )


def _is_personal_email(email: str, email_type: str = "") -> bool:
    value = str(email or "").strip()
    return bool(
        value
        and _EMAIL_RE.fullmatch(value)
        and not _is_non_personal_email(value, email_type)
    )


def _email_warning(email: str, email_type: str = "") -> str:
    value = str(email or "").strip()
    if not value:
        return ""
    if _is_non_personal_email(value, email_type):
        return "⚠️"
    if "..." in value or ".." in value or not _EMAIL_RE.fullmatch(value):
        return "⚠️"
    return ""
_FOUNDER_SEARCH_PRESET = {
    **_STD_PRESET,
    "search": "brave",
    "email_provider": "hunter",
    "people_provider": "none",
    "founder_search": True,
    "verify": True,
    "paid_fallback": False,
}


def _opt_index(options, value, fallback=0):
    return options.index(value) if value in options else fallback


def _is_seed_csv(path) -> bool:
    """A data/ CSV counts as a seed list if it has a 'Seed URL' column."""
    try:
        with open(path, encoding="utf-8-sig", newline="") as f:
            return "Seed URL" in f.readline()
    except Exception:
        return False


def _read_seed_rows(path) -> list[dict]:
    """Return [{ID, label}] for the seed-selection multiselect."""
    try:
        df = pd.read_csv(path, dtype=str, keep_default_na=False)
    except Exception:
        return []
    rows = []
    for _, r in df.iterrows():
        sid = str(r.get("ID", "")).strip()
        if not sid:
            continue
        org = str(r.get("Organization / University", "")).strip()
        prog = str(r.get("Program / Source Page", "")).strip()
        pri = str(r.get("Priority (1=highest)", "")).strip()
        label = f"{sid} · P{pri} · {org}" + (f" — {prog}" if prog else "")
        rows.append({"ID": sid, "label": label})
    return rows


def _contact_waterfall_settings(
    prefix: str,
    defaults: dict,
    *,
    disabled: bool = False,
    enrichment_scope: str = "all",
) -> dict:
    """Render one independently keyed contact-waterfall configuration."""
    contact_max_pages = defaults["contact_pages"]
    if enrichment_scope != "founder":
        contact_max_pages = st.slider(
            "Startup-site pages", 1, 30, defaults["contact_pages"],
            disabled=disabled, key=f"{prefix}_contact_pages",
        )
    scope_label = {"company": "Company", "founder": "Founder"}.get(enrichment_scope, "")
    search_backend = st.selectbox(
        f"{scope_label + ' ' if scope_label else ''}search backend", SEARCH_BACKENDS,
        index=_opt_index(SEARCH_BACKENDS, defaults["search"]),
        disabled=disabled, key=f"{prefix}_search",
    )
    search_disabled = disabled or search_backend == "none"
    search_max_results = st.slider(
        "Results per search", 1, 20, defaults["results"],
        disabled=search_disabled, key=f"{prefix}_results",
    )
    search_max_pages = st.slider(
        "Search-result pages per startup", 0, 20, defaults["search_pages"],
        disabled=search_disabled, key=f"{prefix}_search_pages",
    )
    queries_per_founder = defaults["queries"]
    enable_founder_search = defaults["founder_search"]
    if enrichment_scope != "company":
        queries_per_founder = st.slider(
            "Queries per founder", 1, 6, defaults["queries"],
            disabled=search_disabled, key=f"{prefix}_queries",
        )
        if enrichment_scope == "all":
            enable_founder_search = st.checkbox(
                "Founder-level search", defaults["founder_search"],
                disabled=search_disabled, key=f"{prefix}_founder_search",
            )
        else:
            enable_founder_search = True
    else:
        enable_founder_search = False
    enable_pdf_search = st.checkbox(
        f"{scope_label + ' ' if scope_label else ''}PDF search", defaults["pdf"],
        disabled=search_disabled, key=f"{prefix}_pdf_search",
    )
    email_provider = st.selectbox(
        f"{scope_label + ' ' if scope_label else ''}email provider", EMAIL_PROVIDERS,
        index=_opt_index(EMAIL_PROVIDERS, defaults["email_provider"]),
        disabled=disabled, key=f"{prefix}_email_provider",
    )
    people_provider = "none"
    if enrichment_scope != "company":
        people_provider = st.selectbox(
            "People provider", PEOPLE_PROVIDERS,
            index=_opt_index(PEOPLE_PROVIDERS, defaults["people_provider"]),
            disabled=disabled, key=f"{prefix}_people_provider",
        )
    verify_emails = st.checkbox(
        f"Verify {scope_label.lower() + ' ' if scope_label else ''}emails with Hunter",
        defaults["verify"],
        disabled=disabled, key=f"{prefix}_verify",
    )
    paid_fallback_only = st.checkbox(
        "Paid providers only as fallback", defaults["paid_fallback"],
        disabled=disabled, key=f"{prefix}_paid_fallback",
    )
    apollo_reveal_personal = False
    apollo_waterfall = False
    if enrichment_scope != "company":
        apollo_disabled = disabled or people_provider != "apollo"
        apollo_reveal_personal = st.checkbox(
            "Apollo: reveal personal emails", defaults["apollo_personal"],
            disabled=apollo_disabled, key=f"{prefix}_apollo_personal",
        )
        apollo_waterfall = st.checkbox(
            "Apollo: run email waterfall", defaults["apollo_waterfall"],
            disabled=apollo_disabled, key=f"{prefix}_apollo_waterfall",
        )
    return {
        "contact_max_pages": contact_max_pages,
        "search_backend": search_backend,
        "search_max_results": search_max_results,
        "search_max_pages": search_max_pages,
        "queries_per_founder": queries_per_founder,
        "enable_founder_search": enable_founder_search,
        "enable_pdf_search": enable_pdf_search,
        "email_provider": email_provider,
        "people_provider": people_provider,
        "verify_emails": verify_emails,
        "paid_fallback_only": paid_fallback_only,
        "apollo_reveal_personal": apollo_reveal_personal,
        "apollo_waterfall": apollo_waterfall,
    }


def _contact_configuration_warnings(crawler_backend: str, config: dict) -> list[str]:
    missing = []
    if crawler_backend == "firecrawl" and not settings.firecrawl_api_key:
        missing.append("FIRECRAWL_API_KEY")
    if config["search_backend"] == "brave" and not settings.brave_search_api_key:
        missing.append("BRAVE_SEARCH_API_KEY")
    if config["search_backend"] == "tavily" and not settings.tavily_api_key:
        missing.append("TAVILY_API_KEY")
    if (config["email_provider"] == "hunter" or config["verify_emails"]) and not settings.hunter_api_key:
        missing.append("HUNTER_API_KEY")
    if config["people_provider"] == "apollo" and not settings.apollo_api_key:
        missing.append("APOLLO_API_KEY")
    return missing


def _frame_signature(frame: pd.DataFrame) -> str:
    normalized = frame.fillna("").astype(str)
    hashed = pd.util.hash_pandas_object(normalized, index=True).values.tobytes()
    columns = "\x1f".join(str(column) for column in frame.columns).encode("utf-8")
    return hashlib.sha1(columns + hashed).hexdigest()[:12]


def _has_csv_value(value) -> bool:
    if value is None:
        return False
    if isinstance(value, float) and pd.isna(value):
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, dict, tuple, set)):
        return bool(value)
    return True


def _nonempty_column_count(frame: pd.DataFrame, column: str) -> int:
    if column not in frame.columns:
        return 0
    return int(frame[column].map(_has_csv_value).sum())


def _seed_source_value(row: dict) -> str:
    organization = str(row.get("source_org", "") or "").strip()
    if organization:
        return organization
    source_urls = row.get("source_urls", []) or []
    if isinstance(source_urls, str):
        try:
            source_urls = json.loads(source_urls)
        except json.JSONDecodeError:
            source_urls = [source_urls]
    if isinstance(source_urls, (list, tuple)):
        for url in source_urls:
            value = str(url or "").strip()
            if value:
                return value
    return str(row.get("source_url", "") or "").strip()


def _with_seed_source_column(frame: pd.DataFrame) -> pd.DataFrame:
    enriched = frame.copy()
    enriched["seed_source"] = [
        _seed_source_value(row) for row in enriched.to_dict(orient="records")
    ]
    return enriched


def _metric(card, label: str, value, definition: str) -> None:
    """Render a metric with Streamlit's built-in question-mark hover tooltip."""
    card.metric(label, value, help=definition)


_PRIORITY_SCORE_HELP = (
    "A 0-100 ranking score, capped at 100. It is calculated as: "
    "eligibility confidence x 50 + the average of innovation, scalability, "
    "international-potential, and competition-fit scores x 3 + a source-priority "
    "bonus (priority 1 = 9, 2 = 6, 3 = 3) + an eligibility gate "
    "(qualified = 20, review = 5, not qualified = 0). The four screening scores "
    "are LLM assessments, not eligibility evidence."
)
_VERIFICATION_STATUS_HELP = (
    "Email deliverability status. valid: the verifier considers the address deliverable. "
    "accept_all: the domain accepts any address, so the specific mailbox is not confirmed. "
    "invalid: the verifier considers the address undeliverable. unknown: the verifier could "
    "not determine deliverability. unverified: no verification result is available."
)
_ELIGIBILITY_STATUS_HELP = (
    "Whether the startup meets the competition's founder eligibility rule. qualified: "
    "at least one founder is a current student or graduated in the configured cutoff year "
    "or later; candidates from pre-vetted student competitions are also treated as "
    "qualified unless their founder identity needs review. review: public evidence is "
    "missing, ambiguous, or conflicts with a pre-vetted source. not_qualified: all "
    "available founder evidence is older than the cutoff year and none is unknown."
)


def _table_column_config(columns, link_columns: list[str] | None = None) -> dict:
    column_set = set(columns)
    config = {
        column: st.column_config.LinkColumn(column.replace("_", " ").title())
        for column in (link_columns or [])
        if column in column_set
    }
    if "priority_score" in column_set:
        config["priority_score"] = st.column_config.TextColumn(
            "Priority score", help=_PRIORITY_SCORE_HELP
        )
    if "verification_status" in column_set:
        config["verification_status"] = st.column_config.TextColumn(
            "Verification status", help=_VERIFICATION_STATUS_HELP
        )
    if "eligibility_status" in column_set:
        config["eligibility_status"] = st.column_config.TextColumn(
            "Eligibility status", help=_ELIGIBILITY_STATUS_HELP
        )
    return config


def _merge_enrichment_rows(
    base: pd.DataFrame,
    updates: list[dict],
    *,
    row_id_column: str = "__app_row_id",
) -> pd.DataFrame:
    merged = base.reset_index(drop=True).copy().astype(object)
    for update in updates:
        try:
            row_index = int(update.get(row_id_column, ""))
        except (TypeError, ValueError):
            continue
        if row_index < 0 or row_index >= len(merged):
            continue
        for column, value in update.items():
            if column in {row_id_column, "rank"} or not _has_csv_value(value):
                continue
            if column not in merged.columns:
                merged[column] = pd.Series([""] * len(merged), dtype=object)
            merged.at[row_index, column] = value
    return merged


def _csv_download_bytes(frame: pd.DataFrame) -> bytes:
    serializable = frame.copy()
    for column in serializable.columns:
        serializable[column] = serializable[column].map(
            lambda value: json.dumps(value, ensure_ascii=False)
            if isinstance(value, (list, dict)) else value
        )
    return serializable.to_csv(index=False).encode("utf-8-sig")


def _save_merged_csv(frame: pd.DataFrame, path: Path) -> bytes:
    payload = _csv_download_bytes(frame)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return payload


def _read_csv_frame(path: Path) -> pd.DataFrame:
    if not path.exists() or path.stat().st_size == 0:
        return pd.DataFrame()
    try:
        return pd.read_csv(path, keep_default_na=False)
    except pd.errors.EmptyDataError:
        return pd.DataFrame()


def _reserve_run_output_dir() -> Path:
    """Reserve outputs/YYYY-MM-DD_run_NN without reusing an earlier run folder."""
    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    day = datetime.now().astimezone().strftime("%Y-%m-%d")
    run_number = 1
    while True:
        path = OUTPUT_ROOT / f"{day}_run_{run_number:02d}"
        try:
            path.mkdir(parents=False, exist_ok=False)
            return path
        except FileExistsError:
            run_number += 1


def _load_latest_result_from_root() -> dict | None:
    """Load only root-level output files; dated and legacy subfolders are ignored."""
    candidate_path = LATEST_CANDIDATES_PATH
    if not candidate_path.exists():
        candidate_path = OUTPUT_ROOT / "candidates_enriched.csv"
    candidates_df = _read_csv_frame(candidate_path)
    founders_df = _read_csv_frame(LATEST_FOUNDERS_PATH)
    if candidates_df.empty and founders_df.empty:
        return None

    candidates = [
        normalize_candidate_row(row)
        for row in candidates_df.to_dict(orient="records")
    ]
    founders = founders_df.to_dict(orient="records")
    if not founders and candidates:
        founders = build_founder_rows(candidates)

    summary_path = OUTPUT_ROOT / "run_summary.json"
    try:
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        summary = {}
    summary.setdefault("unique_candidates", len(candidates))
    summary.setdefault(
        "qualified",
        sum(row.get("eligibility_status") == "qualified" for row in candidates),
    )
    summary.setdefault("founder_records", len(founders))
    summary.setdefault("sources_attempted", 0)
    summary.setdefault("pages_processed", 0)
    summary.setdefault("errors", 0)
    summary.setdefault("drafts_generated", 0)

    evidence_df = _read_csv_frame(OUTPUT_ROOT / "contact_evidence.csv")
    drafts_df = _read_csv_frame(OUTPUT_ROOT / "outreach_drafts.csv")
    errors_df = _read_csv_frame(OUTPUT_ROOT / "errors.csv")
    return {
        "summary": summary,
        "candidates": candidates,
        "founders": founders,
        "contact_evidence": evidence_df.to_dict(orient="records"),
        "drafts": drafts_df.to_dict(orient="records"),
        "errors": errors_df.to_dict(orient="records"),
        "output_dir": str(OUTPUT_ROOT),
        "loaded_from_root": True,
    }


def _reset_latest_working_state() -> None:
    for key in (
        "candidate_working_revision",
        "founder_working_revision",
        "uploaded_contact_result",
        "founder_contact_result",
    ):
        st.session_state.pop(key, None)


def _publish_latest_candidates(frame: pd.DataFrame, source: str) -> list[dict]:
    """Persist the company-stage dataset and rebuild its founder-stage handoff."""
    frame = frame.reset_index(drop=True).copy()
    _save_merged_csv(frame, LATEST_CANDIDATES_PATH)
    candidates = [
        normalize_candidate_row(row)
        for row in frame.to_dict(orient="records")
    ]
    founders = build_founder_rows(candidates)
    _save_merged_csv(pd.DataFrame(founders), LATEST_FOUNDERS_PATH)
    evidence_rows = []
    query_rows = []
    for candidate in candidates:
        for evidence in candidate.get("contact_evidence", []) or []:
            if isinstance(evidence, dict):
                evidence_rows.append({
                    "startup_name": candidate.get("startup_name", ""),
                    "website": candidate.get("website", ""),
                    "official_domain": candidate.get("official_domain", ""),
                    **evidence,
                })
        for query in candidate.get("search_queries_run", []) or []:
            query_rows.append({
                "startup_name": candidate.get("startup_name", ""),
                "query": query,
            })
    _save_merged_csv(pd.DataFrame(evidence_rows), OUTPUT_ROOT / "contact_evidence.csv")
    _save_merged_csv(pd.DataFrame(query_rows), OUTPUT_ROOT / "search_queries.csv")

    latest = dict(st.session_state.get("result") or {})
    latest["candidates"] = candidates
    latest["founders"] = founders
    latest["output_dir"] = str(OUTPUT_ROOT)
    latest["latest_source"] = source
    latest.setdefault("summary", {})
    latest["summary"].update({
        "unique_candidates": len(candidates),
        "qualified": sum(
            row.get("eligibility_status") == "qualified" for row in candidates
        ),
        "founder_records": len(founders),
    })
    latest["contact_evidence"] = evidence_rows
    st.session_state["result"] = latest
    st.session_state["latest_candidates_source"] = source
    st.session_state["latest_candidates_revision"] = _frame_signature(frame)
    st.session_state["latest_founders_revision"] = _frame_signature(pd.DataFrame(founders))
    _reset_latest_working_state()
    return candidates


def _publish_latest_founders(frame: pd.DataFrame, source: str) -> list[dict]:
    frame = frame.reset_index(drop=True).copy()
    _save_merged_csv(frame, LATEST_FOUNDERS_PATH)
    founders = frame.to_dict(orient="records")
    founder_evidence = []
    founder_queries = []
    for raw_founder in founders:
        founder = normalize_candidate_row(raw_founder)
        for evidence in founder.get("contact_evidence", []) or []:
            if isinstance(evidence, dict):
                founder_evidence.append({
                    "startup_name": founder.get("startup_name", ""),
                    "founder_name": founder.get("founder_name", ""),
                    "website": founder.get("website", ""),
                    "official_domain": founder.get("official_domain", ""),
                    **evidence,
                })
        for query in founder.get("search_queries_run", []) or []:
            founder_queries.append({
                "startup_name": founder.get("startup_name", ""),
                "founder_name": founder.get("founder_name", ""),
                "query": query,
            })

    evidence_frame = pd.concat(
        [_read_csv_frame(OUTPUT_ROOT / "contact_evidence.csv"), pd.DataFrame(founder_evidence)],
        ignore_index=True,
    ).fillna("")
    if not evidence_frame.empty:
        evidence_frame = evidence_frame.drop_duplicates()
    _save_merged_csv(evidence_frame, OUTPUT_ROOT / "contact_evidence.csv")
    query_frame = pd.concat(
        [_read_csv_frame(OUTPUT_ROOT / "search_queries.csv"), pd.DataFrame(founder_queries)],
        ignore_index=True,
    ).fillna("")
    if not query_frame.empty:
        query_frame = query_frame.drop_duplicates()
    _save_merged_csv(query_frame, OUTPUT_ROOT / "search_queries.csv")

    latest = dict(st.session_state.get("result") or {})
    latest["founders"] = founders
    latest["output_dir"] = str(OUTPUT_ROOT)
    latest["latest_founders_source"] = source
    latest.setdefault("summary", {})
    latest["summary"].update({
        "founder_records": len(founders),
        "founder_records_with_email": sum(
            bool(str(row.get("email", "") or "").strip()) for row in founders
        ),
    })
    latest["contact_evidence"] = evidence_frame.to_dict(orient="records")
    st.session_state["result"] = latest
    st.session_state["latest_founders_source"] = source
    st.session_state["latest_founders_revision"] = _frame_signature(frame)
    st.session_state.pop("founder_working_revision", None)
    return founders


def _publish_completed_run(result: dict, run_dir: Path) -> None:
    """Promote a dated run's files to the root-level latest snapshot."""
    for filename in (
        "candidates_raw.csv",
        "candidates_ranked.csv",
        "founders.csv",
        "contact_evidence.csv",
        "search_queries.csv",
        "outreach_drafts.csv",
        "errors.csv",
        "run_summary.json",
    ):
        source = run_dir / filename
        if source.exists():
            (OUTPUT_ROOT / filename).write_bytes(source.read_bytes())

    result["output_dir"] = str(run_dir)
    result["latest_source"] = run_dir.name
    st.session_state["result"] = result
    candidates_df = pd.DataFrame(result.get("candidates", []))
    founders_df = pd.DataFrame(result.get("founders", []))
    st.session_state["latest_candidates_source"] = run_dir.name
    st.session_state["latest_founders_source"] = run_dir.name
    st.session_state["latest_candidates_revision"] = _frame_signature(candidates_df)
    st.session_state["latest_founders_revision"] = _frame_signature(founders_df)
    _reset_latest_working_state()


def _filter_table_rows(
    frame: pd.DataFrame,
    *,
    key_prefix: str,
    search_columns: list[str],
    filter_columns: list[str],
) -> pd.DataFrame:
    filtered = frame.copy()
    search_columns = [column for column in search_columns if column in filtered.columns]
    query = st.text_input(
        "Search table",
        key=f"{key_prefix}_text_filter",
        placeholder="Search names, organisations, countries, industries, or contacts",
    ).strip()
    if query and search_columns:
        matches = pd.Series(False, index=filtered.index)
        for column in search_columns:
            matches |= filtered[column].astype(str).str.contains(
                query, case=False, regex=False, na=False
            )
        filtered = filtered[matches]

    available_filters = [column for column in filter_columns if column in frame.columns]
    if available_filters:
        filter_hosts = st.columns(min(3, len(available_filters)))
        for index, column in enumerate(available_filters):
            options = sorted(
                value for value in frame[column].astype(str).str.strip().unique() if value
            )
            if not options:
                continue
            selected = filter_hosts[index % len(filter_hosts)].multiselect(
                column.replace("_", " ").title(),
                options,
                key=f"{key_prefix}_{column}_filter",
            )
            if selected:
                filtered = filtered[filtered[column].astype(str).str.strip().isin(selected)]
    st.caption(f"Showing {len(filtered):,} of {len(frame):,} rows")
    return filtered


def _founder_source_url(candidate: dict, founder: dict) -> str:
    for field in ("source_url", "evidence_url"):
        value = str(founder.get(field, "") or "").strip()
        if value:
            return value
    for field in ("founder_source_url", "source_url"):
        value = str(candidate.get(field, "") or "").strip()
        if value:
            return value
    source_urls = candidate.get("source_urls", []) or []
    if isinstance(source_urls, str):
        source_urls = [source_urls]
    for value in source_urls:
        value = str(value or "").strip()
        if value:
            return value
    return ""


def _company_website_url(candidate: dict) -> str:
    url = str(
        candidate.get("website") or candidate.get("official_domain") or ""
    ).strip()
    if not url:
        return ""
    return url if "://" in url else f"https://{url.lstrip('/')}"


def _selectable_filtered_table(
    frame: pd.DataFrame,
    *,
    key_prefix: str,
    search_columns: list[str],
    filter_columns: list[str],
    display_columns: list[str] | None = None,
    link_columns: list[str] | None = None,
    selection_label: str = "Select",
    initial_selected_indices: list[int] | None = None,
) -> pd.DataFrame:
    frame = frame.reset_index(drop=True).copy()
    signature = _frame_signature(frame)
    state_key = f"{key_prefix}_{signature}_selected"
    editor_epoch_key = f"{key_prefix}_{signature}_editor_epoch"
    if state_key not in st.session_state:
        st.session_state[state_key] = [str(value) for value in (initial_selected_indices or [])]
    if editor_epoch_key not in st.session_state:
        st.session_state[editor_epoch_key] = 0
    selected = set(str(value) for value in st.session_state.get(state_key, []))

    filtered = _filter_table_rows(
        frame,
        key_prefix=f"{key_prefix}_{signature}",
        search_columns=search_columns,
        filter_columns=filter_columns,
    )
    visible_ids = set(filtered.index.astype(str))
    select_col, clear_col, count_col = st.columns([1, 1, 2])
    count_placeholder = count_col.empty()
    if select_col.button("Select all shown", key=f"{key_prefix}_{signature}_select_all"):
        st.session_state[state_key] = sorted(selected | visible_ids, key=int)
        st.session_state[editor_epoch_key] += 1
        st.rerun()
    if clear_col.button("Clear selection", key=f"{key_prefix}_{signature}_clear"):
        st.session_state[state_key] = []
        st.session_state[editor_epoch_key] += 1
        st.rerun()

    if filtered.empty:
        count_placeholder.caption(f"{len(selected):,} of {len(frame):,} selected")
        st.info("No rows match the current filters.")
        return frame.iloc[[int(value) for value in sorted(selected, key=int)]] if selected else frame.iloc[0:0]

    # Streamlit serializes every column passed to data_editor before applying
    # column_order. Keep nested evidence/history fields out of the editor itself;
    # the selected rows are still returned from the full source dataframe below.
    visible_columns = [
        column for column in (display_columns or list(frame.columns)) if column in frame.columns
    ]
    editor = filtered.loc[:, visible_columns].copy()
    editor.insert(0, "__row_id", editor.index.astype(str))
    editor.insert(1, "#", editor.index + 1)
    editor.insert(2, selection_label, editor["__row_id"].isin(selected))
    visible_token = hashlib.sha1(
        "|".join(editor["__row_id"].tolist()).encode("utf-8")
    ).hexdigest()[:8]
    editor_key = (
        f"{key_prefix}_{signature}_{visible_token}_"
        f"{st.session_state[editor_epoch_key]}_editor"
    )
    column_config = _table_column_config(editor.columns, link_columns)
    column_config[selection_label] = st.column_config.CheckboxColumn(
        selection_label, default=False, width="small"
    )
    st.caption("Tick the rows you want, then apply the selection below the table.")
    with st.form(f"{editor_key}_form", clear_on_submit=False, border=False):
        edited = st.data_editor(
            editor,
            use_container_width=True,
            hide_index=True,
            num_rows="fixed",
            disabled=[column for column in editor.columns if column != selection_label],
            column_order=[selection_label, "#", *visible_columns],
            column_config=column_config,
            key=editor_key,
        )
        selection_submitted = st.form_submit_button(
            "Apply row selection", type="primary", use_container_width=False
        )

    updated_selected = selected
    if selection_submitted:
        checked_visible = set(
            edited.loc[edited[selection_label].fillna(False), "__row_id"].astype(str)
        )
        updated_selected = (selected - visible_ids) | checked_visible
        st.session_state[state_key] = sorted(updated_selected, key=int)
    count_placeholder.caption(f"{len(updated_selected):,} of {len(frame):,} selected")
    selected_indices = [int(value) for value in sorted(updated_selected, key=int)]
    return frame.iloc[selected_indices] if selected_indices else frame.iloc[0:0]


def _render_filterable_table(
    frame: pd.DataFrame,
    *,
    key_prefix: str,
    search_columns: list[str],
    filter_columns: list[str],
    display_columns: list[str] | None = None,
    link_columns: list[str] | None = None,
) -> pd.DataFrame:
    frame = frame.reset_index(drop=True).copy()
    signature = _frame_signature(frame)
    filtered = _filter_table_rows(
        frame,
        key_prefix=f"{key_prefix}_{signature}",
        search_columns=search_columns,
        filter_columns=filter_columns,
    )
    column_config = _table_column_config(filtered.columns, link_columns)
    st.dataframe(
        filtered,
        use_container_width=True,
        hide_index=True,
        column_order=[
            column for column in (display_columns or list(frame.columns)) if column in frame.columns
        ],
        column_config=column_config,
    )
    return filtered


# Preload the root-level latest snapshot once per Streamlit session. Subfolders are
# deliberately excluded so historical dated runs never replace the active dataset.
if "_root_latest_initialized" not in st.session_state:
    root_latest = _load_latest_result_from_root()
    if root_latest:
        st.session_state["result"] = root_latest
        candidate_frame = pd.DataFrame(root_latest.get("candidates", []))
        founder_frame = pd.DataFrame(root_latest.get("founders", []))
        st.session_state["latest_candidates_source"] = (
            LATEST_CANDIDATES_PATH.name
            if LATEST_CANDIDATES_PATH.exists()
            else "candidates_enriched.csv"
        )
        st.session_state["latest_founders_source"] = LATEST_FOUNDERS_PATH.name
        st.session_state["latest_candidates_revision"] = _frame_signature(candidate_frame)
        st.session_state["latest_founders_revision"] = _frame_signature(founder_frame)
    st.session_state["_root_latest_initialized"] = True

# Page-independent source state used by the Sources page and advisor.
seed_file_name = st.session_state.get(
    "active_seed_file", st.session_state.get("run_seed_list", "seed_sources.csv")
)
if not _is_seed_csv(ROOT / "data" / seed_file_name):
    seed_file_name = "seed_sources.csv"
st.session_state["active_seed_file"] = seed_file_name
seed_csv_path = ROOT / "data" / seed_file_name
high_value = seed_file_name != "seed_sources.csv"
P = _HIGH_VALUE_PRESET if high_value else _STD_PRESET
pid = "hv" if high_value else "std"
llm_backend = st.session_state.get(f"run_llm_{pid}", settings.llm_backend)
pick_mode = st.session_state.get(
    f"run_seed_mode_{pid}", "Select seed sources"
).startswith("Select")

workspace_pages = (
    "Sources",
    "Companies",
    "Founders",
    "Contact evidence",
    "Outreach",
    "AI advisor",
)
if st.session_state.get("workspace_page") not in workspace_pages:
    st.session_state["workspace_page"] = "Sources"

with st.sidebar:
    st.markdown(
        '<div class="sidebar-title">University Startup Sourcing Agent</div>'
        '<div class="sidebar-tagline">Task 1 &middot; '
        '<a href="https://lkygbpc.smu.edu.sg/" target="_blank">SMU Startup Competition 2027</a></div>',
        unsafe_allow_html=True,
    )
    st.markdown("### Workspace")
    page = st.radio(
        "Workspace navigation",
        workspace_pages,
        label_visibility="collapsed",
        key="workspace_page",
    )

if page == "Sources":
    st.subheader("Sources and pipeline configuration")
    run_disabled = False

    up = st.file_uploader(
        "Upload source CSV", type="csv", key="seed_import",
        help="Upload a seed_sources-format CSV with a 'Seed URL' column.",
    )
    imported_name = None
    if up is not None:
        upload_bytes = up.getvalue()
        upload_digest = hashlib.sha1(upload_bytes).hexdigest()
        try:
            uploaded_sources = pd.read_csv(io.BytesIO(upload_bytes), keep_default_na=False)
            if "Seed URL" not in uploaded_sources.columns:
                raise ValueError("The source CSV must contain a 'Seed URL' column.")
            if uploaded_sources.empty:
                raise ValueError("The source CSV does not contain any rows.")
            dest = ROOT / "data" / Path(up.name).name
            if st.session_state.get("_seed_import_saved") != upload_digest:
                dest.write_bytes(upload_bytes)
                st.session_state["_seed_import_saved"] = upload_digest
                st.session_state["active_seed_file"] = dest.name
                st.success(f"Imported to data/{dest.name}")
            imported_name = dest.name
            st.caption(
                f"{len(uploaded_sources):,} sources and "
                f"{len(uploaded_sources.columns):,} columns read from {up.name}."
            )
            with st.expander("Preview uploaded sources", expanded=False):
                _render_filterable_table(
                    uploaded_sources,
                    key_prefix="run_source_upload_preview",
                    search_columns=[
                        "Organization / University", "Program / Source Page",
                        "Country", "Seed URL",
                    ],
                    filter_columns=["Country", "Source Type", "Priority (1=highest)"],
                    link_columns=["Seed URL"],
                )
        except Exception as exc:
            st.error(f"Could not import {up.name}: {exc}")

    seed_file_name = imported_name or st.session_state["active_seed_file"]
    seed_csv_path = ROOT / "data" / seed_file_name
    st.caption(f"Active source file: {seed_file_name}")

    high_value = seed_file_name != "seed_sources.csv"
    P = _HIGH_VALUE_PRESET if high_value else _STD_PRESET
    pid = "hv" if high_value else "std"
    if high_value:
        st.success(f"High-value preset active for {seed_file_name}. All settings remain editable.")

    crawler_backend = settings.crawler_backend
    llm_backend = settings.llm_backend
    ollama_model = settings.ollama_model
    google_model = settings.google_model
    claude_model = settings.claude_model
    deepseek_model = settings.deepseek_model

    sources = pd.read_csv(seed_csv_path, keep_default_na=False)
    source_metrics = st.columns(3)
    _metric(
        source_metrics[0], "Sources", len(sources),
        "Total seed-source rows in the selected seed list."
    )
    _metric(
        source_metrics[1], "Countries",
        sources["Country"].nunique() if "Country" in sources else 0,
        "Number of distinct countries represented by the selected seed sources.",
    )
    _metric(
        source_metrics[2], "Priority 1",
        int((sources["Priority (1=highest)"] == 1).sum())
        if "Priority (1=highest)" in sources else 0,
        "Sources marked highest priority in the seed list.",
    )

    max_sources, seed_start_index = P["max_sources"], 1
    source_mode_options = ["Select seed sources", "First N (by priority)"]
    source_mode_key = f"run_seed_mode_{pid}"
    if st.session_state.get(source_mode_key) not in source_mode_options:
        st.session_state[source_mode_key] = source_mode_options[0]
    run_mode = st.radio(
        "Which sources to run",
        source_mode_options,
        key=source_mode_key,
        horizontal=True,
    )
    pick_mode = run_mode.startswith("Select")
    selected_seed_state_key = f"selected_seed_ids::{seed_file_name}"
    ticked_ids: list[str] = list(
        st.session_state.get(selected_seed_state_key, [])
    )

    hidden_source_columns = {
        "ID", "Region", "Crawl Focus", "Suggested Keywords / Paths",
        "Suggested Crawl Frequency", "Eligibility Signals to Extract",
        "Contact Strategy",
    }
    visible_source_columns = [
        column for column in sources.columns
        if column not in hidden_source_columns
    ]
    with st.expander("Source list and selection", expanded=pick_mode):
        if pick_mode:
            initial_seed_indices = (
                sources.index[
                    sources["ID"].astype(str).isin(ticked_ids)
                ].tolist()
                if "ID" in sources.columns else []
            )
            selected_sources = _selectable_filtered_table(
                sources,
                key_prefix=f"source_selection_{seed_file_name}",
                search_columns=[
                    "Organization / University", "Program / Source Page",
                    "Country", "Seed URL",
                ],
                filter_columns=[
                    "Country", "Source Type", "Priority (1=highest)",
                ],
                display_columns=visible_source_columns,
                link_columns=["Seed URL"],
                selection_label="Run?",
                initial_selected_indices=initial_seed_indices,
            )
            if "ID" in selected_sources.columns:
                ticked_ids = selected_sources["ID"].astype(str).tolist()
            else:
                ticked_ids = [str(index + 1) for index in selected_sources.index]
            st.session_state[selected_seed_state_key] = ticked_ids
            st.session_state["selected_seed_ids"] = ticked_ids
            st.caption(f"{len(ticked_ids):,} source(s) selected for this run.")
        else:
            _render_filterable_table(
                sources,
                key_prefix=f"run_sources_{seed_file_name}",
                search_columns=[
                    "Organization / University", "Program / Source Page",
                    "Country", "Seed URL",
                ],
                filter_columns=[
                    "Country", "Source Type", "Priority (1=highest)",
                ],
                display_columns=visible_source_columns,
                link_columns=["Seed URL"],
            )

    with st.expander("Crawling and extraction", expanded=True):
        crawler_backend = st.selectbox(
            "Crawler", CRAWLER_BACKENDS,
            index=_opt_index(CRAWLER_BACKENDS, P["crawler"]), key=f"run_crawler_{pid}",
        )
        llm_backend = st.selectbox(
            "LLM", LLM_BACKENDS,
            index=_opt_index(LLM_BACKENDS, settings.llm_backend, 1), key=f"run_llm_{pid}",
        )
        if llm_backend == "ollama":
            ollama_model = st.text_input(
                "Ollama model", settings.ollama_model, key=f"run_ollama_{pid}"
            )
        elif llm_backend == "google":
            google_model = st.text_input(
                "Google AI Studio model", settings.google_model, key=f"run_google_{pid}"
            )
        elif llm_backend == "claude":
            claude_model = st.text_input(
                "Claude model", settings.claude_model, key=f"run_claude_{pid}"
            )
        elif llm_backend == "deepseek":
            deepseek_model = st.text_input(
                "DeepSeek model", settings.deepseek_model, key=f"run_deepseek_{pid}"
            )

        if not pick_mode:
            max_sources = st.slider("Seed sources", 1, 200, P["max_sources"], key=f"run_sources_{pid}")
            seed_start_index = st.number_input(
                "Starting seed source #", min_value=1, value=1, step=1,
                key=f"run_start_index_{pid}",
            )
        max_priority = st.selectbox(
            "Maximum source priority", [1, 2, 3], index=1, key=f"run_priority_{pid}",
        )
        max_pages = st.slider("Pages per source", 1, 20, P["max_pages"], key=f"run_pages_{pid}")

    st.caption(
        "Sources discovers, qualifies, and ranks startups. Company and founder contact "
        "searches are handled separately in Companies and Founders."
    )

    if crawler_backend == "crawl4ai" and not _is_installed("crawl4ai"):
        run_disabled = True
        st.error("Crawl4AI is selected but is not installed. Install requirements and run `crawl4ai-setup`.")
    missing = []
    llm_key = {
        "openai": ("OPENAI_API_KEY", settings.openai_api_key),
        "google": ("GOOGLE_API_KEY", settings.google_api_key),
        "claude": ("ANTHROPIC_API_KEY", settings.anthropic_api_key),
        "deepseek": ("DEEPSEEK_API_KEY", settings.deepseek_api_key),
    }.get(llm_backend)
    if llm_key and not llm_key[1]:
        missing.append(llm_key[0])
    if crawler_backend == "firecrawl" and not settings.firecrawl_api_key:
        missing.append("FIRECRAWL_API_KEY")
    if missing:
        st.warning("Missing: " + ", ".join(dict.fromkeys(missing)))

if page == "Sources":
    selected_ids = ticked_ids if pick_mode else None
    _pick_blocked = pick_mode and not ticked_ids
    if _pick_blocked:
        st.info("Select one or more source rows above to enable the pipeline.")

    _shared = st.session_state.get("_run_shared")
    _running = bool(_shared) and not _shared.get("done")

    _c_run, _c_stop = st.columns([1, 1])
    if _c_run.button("Run sourcing pipeline", type="primary",
                     disabled=run_disabled or _pick_blocked or _running):
        _stop_event = threading.Event()
        run_output_dir = _reserve_run_output_dir()
        _shared = {
            "log": [], "result": None, "error": None, "done": False,
            "t0": time.time(), "output_dir": str(run_output_dir),
        }

        # Snapshot the config as default args so the background thread is unaffected by reruns.
        def _run_worker(sh=_shared, stop=_stop_event, cfg=dict(
                seed_csv=seed_csv_path, crawler=crawler_backend, llm=llm_backend,
                om=ollama_model, gm=google_model, cm=claude_model, dm=deepseek_model, ms=max_sources, ssi=seed_start_index,
                sids=selected_ids, mp=max_priority, mpp=max_pages,
                output_dir=run_output_dir)):
            try:
                sh["result"] = run_pipeline(
                    seed_csv=cfg["seed_csv"], output_dir=cfg["output_dir"], settings=settings, mode="live",
                    crawler_backend=cfg["crawler"], llm_backend=cfg["llm"], ollama_model=cfg["om"],
                    google_model=cfg["gm"], claude_model=cfg["cm"], deepseek_model=cfg["dm"], max_sources=cfg["ms"],
                    seed_start_index=cfg["ssi"], selected_ids=cfg["sids"], max_priority=cfg["mp"],
                    max_pages_per_source=cfg["mpp"], generate_drafts=False, enrich_contacts=False,
                    progress=lambda m: sh["log"].append(str(m)), should_stop=stop.is_set)
            except Exception as exc:
                sh["error"] = repr(exc)
            finally:
                sh["done"] = True

        threading.Thread(target=_run_worker, daemon=True).start()
        st.session_state["_run_shared"] = _shared
        st.session_state["_run_stop"] = _stop_event
        st.rerun()

    # Stop button (a user action — no auto-rerun involved here).
    if _running and _c_stop.button("⏹ Stop & export partial"):
        st.session_state["_run_stop"].set()
        st.warning("Stopping — the pipeline will finish the current step and write partial results.")

    # Live status refreshes inside an isolated fragment (run_every) rather than
    # re-running the WHOLE app on a poll loop. The repeated full-app reruns were what
    # made the browser tab title/favicon flicker (idle ⇄ running every ~1s). A fragment
    # reruns only itself; when the run finishes it triggers ONE full app rerun so the
    # results render in the main flow below.
    @st.fragment(run_every=1.0 if _running else None)
    def _run_status():
        shared = st.session_state.get("_run_shared")
        if not shared:
            return
        if shared.get("done"):
            st.rerun(scope="app")
            return
        _last = shared["log"][-1] if shared["log"] else "starting …"
        st.info(f"⏳ Running — {_last}")
        st.caption(f"⏱ Elapsed: {int(time.time() - shared['t0'])}s")

    if _running:
        st.caption(f"Run output: {_shared['output_dir']}")
        _run_status()
    elif _shared and _shared.get("done"):
        if _shared.get("error"):
            st.error("Run failed: " + _shared["error"])
        elif _shared.get("result") is not None:
            _publish_completed_run(
                _shared["result"], Path(_shared["output_dir"])
            )
        st.session_state.pop("_run_shared", None)
        st.session_state.pop("_run_stop", None)

    result = st.session_state.get("result")
    if result:
        s = result.get("summary", {})
        if result.get("output_dir"):
            st.caption(f"Latest result: {result['output_dir']}")
        _secs = s.get("elapsed_seconds")
        _tstr = f"  ⏱ {_secs}s" if _secs is not None else ""
        if s.get("stopped"):
            st.warning("⏹ Stopped early — partial results below." + _tstr)
        elif result.get("loaded_from_root"):
            st.info("Latest saved result loaded from the root outputs folder." + _tstr)
        else:
            st.success("✅ Pipeline complete." + _tstr)
        cols = st.columns(6)
        _metric(cols[0], "Companies", s.get("unique_candidates", len(result.get("candidates", []))),
                "Unique startup candidates remaining after deduplication.")
        _metric(cols[1], "Qualified", s.get("qualified", 0),
                "Startups that satisfy the founder student or recent-graduate eligibility rule.")
        _metric(cols[2], "Founders", s.get("founder_records", len(result.get("founders", []))),
                "Founder records extracted from the discovered startup candidates.")
        _metric(cols[3], "Sources", s.get("sources_attempted", 0),
                "Seed sources included in the completed pipeline run.")
        _metric(cols[4], "Pages", s.get("pages_processed", 0),
                "Source pages successfully crawled and supplied to the extractor.")
        _metric(cols[5], "Errors", s.get("errors", len(result.get("errors", []))),
                "Crawl, extraction, or drafting errors recorded during the run.")
        st.json(s)

if page == "Companies":
    st.subheader("Company contact search")
    uploaded_file = st.file_uploader(
        "Replace latest companies with a candidates CSV",
        type="csv",
        key="candidate_csv_upload",
        help="Without an upload, this tab uses candidates_ranked.csv from the root outputs folder.",
    )
    uploaded_rows = []
    uploaded_df = pd.DataFrame()
    if uploaded_file is not None:
        try:
            candidate_upload_bytes = uploaded_file.getvalue()
            candidate_upload_digest = hashlib.sha1(candidate_upload_bytes).hexdigest()
            if st.session_state.get("candidate_upload_applied") != candidate_upload_digest:
                uploaded_candidate_frame = pd.read_csv(
                    io.BytesIO(candidate_upload_bytes), keep_default_na=False
                )
                if uploaded_candidate_frame.empty:
                    raise ValueError("The uploaded CSV does not contain any company rows.")
                _publish_latest_candidates(uploaded_candidate_frame, uploaded_file.name)
                st.session_state["candidate_upload_applied"] = candidate_upload_digest
                st.success(
                    f"{uploaded_file.name} replaced the latest Companies dataset."
                )
        except Exception as exc:
            st.error(f"Could not read {uploaded_file.name}: {exc}")

    latest_result = st.session_state.get("result") or {}
    latest_candidates = pd.DataFrame(latest_result.get("candidates", []))
    latest_revision = st.session_state.get("latest_candidates_revision")
    if latest_revision is None:
        latest_revision = _frame_signature(latest_candidates)
        st.session_state["latest_candidates_revision"] = latest_revision
    if st.session_state.get("candidate_working_revision") != latest_revision:
        st.session_state["candidate_working_df"] = latest_candidates.reset_index(drop=True).copy()
        st.session_state["candidate_working_revision"] = latest_revision
        st.session_state.pop("uploaded_contact_result", None)
    uploaded_df = _with_seed_source_column(
        st.session_state.get("candidate_working_df", pd.DataFrame())
    )

    if uploaded_df.empty:
        st.info(
            "No companies are available. Complete Run or upload a candidates CSV above."
        )
    else:
        source_label = st.session_state.get(
            "latest_candidates_source", LATEST_CANDIDATES_PATH.name
        )
        st.caption(
            f"{len(uploaded_df):,} companies loaded from the latest dataset: {source_label}."
        )
        qualified_count = (
            int((uploaded_df["eligibility_status"] == "qualified").sum())
            if "eligibility_status" in uploaded_df.columns else 0
        )
        startup_count = len({
            str(row.get("startup_name", "") or "").strip()
            for row in uploaded_df.to_dict(orient="records")
            if str(row.get("startup_name", "") or "").strip()
        })
        company_metrics = st.columns(3)
        _metric(company_metrics[0], "Startups", startup_count,
                "Distinct startup names in the active Companies dataset.")
        _metric(company_metrics[1], "Qualified", qualified_count,
                "Companies marked qualified by the startup eligibility assessment.")
        _metric(company_metrics[2], "Websites",
                _nonempty_column_count(uploaded_df, "website"),
                "Companies with a website URL available for company-level contact research.")
        try:
            selected_candidates = _selectable_filtered_table(
                uploaded_df,
                key_prefix="candidate_search_selection",
                search_columns=[
                    "startup_name", "industry", "country", "website",
                    "official_domain", "seed_source", "company_email", "contact_email",
                ],
                filter_columns=[
                    "country", "industry", "eligibility_status", "contact_waterfall_status",
                ],
                display_columns=[
                    "startup_name", "seed_source", "industry", "country", "website", "official_domain",
                    "company_email", "contact_email", "eligibility_status", "priority_score",
                ],
                link_columns=["website"],
                selection_label="Search?",
            )
            selected_candidates = selected_candidates.copy()
            selected_candidates["__app_row_id"] = selected_candidates.index.astype(str)
            uploaded_rows = selected_candidates.to_dict(orient="records")
            st.caption(
                f"{len(uploaded_rows):,} company row(s) selected. Hunter may consume "
                "one or more credits per selected row."
            )
        except Exception as exc:
            st.error(f"Could not display the latest Companies dataset: {exc}")

    with st.expander("Company search settings", expanded=True):
        candidate_crawler = st.selectbox(
            "Website and search-result crawler", CRAWLER_BACKENDS,
            index=_opt_index(CRAWLER_BACKENDS, settings.crawler_backend),
            key="candidate_crawler",
        )
        candidate_contact = _contact_waterfall_settings(
            "candidate", _STD_PRESET, enrichment_scope="company"
        )
        candidate_missing = _contact_configuration_warnings(candidate_crawler, candidate_contact)
        if candidate_missing:
            st.warning("Missing: " + ", ".join(dict.fromkeys(candidate_missing)))

    candidate_crawler_unavailable = (
        candidate_crawler == "crawl4ai" and not _is_installed("crawl4ai")
    )
    if candidate_crawler_unavailable:
        st.error("Crawl4AI is selected but is not installed. Install requirements and run `crawl4ai-setup`.")
    upload_disabled = not uploaded_rows or candidate_crawler_unavailable

    if st.button("Search for company contacts", type="primary", disabled=upload_disabled):
        status = st.empty()
        try:
            batch_rows = uploaded_rows
            status.info(f"Processing {len(batch_rows)} selected companies.")
            result = enrich_candidates_with_contacts(
                candidates=batch_rows,
                output_dir=UPLOAD_OUTPUT_DIR,
                settings=settings,
                mode="live",
                crawler_backend=candidate_crawler,
                contact_max_pages=candidate_contact["contact_max_pages"],
                search_backend=candidate_contact["search_backend"],
                email_provider=candidate_contact["email_provider"],
                people_provider=candidate_contact["people_provider"],
                search_max_results=candidate_contact["search_max_results"],
                search_max_pages_per_candidate=candidate_contact["search_max_pages"],
                search_queries_per_founder=candidate_contact["queries_per_founder"],
                enable_founder_search=candidate_contact["enable_founder_search"],
                enable_pdf_search=candidate_contact["enable_pdf_search"],
                verify_emails=candidate_contact["verify_emails"],
                paid_fallback_only=candidate_contact["paid_fallback_only"],
                apollo_reveal_personal_emails=candidate_contact["apollo_reveal_personal"],
                apollo_run_waterfall_email=candidate_contact["apollo_waterfall"],
                enrichment_scope="company",
                progress=lambda msg: status.info(msg),
            )
            merged_candidates = _merge_enrichment_rows(
                st.session_state["candidate_working_df"], result["candidates"]
            )
            merged_name = "candidates_ranked_with_company_search.csv"
            merged_path = UPLOAD_OUTPUT_DIR / merged_name
            merged_payload = _save_merged_csv(merged_candidates, merged_path)
            _publish_latest_candidates(merged_candidates, "Companies contact search")
            st.session_state["candidate_working_df"] = merged_candidates
            st.session_state["candidate_working_revision"] = st.session_state[
                "latest_candidates_revision"
            ]
            result["merged_csv_path"] = str(merged_path)
            result["merged_csv_name"] = merged_name
            result["merged_csv_bytes"] = merged_payload
            result["candidates"] = st.session_state["result"]["candidates"]
            result["founders"] = st.session_state["result"]["founders"]
            st.session_state["uploaded_contact_result"] = result
            status.success(
                "Company contact search complete. Results replaced the root-level "
                "candidates_ranked.csv and refreshed founders.csv."
            )
        except Exception as exc:
            st.exception(exc)

    contact_result = st.session_state.get("uploaded_contact_result")
    if contact_result:
        s = contact_result["summary"]
        cols = st.columns(5)
        _metric(cols[0], "Uploaded", s["candidates_uploaded"],
                "Company rows processed in the latest company contact search.")
        _metric(cols[1], "Any contact", s["candidates_with_any_contact"],
                "Processed companies with any usable contact address recorded.")
        _metric(cols[2], "Evidence rows", s["contact_evidence_rows"],
                "Published-source email findings retained as contact evidence.")
        _metric(cols[3], "Search calls", s["search_api_calls"],
                "Web-search API requests made during the latest company search.")
        _metric(cols[4], "Errors", s["errors"],
                "Rows that encountered an error during the latest company search.")

        enriched_df = pd.DataFrame(contact_result["candidates"]).drop(
            columns=["__app_row_id"], errors="ignore"
        )
        evidence_df = pd.DataFrame(contact_result.get("contact_evidence", []))
        st.subheader("Enriched candidates")
        _render_filterable_table(
            enriched_df,
            key_prefix="candidate_enriched_results",
            search_columns=[
                "startup_name", "industry", "country", "website", "official_domain",
                "company_email", "contact_email",
            ],
            filter_columns=["country", "industry", "eligibility_status", "contact_waterfall_status"],
            link_columns=["website"],
        )
        st.subheader("Contact evidence")
        _render_filterable_table(
            evidence_df,
            key_prefix="candidate_contact_evidence",
            search_columns=[
                "startup_name", "founder_name", "email", "source_url", "provider",
            ],
            filter_columns=["provider", "origin", "email_type", "verification_status"],
            link_columns=["source_url"],
        )

        download_dir = Path(contact_result["output_dir"])
        if contact_result.get("merged_csv_bytes"):
            st.download_button(
                "Download complete CSV with appended search results",
                contact_result["merged_csv_bytes"],
                contact_result.get("merged_csv_name", "candidates_with_search_results.csv"),
                "text/csv",
                key="download_merged_candidate_search_results",
            )
        for filename, label in [
            ("candidates_enriched.csv", "Download enriched candidates"),
            ("founders.csv", "Download founders"),
            ("contact_evidence.csv", "Download contact evidence"),
            ("search_queries.csv", "Download search queries"),
            ("errors.csv", "Download errors"),
            ("run_summary.json", "Download run summary"),
        ]:
            path = download_dir / filename
            if path.exists():
                mime = "application/json" if filename.endswith(".json") else "text/csv"
                st.download_button(label, path.read_bytes(), filename, mime)

if page == "Outreach":
    # Native Streamlit outreach UI (replaces the embedded send_email_app.html iframe),
    # styled to match that reference design, with server-side SMTP sending built in.

    # --- Data source: uploaded CSV / latest founder-stage snapshot ------------
    _src = st.file_uploader(
        "Load a candidates CSV (or uses the last pipeline / contact-waterfall run)",
        type="csv", key="outreach_csv")
    if _src is not None:
        _cands = pd.read_csv(_src, keep_default_na=False).to_dict(orient="records")
    elif (st.session_state.get("result") or {}).get("founders"):
        _cands = st.session_state["result"]["founders"]
    else:
        _cands = None

    if not _cands:
        st.info("No founders loaded yet. Complete Run and Founders, or upload a CSV "
                "above to start drafting outreach.")
    else:
        _rows_all = _outreach_rows(_cands)
        _smtp_conf = bool(getattr(settings, "smtp_host", "")) and bool(getattr(settings, "smtp_user", ""))

        # ---- Email template (mirrors the HTML "Edit email template" modal) ----
        _tpl = _load_email_template()
        with st.expander("✎ Edit email template", expanded=False):
            _subj = st.text_input("Subject", _tpl["subject"], key="o_subj")
            _body = st.text_area("Body", _tpl["body"], height=240, key="o_body")
            st.caption("Placeholders: {founder_first}, {founder_name}, {founder_role}, "
                       "{startup_name}, {industry}, {country}, {startup_url}, {competition_name}.")
            if st.button("Save as default template", key="o_save_tpl"):
                _save_email_template(_subj, _body)
                st.success("Template saved.")

        # ---- Toolbar: filters + send method (mirrors the HTML toolbar) ----
        with st.container(border=True):
            _tc = st.columns([1, 1, 1, 1, 1.25, 0.8])
            _countries = sorted({r["country"] for r in _rows_all if r["country"]})
            _industries = sorted({r["industry"] for r in _rows_all if r["industry"]})
            _f_country = _tc[0].selectbox("Country", ["All countries", *_countries], key="o_country")
            _f_industry = _tc[1].selectbox("Industry", ["All industries", *_industries], key="o_industry")
            _f_email_type = _tc[2].selectbox(
                "Email type",
                ["All", "Personal only", "Non-personal only ⚠️"],
                key="o_email_type_filter",
                help=(
                    "Non-personal emails are shared mailboxes such as info@, "
                    "contact@, admin@, or team@."
                ),
            )
            _send_via = _tc[3].selectbox(
                "Send via", ["Email client (mailto)", "SMTP (auto)"], key="o_method"
            )
            _f_search = _tc[4].text_input(
                "Search", key="o_search", placeholder="startup or founder name…"
            )
            _f_email_only = _tc[5].checkbox("Has email only", key="o_email_only")

        # Outreach always targets the founder email; company mailboxes remain visible as evidence.
        _rows = [_with_recipient(r, "Founder email") for r in _rows_all]

        def _keep(r: dict) -> bool:
            if _f_country != "All countries" and r["country"] != _f_country:
                return False
            if _f_industry != "All industries" and r["industry"] != _f_industry:
                return False
            recipient_email = _recipient_email(r)
            non_personal = _is_non_personal_email(
                recipient_email, r.get("recipient_email_type", "")
            )
            if _f_email_only and not recipient_email:
                return False
            if _f_email_type == "Personal only" and (
                not recipient_email or non_personal
            ):
                return False
            if _f_email_type == "Non-personal only ⚠️" and (
                not recipient_email or not non_personal
            ):
                return False
            if _f_search.strip():
                hay = f"{r['startup_name']} {r['founder_name']}".lower()
                if _f_search.strip().lower() not in hay:
                    return False
            return True

        _rows = [r for r in _rows if _keep(r)]
        _n_email = sum(1 for r in _rows if _recipient_email(r))
        _n_non_personal = sum(
            _is_non_personal_email(
                _recipient_email(r), r.get("recipient_email_type", "")
            )
            for r in _rows
        )
        st.markdown(
            f'<div class="outreach-count">{len(_rows)} shown of {len(_rows_all)} entries '
            f'· {_n_email} with an email · {_n_non_personal} non-personal</div>',
            unsafe_allow_html=True,
        )

        if not _rows:
            st.warning("No entries match the current filters.")
        else:
            # ---- Selectable recipient table with clickable source, web, and LinkedIn links ----
            _preselect = st.checkbox("Preselect all shown rows that have an email",
                                     value=True, key="o_preselect")

            def _mailto(r: dict) -> str:
                """A personalised mailto: draft for this row, or '' when it has no email."""
                em = _recipient_email(r)
                if not em:
                    return ""
                return ("mailto:" + quote(em)
                        + "?subject=" + quote(_render_template(_subj, r))
                        + "&body=" + quote(_render_template(_body, r)))

            _table = pd.DataFrame([{
                "row_id": r["row_id"],
                "Startup": r["startup_name"] or "(unnamed)",
                "startup_url": r.get("startup_url", ""),
                "Industry": r["industry"],
                "Country": r["country"],
                "Founder": r["founder_name"],
                "founder_source_url": r.get("founder_source_url", ""),
                "Contact": (
                    _recipient_email(r)
                    + (" ⚠️" if _email_warning(
                        _recipient_email(r), r.get("recipient_email_type", "")
                    ) else "")
                ),
                "LinkedIn": r.get("founder_linkedin", ""),
                "Email": _mailto(r),
            } for r in _rows])
            _all_visible_ids = set(_table["row_id"].astype(str))
            _selected_ids_key = "o_selected_outreach_row_ids"
            _preselect_state_key = "o_preselect_last_value"
            if _selected_ids_key not in st.session_state:
                st.session_state[_selected_ids_key] = []
            if st.session_state.get(_preselect_state_key) != _preselect:
                current = set(st.session_state[_selected_ids_key]) - _all_visible_ids
                if _preselect:
                    current |= {
                        str(row["row_id"])
                        for _, row in _table.loc[_table["Email"].astype(bool)].iterrows()
                    }
                st.session_state[_selected_ids_key] = sorted(current)
                st.session_state[_preselect_state_key] = _preselect

            _selected_ids = set(st.session_state[_selected_ids_key])
            _preselected_rows = [
                index for index, row_id in enumerate(_table["row_id"].astype(str))
                if row_id in _selected_ids
            ]
            _text_link, _email_link, _linkedin_link = _outreach_grid_renderers()
            _grid_builder = GridOptionsBuilder.from_dataframe(_table)
            _grid_builder.configure_default_column(
                editable=False, sortable=True, resizable=True, filter=False
            )
            _grid_builder.configure_selection(
                "multiple",
                use_checkbox=True,
                header_checkbox=True,
                pre_selected_rows=_preselected_rows,
                suppressRowClickSelection=True,
            )
            _grid_builder.configure_column("row_id", hide=True)
            _grid_builder.configure_column("startup_url", hide=True)
            _grid_builder.configure_column("founder_source_url", hide=True)
            _grid_builder.configure_column(
                "Startup",
                cellRenderer=_text_link,
                cellRendererParams={"linkField": "startup_url"},
                headerTooltip="Open the startup's website.",
            )
            _grid_builder.configure_column(
                "Founder",
                cellRenderer=_text_link,
                cellRendererParams={"linkField": "founder_source_url"},
                headerTooltip="Open the source page where this founder was identified.",
            )
            _grid_builder.configure_column(
                "Contact",
                headerTooltip="⚠️ indicates a shared or otherwise non-personal mailbox.",
            )
            _grid_builder.configure_column(
                "LinkedIn",
                cellRenderer=_linkedin_link,
                cellRendererParams={"iconUrl": _linkedin_icon_data_uri()},
                width=88,
                maxWidth=88,
                headerTooltip="Open the founder's LinkedIn profile when available.",
            )
            _grid_builder.configure_column(
                "Email",
                cellRenderer=_email_link,
                width=94,
                maxWidth=94,
                headerTooltip="Open a personalised draft in your email client.",
            )
            _grid_result = AgGrid(
                _table,
                gridOptions=_grid_builder.build(),
                height=460,
                allow_unsafe_jscode=True,
                data_return_mode="AS_INPUT",
                update_on=[],
                show_toolbar=False,
                show_search=False,
                show_download_button=False,
                key=f"o_editor_{len(_rows)}",
            )
            _selected_grid = _grid_result.selected_rows
            if st.button("Apply selected recipients", key="o_apply_selection") and _selected_grid is not None:
                _checked_visible_ids = set(_selected_grid["row_id"].astype(str))
                _selected_ids = (_selected_ids - _all_visible_ids) | _checked_visible_ids
                st.session_state[_selected_ids_key] = sorted(_selected_ids)
                st.rerun()
            _sel = [
                row for row in _rows
                if str(row["row_id"]) in _selected_ids and _recipient_email(row)
            ]

            # ---- Preview first selected email ----
            if _sel:
                _r0 = _sel[0]
                with st.expander(
                        f"Preview first email — {_r0['founder_name'] or _r0['startup_name']} "
                        f"<{_recipient_email(_r0)}>", expanded=False):
                    st.code(f"To: {_recipient_email(_r0)}\n"
                            f"Subject: {_render_template(_subj, _r0)}\n\n"
                            f"{_render_template(_body, _r0)}", language=None)

            # ---- Send ----
            if _send_via.startswith("SMTP"):
                if _smtp_conf:
                    st.caption(f"SMTP: {settings.smtp_user} via "
                               f"{settings.smtp_host}:{settings.smtp_port}")
                else:
                    st.warning("Set SMTP_HOST / SMTP_USER / SMTP_PASSWORD in .env to send via SMTP. "
                               "For a personal Gmail, create an App Password at "
                               "https://myaccount.google.com/apppasswords (needs 2-Step Verification).")
                _b1, _b2 = st.columns([1, 2])
                if _b1.button("Test SMTP connection", disabled=not _smtp_conf, key="o_smtp_test"):
                    try:
                        with SmtpClient(settings) as _c:
                            _c.verify()
                        st.success("SMTP login succeeded — ready to send.")
                    except Exception as exc:  # noqa: BLE001
                        st.error(f"SMTP login failed: {exc}")
                if _b2.button(f"✉ Send {len(_sel)} email(s) via SMTP", type="primary",
                              disabled=not _sel or not _smtp_conf, key="o_smtp_send"):
                    _msgs = [{
                        "to": _recipient_email(r),
                        "subject": _render_template(_subj, r),
                        "body": _render_template(_body, r),
                    } for r in _sel]
                    with st.spinner(f"Sending {len(_msgs)} email(s) via SMTP …"):
                        _res = _smtp_send_messages(_msgs)
                    if _res.get("error"):
                        st.error(_res["error"])
                    else:
                        _sent = sum(1 for x in _res["results"] if x.get("status") == "sent")
                        (st.success if _sent == len(_res["results"]) else st.warning)(
                            f"Sent {_sent} of {len(_res['results'])} via {settings.smtp_user}.")
                        _fail = [x for x in _res["results"] if x.get("status") != "sent"]
                        if _fail:
                            st.dataframe(pd.DataFrame(_fail), use_container_width=True,
                                         hide_index=True)
            else:
                st.caption("Opens each draft in your email client (mailto). Browsers open "
                           "one at a time — click each button below.")
                if not _sel:
                    st.info("Tick one or more rows in the table to prepare drafts.")
                for r in _sel[:50]:
                    _url = ("mailto:" + quote(_recipient_email(r))
                            + "?subject=" + quote(_render_template(_subj, r))
                            + "&body=" + quote(_render_template(_body, r)))
                    st.link_button(
                        f"✉ {r['founder_name'] or r['startup_name']} — {_recipient_email(r)}",
                        _url)
                if len(_sel) > 50:
                    st.caption(f"Showing the first 50 of {len(_sel)} drafts.")


if page == "Founders":
    st.subheader("Founder email search")
    founders_upload = st.file_uploader(
        "Replace latest founders with a founders CSV",
        type="csv",
        key="founders_csv_upload",
        help="Without an upload, this tab uses founders.csv from the root outputs folder.",
    )

    founders_df = pd.DataFrame()
    selected_founders_df = pd.DataFrame()
    founders_source = st.session_state.get(
        "latest_founders_source", LATEST_FOUNDERS_PATH.name
    )
    if founders_upload is not None:
        try:
            founder_upload_bytes = founders_upload.getvalue()
            founder_upload_digest = hashlib.sha1(founder_upload_bytes).hexdigest()
            if st.session_state.get("founder_upload_applied") != founder_upload_digest:
                uploaded_founders = pd.read_csv(
                    io.BytesIO(founder_upload_bytes), keep_default_na=False
                )
                if uploaded_founders.empty:
                    raise ValueError("The uploaded CSV does not contain any founder rows.")
                _publish_latest_founders(uploaded_founders, founders_upload.name)
                st.session_state["founder_upload_applied"] = founder_upload_digest
                st.success(
                    f"{founders_upload.name} replaced the latest Founders dataset."
                )
            founders_source = founders_upload.name
        except Exception as exc:
            st.error(f"Could not read {founders_upload.name}: {exc}")

    latest_founders = pd.DataFrame(
        (st.session_state.get("result") or {}).get("founders", [])
    )
    latest_founder_revision = st.session_state.get("latest_founders_revision")
    if latest_founder_revision is None:
        latest_founder_revision = _frame_signature(latest_founders)
        st.session_state["latest_founders_revision"] = latest_founder_revision
    if st.session_state.get("founder_working_revision") != latest_founder_revision:
        st.session_state["founder_working_df"] = latest_founders.reset_index(drop=True).copy()
        st.session_state["founder_working_revision"] = latest_founder_revision
        st.session_state.pop("founder_contact_result", None)
    founders_df = st.session_state.get("founder_working_df", pd.DataFrame()).copy()
    founders_source = st.session_state.get("latest_founders_source", founders_source)

    if founders_df.empty:
        st.info(
            "No founders are available. Complete Run, complete Companies, or upload "
            "a founders CSV above."
        )
    else:
        st.caption(
            f"{len(founders_df):,} founders loaded from the latest dataset: {founders_source} "
            f"across {len(founders_df.columns):,} columns."
        )
        founder_metric_rows = founders_df.to_dict(orient="records")
        founders_with_email = sum(
            _has_csv_value(row.get("email")) for row in founder_metric_rows
        )
        personal_email_count = sum(
            _is_personal_email(row.get("email", ""), row.get("email_type", ""))
            for row in founder_metric_rows
        )
        startup_count = len({
            str(row.get("startup_name", "") or "").strip()
            for row in founder_metric_rows
            if str(row.get("startup_name", "") or "").strip()
        })
        founder_metrics = st.columns(5)
        _metric(founder_metrics[0], "Founders", len(founders_df),
                "Founder rows in the active Founders dataset.")
        _metric(founder_metrics[1], "Startups", startup_count,
                "Distinct startups represented by the active founder rows.")
        _metric(founder_metrics[2], "Contactable", personal_email_count,
                "Founders with a valid personal email address, excluding shared or company mailboxes.")
        _metric(founder_metrics[3], "Other emails",
                founders_with_email - personal_email_count,
                "Founders with an email that is shared, company-level, generic, or invalid.")
        _metric(founder_metrics[4], "LinkedIn",
                _nonempty_column_count(founders_df, "linkedin_url"),
                "Founders with a LinkedIn profile URL available for person-level research.")
        st.caption(
            "Contactable counts valid personal email addresses. Other emails include "
            "shared, company, generic, or invalid addresses."
        )
        selected_founders_df = _selectable_filtered_table(
            founders_df,
            key_prefix="founder_search_selection",
            search_columns=[
                "startup_name", "founder_name", "university", "industry", "country",
                "website", "official_domain", "linkedin_url", "email",
            ],
            filter_columns=[
                "country", "industry", "founder_status", "provider", "verification_status",
            ],
            display_columns=[
                "startup_name", "founder_name", "industry", "country", "website",
                "official_domain", "linkedin_url", "email", "provider", "verification_status",
            ],
            link_columns=["website", "linkedin_url", "email_source_url"],
            selection_label="Search?",
        )
        st.caption(
            f"{len(selected_founders_df):,} founder(s) selected. Rows that already contain "
            "an email retain their existing evidence."
        )
        st.download_button(
            "Download founders.csv",
            founders_df.to_csv(index=False).encode("utf-8-sig"),
            "founders.csv",
            "text/csv",
            key="download_founders_tab_csv",
        )

    with st.expander("Email search settings", expanded=True):
        founder_crawler = st.selectbox(
            "Founder search-result crawler", CRAWLER_BACKENDS,
            index=_opt_index(CRAWLER_BACKENDS, settings.crawler_backend),
            key="founder_crawler",
        )
        founder_contact = _contact_waterfall_settings(
            "founder", _FOUNDER_SEARCH_PRESET, enrichment_scope="founder"
        )
        founder_missing = _contact_configuration_warnings(founder_crawler, founder_contact)
        if founder_missing:
            st.warning("Missing: " + ", ".join(dict.fromkeys(founder_missing)))

    founder_crawler_unavailable = (
        founder_crawler == "crawl4ai" and not _is_installed("crawl4ai")
    )
    if founder_crawler_unavailable:
        st.error("Crawl4AI is selected but is not installed. Install requirements and run `crawl4ai-setup`.")

    founder_search_disabled = (
        selected_founders_df.empty or founder_crawler_unavailable
    )
    if st.button(
        "Search for founder emails", type="primary",
        disabled=founder_search_disabled, key="run_founder_email_search",
    ):
        founder_status = st.empty()
        try:
            selected_founders_df = selected_founders_df.copy()
            selected_founders_df["__app_row_id"] = selected_founders_df.index.astype(str)
            founder_rows = selected_founders_df.to_dict(orient="records")
            founder_status.info(f"Searching contacts for {len(founder_rows)} founders.")
            founder_result = enrich_candidates_with_contacts(
                candidates=founder_rows,
                output_dir=FOUNDER_OUTPUT_DIR,
                settings=settings,
                mode="live",
                crawler_backend=founder_crawler,
                contact_max_pages=founder_contact["contact_max_pages"],
                search_backend=founder_contact["search_backend"],
                email_provider=founder_contact["email_provider"],
                people_provider=founder_contact["people_provider"],
                search_max_results=founder_contact["search_max_results"],
                search_max_pages_per_candidate=founder_contact["search_max_pages"],
                search_queries_per_founder=founder_contact["queries_per_founder"],
                enable_founder_search=founder_contact["enable_founder_search"],
                enable_pdf_search=founder_contact["enable_pdf_search"],
                verify_emails=founder_contact["verify_emails"],
                paid_fallback_only=founder_contact["paid_fallback_only"],
                apollo_reveal_personal_emails=founder_contact["apollo_reveal_personal"],
                apollo_run_waterfall_email=founder_contact["apollo_waterfall"],
                enrichment_scope="founder",
                progress=lambda msg: founder_status.info(msg),
            )
            founder_updates = []
            for candidate in founder_result.get("candidates", []):
                row_id = candidate.get("__app_row_id", "")
                built_rows = build_founder_rows([candidate])
                update = built_rows[0] if built_rows else {
                    "startup_name": candidate.get("startup_name", ""),
                    "founder_name": candidate.get("founder_name", ""),
                    "email": candidate.get("founder_email", "") or candidate.get("contact_email", ""),
                    "linkedin_url": candidate.get("founder_linkedin_url", ""),
                }
                update["__app_row_id"] = row_id
                for history_column in (
                    "contact_evidence", "search_queries_run", "all_public_emails",
                    "contact_enrichment_errors", "contact_methods_attempted",
                    "providers_attempted", "contact_enrichment_status",
                    "contact_waterfall_status",
                ):
                    if _has_csv_value(candidate.get(history_column)):
                        update[history_column] = candidate[history_column]
                founder_updates.append(update)
            merged_founders = _merge_enrichment_rows(
                st.session_state["founder_working_df"], founder_updates
            )
            founder_merged_name = (
                f"{Path(founders_upload.name).stem}_with_search_results.csv"
                if founders_upload is not None else "founders_with_search_results.csv"
            )
            founder_merged_path = FOUNDER_OUTPUT_DIR / founder_merged_name
            founder_merged_payload = _save_merged_csv(
                merged_founders, founder_merged_path
            )
            latest_founder_rows = _publish_latest_founders(
                merged_founders, "Founders email search"
            )
            st.session_state["founder_working_df"] = merged_founders
            st.session_state["founder_working_revision"] = st.session_state[
                "latest_founders_revision"
            ]
            founder_result["merged_csv_path"] = str(founder_merged_path)
            founder_result["merged_csv_name"] = founder_merged_name
            founder_result["merged_csv_bytes"] = founder_merged_payload
            founder_result["founders"] = latest_founder_rows
            st.session_state["founder_contact_result"] = founder_result
            st.session_state["founder_contact_source"] = founders_source
            founder_status.success(
                "Founder email search complete. Results replaced the root-level founders.csv."
            )
        except Exception as exc:
            st.exception(exc)

    founder_result = st.session_state.get("founder_contact_result")
    if founder_result:
        st.divider()
        st.subheader("Latest email-search results")
        st.caption(f"Input: {st.session_state.get('founder_contact_source', 'founders data')}")
        founder_summary = founder_result["summary"]
        founder_metrics = st.columns(5)
        _metric(founder_metrics[0], "Processed", founder_summary["candidates_uploaded"],
                "Founder rows processed in the latest person-level email search.")
        _metric(founder_metrics[1], "Founder emails", founder_summary["founder_emails_found"],
                "Processed founders with an email found or retained after enrichment.")
        _metric(founder_metrics[2], "Evidence rows", founder_summary["contact_evidence_rows"],
                "Published-source email findings retained as contact evidence.")
        _metric(founder_metrics[3], "Search calls", founder_summary["search_api_calls"],
                "Web-search API requests made during the latest founder search.")
        _metric(founder_metrics[4], "Errors", founder_summary["errors"],
                "Rows that encountered an error during the latest founder search.")

        enriched_founders_df = pd.DataFrame(founder_result.get("founders", []))
        _render_filterable_table(
            enriched_founders_df,
            key_prefix="founder_enriched_results",
            search_columns=[
                "startup_name", "founder_name", "industry", "country", "website",
                "official_domain", "linkedin_url", "email",
            ],
            filter_columns=["country", "industry", "provider", "verification_status"],
            link_columns=["website", "linkedin_url", "email_source_url"],
        )

        founder_download_dir = Path(founder_result["output_dir"])
        if founder_result.get("merged_csv_bytes"):
            st.download_button(
                "Download complete founders CSV with appended search results",
                founder_result["merged_csv_bytes"],
                founder_result.get("merged_csv_name", "founders_with_search_results.csv"),
                "text/csv",
                key="download_merged_founder_search_results",
            )
        for filename, label in [
            ("founders.csv", "Download enriched founders"),
            ("candidates_enriched.csv", "Download enriched candidate records"),
            ("contact_evidence.csv", "Download contact evidence"),
            ("search_queries.csv", "Download search history"),
            ("errors.csv", "Download errors"),
            ("run_summary.json", "Download run summary"),
        ]:
            path = founder_download_dir / filename
            if path.exists():
                mime = "application/json" if filename.endswith(".json") else "text/csv"
                st.download_button(
                    label, path.read_bytes(), filename, mime,
                    key=f"founder_result_{filename}",
                )

if page == "Contact evidence":
    result = st.session_state.get("result")
    if not result:
        st.info("Complete Run, Companies, or Founders first.")
    else:
        rows = list(result.get("contact_evidence", []) or [])
        for c in result.get("candidates", []):
            for e in c.get("contact_evidence", []) or []:
                if isinstance(e, dict):
                    rows.append({"startup_name": c.get("startup_name", ""), **e})
        for raw_founder in result.get("founders", []):
            founder = normalize_candidate_row(raw_founder)
            for evidence in founder.get("contact_evidence", []) or []:
                if isinstance(evidence, dict):
                    rows.append({
                        "startup_name": founder.get("startup_name", ""),
                        "founder_name": founder.get("founder_name", ""),
                        **evidence,
                    })
        df = pd.DataFrame(rows).drop_duplicates() if rows else pd.DataFrame()
        if df.empty:
            st.info("No contact evidence was recorded in the latest pipeline run.")
        else:
            _render_filterable_table(
                df,
                key_prefix="pipeline_contact_evidence",
                search_columns=[
                    "startup_name", "founder_name", "email", "source_url", "provider",
                ],
                filter_columns=["provider", "origin", "email_type", "verification_status"],
                link_columns=["source_url"],
            )

if page == "Outreach":
    _draft_result = st.session_state.get("result")
    if _draft_result and _draft_result.get("drafts"):
        st.divider()
        with st.expander("Generated drafts from the last pipeline run", expanded=False):
            st.dataframe(pd.DataFrame(_draft_result["drafts"]), use_container_width=True, hide_index=True)


if page == "AI advisor":
    st.subheader("AI advisor")
    st.caption(
        "Ask about **this** task: eligibility, choosing seeds, tuning the run, reading "
        "the results, or the contact waterfall. The advisor is given the task brief plus "
        "a live snapshot of your selected seed list and the current run's candidates.")

    # Which chat-capable LLM to use. Defaults to the sidebar's LLM backend when it
    # can chat and its key is set, else the first configured one (DeepSeek first).
    _key_for = {
        "openai": settings.openai_api_key,
        "deepseek": settings.deepseek_api_key,
        "google": settings.google_api_key,
        "claude": settings.anthropic_api_key,
    }

    def _advisor_available(b: str) -> bool:
        return b == "ollama" or bool(_key_for.get(b))

    _adv_order = ["deepseek", "openai", "google", "claude", "ollama"]
    # Prefer the sidebar's LLM when it has a real key; else the first cloud backend
    # with a key (works immediately); else fall back to local Ollama.
    if llm_backend in _adv_order and _key_for.get(llm_backend):
        _default_adv = llm_backend
    else:
        _default_adv = next((b for b in _adv_order if _key_for.get(b)), "ollama")

    _ac1, _ac2, _ac3 = st.columns([1.1, 1.4, 0.8])
    _adv_backend = _ac1.selectbox(
        "Advisor model", _adv_order, index=_adv_order.index(_default_adv),
        format_func=lambda b: b + ("" if _advisor_available(b) else "  (no key)"),
        key="advisor_backend")
    _adv_model = ""
    if _adv_backend == "deepseek":
        _adv_model = _ac2.text_input("DeepSeek model", advisor.DEFAULT_ADVISOR_MODEL,
                                     key="advisor_ds_model")
    if _ac3.button("Clear chat"):
        st.session_state["advisor_history"] = []

    if not _advisor_available(_adv_backend):
        _need = {"openai": "OPENAI_API_KEY", "deepseek": "DEEPSEEK_API_KEY",
                 "google": "GOOGLE_API_KEY", "claude": "ANTHROPIC_API_KEY"}.get(_adv_backend)
        st.warning(f"Set {_need} in .env to use the {_adv_backend} advisor "
                   "(or pick a configured model above / run a local Ollama).")

    st.session_state.setdefault("advisor_history", [])
    _history = st.session_state["advisor_history"]
    for _m in _history:
        st.chat_message(_m["role"]).write(_m["content"])

    if _prompt := st.chat_input("e.g. Which seeds should I run first for European deep-tech?"):
        _history.append({"role": "user", "content": _prompt})
        st.chat_message("user").write(_prompt)
        try:
            _seeds_df = pd.read_csv(seed_csv_path)
        except Exception:
            _seeds_df = None
        _result = st.session_state.get("result")
        _cands = _result.get("candidates") if _result else None
        with st.chat_message("assistant"):
            with st.spinner("Thinking …"):
                try:
                    _reply = advisor.chat_reply(
                        settings, _adv_backend, _history,
                        seeds_df=_seeds_df, candidates=_cands, model=_adv_model)
                except Exception as exc:  # noqa: BLE001 — surface any provider error in-chat
                    _reply = f"⚠️ Couldn't get a reply: {exc}"
            st.write(_reply)
        _history.append({"role": "assistant", "content": _reply})

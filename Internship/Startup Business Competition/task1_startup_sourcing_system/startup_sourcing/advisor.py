"""Built-in AI advisor for the Task 1 startup-sourcing app.

A plain conversational assistant with preset context about *this* task —
what makes a startup eligible, how the pipeline is wired, what a good seed
looks like, and how to read the results. The task brief plus a live snapshot
(the selected seed catalog and, if a run has produced them, the current
candidates) is rebuilt and resent every turn, so the bot reasons over the
app's current state rather than a stale memory of it.

`chat_reply()` runs one turn against whichever LLM backend the sidebar
selected (openai | deepseek | google | claude | ollama). "heuristic"/"mock"
have no model behind them and are rejected — a keyless fallback cannot hold a
conversation.
"""

from __future__ import annotations

import json

import pandas as pd

from .config import Settings

# DeepSeek is a strong, low-cost default for this reasoning-over-context chat.
DEFAULT_ADVISOR_MODEL = "deepseek-v4-flash"

_MAX_HISTORY = 12   # messages sent per turn (the tail of the conversation)


def _task_brief(settings: Settings) -> str:
    cutoff = settings.graduation_cutoff_year
    return f"""You are the built-in advisor of the University Startup Sourcing app (Task 1), chatting with its operator inside the app. Be concise, concrete and practical.

THE TASK
Find and invite eligible student / recent-graduate startups worldwide to apply to the {settings.competition_name} ({settings.competition_year}), run by the {settings.organizer_name}.

ELIGIBILITY (the one rule everything serves)
A startup qualifies when at least one founder is a current university/college student, OR graduated in {cutoff} or later (within five years of the {settings.competition_year} competition). Older-graduate or unknown-status founders do not qualify on their own. The pipeline never invents founder status, graduation year, email, website or LinkedIn — missing evidence means 'review', not a guess.

HOW THE PIPELINE WORKS (the tabs, in order)
1. Sources — choose or upload a vetted seed catalog, inspect/filter/select its sources, crawl the selected pages, and use an LLM to extract and screen candidate startups. Duplicates are merged and survivors ranked by screening scores (innovation, scalability, international potential, competition fit). Good seeds are TEAM / PORTFOLIO / COHORT roster pages that list many ventures with founder names; a company's own homepage is a poor seed.
2. Companies — the ranked company table; upload or use the latest candidates CSV to search only for company contacts.
3. Founders — one row per founder with status/graduation evidence and person-level email search.
4. Contact evidence — every email found and the page it came from.
5. Outreach — personalised invitation drafts; sending is always a manual step by a human, never automatic.

THE CONTACT WATERFALL (how emails are found, published-only, never guessed)
Optional and off by default. When on: crawl the startup's own site for a public address, then web/news SEARCH (Brave or Tavily) over founder/company queries and public PDFs, then optionally the paid providers Hunter (published addresses) and Apollo. Addresses are only ever taken from a real published source.

KEY SETTINGS YOU CAN ADVISE ON
- Seed list + 'which sources to run' (first-N by priority, or select specific rows) + max sources + max priority + pages per source.
- LLM backend: openai / deepseek / google (gemini) / claude / ollama (local). Crawler: firecrawl / crawl4ai.
- Contact enrichment: on/off, search backend (brave/tavily/none), results & pages per startup, founder-level search, PDF search, Hunter/Apollo providers.

HARD TRUTHS TO RESPECT
- Settings decide who is crawled and how contacts are found; they cannot make an ineligible founder eligible. New candidates come only from adding/crawling more (good) seeds.
- Prefer roster/cohort pages over homepages. Fewer high-signal seeds with deeper crawling usually beats many shallow ones.
- Trust the live snapshot below over any assumption about the current state. If you don't have a number, say so rather than inventing one.

Answer the operator's latest message. Keep replies practical and under ~250 words unless they ask for more."""


def _seed_snapshot(seeds_df: pd.DataFrame | None) -> str:
    if seeds_df is None or seeds_df.empty:
        return "Seed catalog: (none loaded)."
    df = seeds_df
    n = len(df)
    lines = [f"Seed catalog: {n} sources."]
    if "Country" in df.columns:
        top = df["Country"].astype(str).replace("", pd.NA).dropna().value_counts().head(6)
        if not top.empty:
            lines.append("By country: " + ", ".join(f"{k} ({v})" for k, v in top.items()) + ".")
    if "Priority (1=highest)" in df.columns:
        p1 = int((df["Priority (1=highest)"].astype(str) == "1").sum())
        lines.append(f"Priority-1 sources: {p1}.")
    # A short sample so the bot can reference real catalog entries.
    org_col = next((c for c in ("Organization / University", "Organization", "Organisation")
                    if c in df.columns), None)
    if org_col:
        sample = [str(x).strip() for x in df[org_col].head(12) if str(x).strip()]
        if sample:
            lines.append("Example sources: " + "; ".join(sample) + ".")
    return "\n".join(lines)


def _results_snapshot(candidates: list | None) -> str:
    if not candidates:
        return "Current run: no candidates in this session yet (the Sources tab has not produced results, or none were uploaded)."
    df = pd.DataFrame(candidates)
    n = len(df)
    lines = [f"Current run: {n} candidate startup(s) in this session."]

    def _count(col: str, values: set[str]) -> int:
        if col not in df.columns:
            return 0
        return int(df[col].astype(str).str.lower().isin(values).sum())

    elig = _count("eligibility_status", {"qualified", "eligible"})
    review = _count("eligibility_status", {"review"})
    if "eligibility_status" in df.columns:
        lines.append(f"Eligibility: ~{elig} qualified, ~{review} to review.")
    with_email = 0
    for c in ("contact_email", "founder_email"):
        if c in df.columns:
            with_email = max(with_email, int(df[c].astype(str).str.contains("@", na=False).sum()))
    lines.append(f"With at least one email: ~{with_email}.")
    for col, label in (("country", "Countries"), ("industry", "Industries")):
        if col in df.columns:
            top = df[col].astype(str).replace("", pd.NA).dropna().value_counts().head(5)
            if not top.empty:
                lines.append(f"{label}: " + ", ".join(f"{k} ({v})" for k, v in top.items()) + ".")
    return "\n".join(lines)


def build_context(settings: Settings, seeds_df: pd.DataFrame | None,
                  candidates: list | None) -> str:
    """The full system prompt: preset task brief + live snapshot."""
    return (
        f"{_task_brief(settings)}\n\n"
        "LIVE SNAPSHOT (this app, right now)\n"
        f"{_seed_snapshot(seeds_df)}\n\n"
        f"{_results_snapshot(candidates)}"
    )


# ---------------- LLM chat backends ----------------

def _chat_openai_compatible(settings: Settings, backend: str, system: str,
                            history: list, model: str) -> str:
    from openai import OpenAI

    if backend == "deepseek":
        if not settings.deepseek_api_key:
            raise ValueError("DEEPSEEK_API_KEY is required for the deepseek advisor.")
        client = OpenAI(api_key=settings.deepseek_api_key,
                        base_url="https://api.deepseek.com/v1")
        model = model or settings.deepseek_model
    else:
        if not settings.openai_api_key:
            raise ValueError("OPENAI_API_KEY is required for the openai advisor.")
        client = OpenAI(api_key=settings.openai_api_key)
        model = model or settings.openai_model
    messages = [{"role": "system", "content": system}, *history]
    completion = client.chat.completions.create(
        model=model, temperature=0.3, messages=messages)
    return completion.choices[0].message.content or ""


def _chat_google(settings: Settings, system: str, history: list, model: str) -> str:
    import requests

    if not settings.google_api_key:
        raise ValueError("GOOGLE_API_KEY is required for the google advisor.")
    model = model or settings.google_model
    contents = [{"role": "model" if m["role"] == "assistant" else "user",
                 "parts": [{"text": m["content"]}]} for m in history]
    resp = requests.post(
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
        params={"key": settings.google_api_key},
        json={"systemInstruction": {"parts": [{"text": system}]},
              "contents": contents,
              "generationConfig": {"temperature": 0.3}},
        timeout=60,
    )
    resp.raise_for_status()
    parts = resp.json().get("candidates", [{}])[0].get("content", {}).get("parts", [])
    return "\n".join(str(p.get("text", "")) for p in parts if p.get("text"))


def _chat_claude(settings: Settings, system: str, history: list, model: str) -> str:
    import requests

    if not settings.anthropic_api_key:
        raise ValueError("ANTHROPIC_API_KEY is required for the claude advisor.")
    model = model or settings.claude_model
    messages = [{"role": m["role"], "content": m["content"]} for m in history]
    resp = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={"x-api-key": settings.anthropic_api_key,
                 "anthropic-version": "2023-06-01",
                 "content-type": "application/json"},
        json={"model": model, "max_tokens": 1500, "system": system,
              "messages": messages},
        timeout=60,
    )
    resp.raise_for_status()
    payload = resp.json()
    return "\n".join(str(b.get("text", "")) for b in payload.get("content", [])
                     if b.get("type") == "text" and b.get("text"))


def _chat_ollama(settings: Settings, system: str, history: list, model: str) -> str:
    from ollama import Client

    client = Client(host=settings.ollama_host)
    messages = [{"role": "system", "content": system}, *history]
    response = client.chat(model=model or settings.ollama_model,
                           messages=messages, stream=False)
    return response.message.content or ""


def chat_reply(settings: Settings, backend: str, history: list,
               *, seeds_df: pd.DataFrame | None = None,
               candidates: list | None = None, model: str = "") -> str:
    """One advisor turn. Returns the assistant's plain-text reply.

    history: [{"role": "user"|"assistant", "content": str}, ...] ending with
    the operator's newest message. Only the last _MAX_HISTORY are sent.
    """
    backend = (backend or "").lower()
    if backend in ("", "mock", "heuristic"):
        raise ValueError("The advisor needs an LLM backend (openai, deepseek, "
                         "google, claude or ollama) — a keyless/mock backend "
                         "cannot hold a conversation.")
    system = build_context(settings, seeds_df, candidates)
    trimmed = [{"role": m["role"], "content": str(m["content"])}
               for m in history[-_MAX_HISTORY:]]
    if backend in ("openai", "deepseek"):
        return _chat_openai_compatible(settings, backend, system, trimmed, model).strip()
    if backend == "google":
        return _chat_google(settings, system, trimmed, model).strip()
    if backend == "claude":
        return _chat_claude(settings, system, trimmed, model).strip()
    if backend == "ollama":
        return _chat_ollama(settings, system, trimmed, model).strip()
    raise ValueError(f"{backend!r} is not a supported advisor backend.")

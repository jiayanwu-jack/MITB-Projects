from __future__ import annotations

import json
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


def _redact_url(url: str) -> str:
    parts = url.split("?")
    if len(parts) == 1:
        return url
    query = []
    for part in parts[1].split("&"):
        if part.lower().startswith("api_key="):
            query.append("api_key=<redacted>")
        else:
            query.append(part)
    return parts[0] + "?" + "&".join(query)


def json_request(
    url: str,
    *,
    method: str = "GET",
    params: dict | None = None,
    headers: dict[str, str] | None = None,
    json_body: dict | None = None,
    timeout: float = 30.0,
) -> dict:
    if params:
        query = urlencode({k: v for k, v in params.items() if v is not None})
        url = url + ("&" if "?" in url else "?") + query
    body = None
    req_headers = {
        "Accept": "application/json",
        "User-Agent": "Mozilla/5.0 (compatible; startup-sourcing-pipeline/1.0)",
        **(headers or {}),
    }
    if json_body is not None:
        body = json.dumps(json_body).encode("utf-8")
        req_headers.setdefault("Content-Type", "application/json")
    try:
        import requests

        response = requests.request(
            method,
            url,
            data=body,
            headers=req_headers,
            timeout=timeout,
        )
        raw = response.text
        if response.status_code >= 400:
            raise RuntimeError(
                f"HTTP {response.status_code} from {_redact_url(url)}: {raw[:1000]}"
            )
        return json.loads(raw) if raw.strip() else {}
    except ImportError:
        pass

    req = Request(url, data=body, headers=req_headers, method=method)
    try:
        with urlopen(req, timeout=timeout) as response:
            raw = response.read().decode("utf-8", errors="replace")
            return json.loads(raw) if raw.strip() else {}
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:1000]
        raise RuntimeError(f"HTTP {exc.code} from {_redact_url(url)}: {detail}") from exc
    except URLError as exc:
        raise RuntimeError(f"Request failed for {_redact_url(url)}: {exc}") from exc

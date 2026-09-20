"""Fast, offline unit tests for the deterministic pieces (no network, no LLM)."""
from __future__ import annotations

from sourcer.email_find import candidate_emails
from sourcer.extract import (extract_emails, find_external_links, guess_people,
                             is_role_inbox, looks_like_portfolio, registrable_domain)


def test_registrable_domain():
    assert registrable_domain("www.acme.io") == "acme.io"
    assert registrable_domain("careers.nus.edu.sg") == "nus.edu.sg"
    assert registrable_domain("ACME.CO.UK") == "acme.co.uk"


def test_is_role_inbox():
    assert is_role_inbox("info@acme.io")
    assert is_role_inbox("contact@acme.io")
    assert not is_role_inbox("jane.tan@acme.io")


def test_portfolio_detection_and_external_links():
    html = """<main><h1>Our Portfolio</h1>
        <a href="https://acme.io">Acme Robotics</a>
        <a href="https://linkedin.com/company/acme">LinkedIn</a>
        <a href="/about">About us</a></main>"""
    assert looks_like_portfolio("https://accel.co/portfolio", "Portfolio", "Our Portfolio")
    assert not looks_like_portfolio("https://accel.co/contact", "Contact", "Email us")
    links = find_external_links(html, "https://accel.co/portfolio")
    urls = {u for _, u in links}
    assert urls == {"https://acme.io"}          # LinkedIn + same-site /about excluded


def test_guess_people_name_role_email():
    html = ("<main><ul>"
            "<li>Jane Tan, Co-Founder &amp; CEO - "
            "<a href='mailto:jane.tan@acme.io'>jane.tan@acme.io</a></li>"
            "<li>General enquiries: <a href='mailto:hello@acme.io'>hello@acme.io</a></li>"
            "</ul></main>")
    people = {p[0]: p for p in guess_people(html)}
    assert "Jane Tan" in people
    name, role, email = people["Jane Tan"]
    assert "founder" in role.lower()
    assert email == "jane.tan@acme.io"
    # The role inbox must not become a "person".
    assert not any(is_role_inbox(e) for _, _, e in people.values() if e)


def test_extract_emails_filters_assets():
    html = ("<main><a href='mailto:hello@club.org'>hello@club.org</a>"
            "<img src='logo@2x.png'></main>")
    emails = {e for e, _ in extract_emails(html)}
    assert "hello@club.org" in emails
    assert not any("2x.png" in e for e in emails)


def test_env_loader_sets_without_override():
    import os
    import tempfile
    from pathlib import Path

    from sourcer.env import load_dotenv

    d = Path(tempfile.mkdtemp())
    (d / ".env").write_text(
        "# a comment\n"
        "SMU_ENVTEST_A=alpha\n"
        'export SMU_ENVTEST_B="beta gamma"\n'
        "SMU_ENVTEST_C=from-file\n", encoding="utf-8")
    os.environ.pop("SMU_ENVTEST_A", None)
    os.environ.pop("SMU_ENVTEST_B", None)
    os.environ["SMU_ENVTEST_C"] = "already-set"      # must NOT be overridden

    n = load_dotenv(d / ".env")
    assert n == 2                                     # A and B set; C skipped
    assert os.environ["SMU_ENVTEST_A"] == "alpha"
    assert os.environ["SMU_ENVTEST_B"] == "beta gamma"   # export + quotes handled
    assert os.environ["SMU_ENVTEST_C"] == "already-set"  # real env wins
    assert load_dotenv(d / "missing.env") == 0           # absent file is a no-op


def test_candidate_email_patterns():
    cands = candidate_emails("Jane Tan", "acme.io")
    assert "jane.tan@acme.io" in cands
    assert cands[0] == "jane.tan@acme.io"       # most common pattern first
    assert "jtan@acme.io" in cands


if __name__ == "__main__":
    import sys
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    fails = 0
    for fn in fns:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except AssertionError as e:
            fails += 1
            print(f"FAIL {fn.__name__}: {e}")
    sys.exit(1 if fails else 0)

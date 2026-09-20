"""Fast, offline unit tests for the deterministic pieces (no network)."""
from __future__ import annotations

from crawler.extract import (extract_emails, find_links, find_portfolio_links,
                             is_role_inbox, looks_like_portfolio_page)
from crawler.models import Target
from crawler.rank import choose, guess_person


def test_is_role_inbox():
    assert is_role_inbox("hello@antler.co")
    assert is_role_inbox("info@x.org")
    assert is_role_inbox("innovation@uct.ac.za")
    assert not is_role_inbox("jane.doe@uni.edu")
    assert not is_role_inbox("sam.lee@club.org")


def test_extract_and_deobfuscate():
    html = """<main>
      <a href="mailto:hello@club.org">hello@club.org</a>
      <p>President: sam.lee [at] club [dot] org</p>
      <img src="logo@2x.png">
    </main>"""
    cands = extract_emails(html, "http://x/", "Contact", office_page=False)
    emails = {c.email for c in cands}
    assert "hello@club.org" in emails
    assert "sam.lee@club.org" in emails      # de-obfuscated
    assert not any("2x.png" in e for e in emails)  # asset filtered


def test_directory_guard_keeps_only_role_inbox():
    # 12 individual emails + 1 role inbox -> a "directory"; only the inbox survives.
    people = "".join(
        f'<a href="mailto:person{i}@dept.edu">person{i}</a>' for i in range(12)
    )
    html = f'<main>{people}<a href="mailto:info@dept.edu">info@dept.edu</a></main>'
    cands = extract_emails(html, "http://x/people", "Staff directory", office_page=False)
    assert all(c.directory_page for c in cands)
    best, conf, note = choose(Target("t", "T", "C", "http://dept.edu/", "university"), cands)
    assert best is not None and best.email == "info@dept.edu"
    assert "directory" in note.lower()


def test_org_prefers_role_inbox_over_person():
    html = ('<main><a href="mailto:sam.lee@club.org">Sam Lee, President</a>'
            '<a href="mailto:hello@club.org">hello@club.org</a></main>')
    cands = extract_emails(html, "http://club.org/contact", "Contact", office_page=True)
    best, conf, note = choose(Target("t", "Club", "C", "http://club.org/", "student_club"), cands)
    assert best.email == "hello@club.org"
    assert is_role_inbox(best.email)


def test_startup_prefers_named_founder_over_generic_inbox():
    html = ('<main><a href="mailto:hello@acme.io">General enquiries</a>'
            '<p>Jane Tan, Co-Founder - <a href="mailto:jane.tan@acme.io">jane.tan@acme.io</a></p>'
            '</main>')
    cands = extract_emails(html, "http://acme.io/team", "Team", office_page=False)
    target = Target("s1", "Acme", "SG", "http://acme.io/", "startup")
    best, conf, note = choose(target, cands)
    assert best.email == "jane.tan@acme.io"
    name, role = guess_person(best, target)
    assert name == "Jane Tan"
    assert "founder" in role.lower()


def test_find_links_enqueues_portfolio_style_pages():
    # A link whose text only matches a portfolio hint (no office/contact overlap) must
    # still be scored and returned, or discover() will never crawl it in the first place.
    html = ('<a href="/success-stories">Success Stories</a>'
            '<a href="/random">Random page</a>')
    urls = {u for _, u in find_links(html, "http://uni.edu/")}
    assert "http://uni.edu/success-stories" in urls
    assert "http://uni.edu/random" not in urls


def test_portfolio_page_detection_and_external_link_extraction():
    html = """<main><h1>Our Startup Portfolio</h1>
        <a href="https://acme.io">Acme Robotics</a>
        <a href="https://linkedin.com/company/acme">LinkedIn</a>
        <a href="/about">About the office</a>
        </main>"""
    assert looks_like_portfolio_page(
        "https://uni.edu/entrepreneurship/portfolio", "Startup Portfolio", "Our Startup Portfolio")
    assert not looks_like_portfolio_page(
        "https://uni.edu/contact", "Contact us", "Email the office at info@uni.edu")

    leads = find_portfolio_links(html, "https://uni.edu/entrepreneurship/portfolio")
    urls = {u for _, u in leads}
    assert urls == {"https://acme.io"}          # LinkedIn and same-site /about excluded


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

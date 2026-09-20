from __future__ import annotations

import argparse
import csv

from startup_sourcing.config import settings
from startup_sourcing.outreach import GmailClient


def load_rows(path: str) -> list[dict]:
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def main() -> None:
    parser = argparse.ArgumentParser(description="Create Gmail drafts or send approved outreach emails")
    parser.add_argument("--input", default="outputs/latest/outreach_drafts.csv")
    parser.add_argument("--action", choices=["draft", "send"], default="draft")
    parser.add_argument("--limit", type=int, default=10)
    args = parser.parse_args()

    rows = load_rows(args.input)
    approved = [
        r for r in rows
        if r.get("approval_status", "").strip().lower() == "approved" and r.get("to_email")
    ][: args.limit]
    if not approved:
        print("No approved rows with a recipient email were found.")
        return

    gmail = GmailClient(settings)
    for row in approved:
        if args.action == "draft":
            gmail.create_draft(row["to_email"], row["subject"], row["body"])
            print(f"Draft created: {row['startup_name']} -> {row['to_email']}")
        else:
            gmail.send(row["to_email"], row["subject"], row["body"])
            print(f"Sent: {row['startup_name']} -> {row['to_email']}")


if __name__ == "__main__":
    main()

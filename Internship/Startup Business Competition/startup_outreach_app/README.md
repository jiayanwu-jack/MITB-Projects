# Startup Outreach App

A single-file web app for reviewing sourced startups and sending template invitation
emails to founders. Built to consume `smu_sourcer/output/startups.csv` (or any CSV in
that same format).

## How to use

1. **Open** `index.html` — just double-click it (it runs in any modern browser).
   Nothing is installed and no server is needed.
2. **Upload** your CSV — drag `startups.csv` onto the drop zone, or click to browse.
3. **Review** the table. Each row shows:
   - **Startup** — a clickable link to the startup's website.
   - **Industry**, **Country**.
   - **Founder** name.
   - **Contact** — the founder's email if available; otherwise a link to their LinkedIn
     profile; blank if neither is known.
   - **Email** — a button that opens a pre-filled, personalised draft in your email client
     (blank when there's no email).
4. **Filter** by country, by industry, by a name search, or "Has email only".
5. **Edit the template** — click *Edit email template*. Change the subject/body and *Save*
   (stored in your browser, so it persists between sessions). Use these placeholders,
   which are filled per founder when you send:
   `{founder_name}` `{founder_first}` `{founder_role}` `{startup_name}` `{industry}` `{country}`
6. **Bulk send** — tick the rows you want (or the header checkbox to select all filtered
   rows), then click *Send bulk email*. Because email clients/browsers won't let a web page
   fire off many messages silently, you get two safe options:
   - **Personalised, one at a time** — a stepper opens each founder's draft (fully
     personalised) when you click; you review and hit send in your mail client.
   - **One email to all (BCC)** — opens a single draft with everyone in BCC (the template
     is used as-is, with per-person placeholders replaced by neutral wording).
   You can also **Copy all addresses** to paste elsewhere.

## Notes

- **Privacy:** the CSV is parsed entirely in your browser. No data leaves your machine.
- **Sending:** the app uses `mailto:` links, so your own email program (Outlook, Gmail in
  the browser, Apple Mail, …) opens with the draft ready — you always review before sending.
- **Expected columns:** `startup_name, url, industry, country, founder_name,
  founder_email, founder_linkedin` (extra columns are ignored). This is exactly the
  `smu_sourcer` output format.
- Remember that many founder emails produced by the sourcer are unverified/guessed —
  review each draft before sending.

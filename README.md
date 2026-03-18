# Call Theme Analyzer

A lightweight transcript-analysis MVP for grouping repeated discussion points across customer calls.

## What version 1 does

- Paste transcript text or upload `.txt` / `.md` transcript files
- Store transcripts in SQLite
- Rebuild analysis after every upload
- Surface:
  - common topics
  - repeated objections
  - feature requests
  - integrations mentioned
- Show supporting snippets for each theme

## Run locally

```bash
python3 app.py serve
```

Then open [http://127.0.0.1:8000](http://127.0.0.1:8000).

## Krisp webhook setup

Krisp's official webhook API supports transcript-created events over HTTPS and lets you attach an authorization header to the webhook request. Point Krisp at:

```text
https://your-app-host/webhooks/krisp
```

Recommended environment variables:

```bash
export WEBHOOK_AUTH_HEADER="Authorization"
export WEBHOOK_AUTH_VALUE="Bearer your-shared-secret"
```

The webhook parser is intentionally tolerant and looks for transcript text, notes, meeting title, event id, and timestamps in common JSON keys so you can adapt it as you inspect your exact Krisp payload shape.

## Daily email digest

Set your SMTP configuration:

```bash
export SMTP_HOST="smtp.your-provider.com"
export SMTP_PORT="587"
export SMTP_USERNAME="user"
export SMTP_PASSWORD="password"
export SMTP_FROM="call-digest@yourdomain.com"
export DIGEST_TO="you@yourdomain.com"
export APP_TIMEZONE="America/Boise"
```

Then send the summary for today:

```bash
python3 app.py send-digest
```

Or for a specific day:

```bash
python3 app.py send-digest --date 2026-03-17
```

You can schedule that command with cron, your hosting platform's scheduled jobs, or a Codex automation.

## Notes on the analysis

This MVP uses a rules-and-keyword approach so it can run in a bare Python environment with no external dependencies.

That keeps the product workflow testable right away:

- ingest
- store
- analyze
- review grouped themes
- inspect evidence

A strong next step is replacing `rebuild_analysis()` in [`app.py`](/Users/ryanhanson/Documents/Playground/app.py) with an LLM-backed extraction pipeline once you want better semantic grouping.

## Sample test data

Two example transcripts are included in [`sample_data/acme-discovery.txt`](/Users/ryanhanson/Documents/Playground/sample_data/acme-discovery.txt) and [`sample_data/brightbank-followup.txt`](/Users/ryanhanson/Documents/Playground/sample_data/brightbank-followup.txt).

## Recommended next upgrades

- Add account name, rep name, and call date metadata to transcripts
- Swap the rules engine for an OpenAI-backed theme extraction job
- Add filters by account, date range, and theme category
- Export weekly summaries for Slack or email

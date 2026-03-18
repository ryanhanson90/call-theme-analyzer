```python
#!/usr/bin/env python3
import argparse
import cgi
import html
import json
import os
import re
import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo


BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "data.db"
STATIC_DIR = BASE_DIR / "static"
HOST = "0.0.0.0"
PORT = int(os.environ.get("PORT", "8000"))
APP_TIMEZONE = ZoneInfo(os.environ.get("APP_TIMEZONE", "America/Boise"))
WEBHOOK_AUTH_HEADER = os.environ.get("WEBHOOK_AUTH_HEADER", "Authorization")
WEBHOOK_AUTH_VALUE = os.environ.get("WEBHOOK_AUTH_VALUE", "")
RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "")
RESEND_FROM_EMAIL = os.environ.get("RESEND_FROM_EMAIL", "")
DIGEST_TO = os.environ.get("DIGEST_TO", "")


STOPWORDS = {
    "a", "about", "above", "after", "again", "against", "all", "also", "am",
    "an", "and", "any", "are", "as", "at", "be", "because", "been", "before",
    "being", "below", "between", "both", "but", "by", "can", "could", "did",
    "do", "does", "doing", "down", "during", "each", "few", "for", "from",
    "further", "had", "has", "have", "having", "he", "her", "here", "hers",
    "herself", "him", "himself", "his", "how", "i", "if", "in", "into", "is",
    "it", "its", "itself", "just", "let", "me", "more", "most", "my",
    "myself", "no", "nor", "not", "now", "of", "off", "on", "once", "only",
    "or", "other", "our", "ours", "ourselves", "out", "over", "own", "same",
    "she", "should", "so", "some", "such", "than", "that", "the", "their",
    "theirs", "them", "themselves", "then", "there", "these", "they", "this",
    "those", "through", "to", "too", "under", "until", "up", "very", "was",
    "we", "were", "what", "when", "where", "which", "while", "who", "whom",
    "why", "will", "with", "would", "you", "your", "yours", "yourself",
    "yourselves",
}

COMMON_TOPIC_BLACKLIST = {
    "better",
    "customer",
    "customers",
    "transcript",
    "speaker",
    "call",
    "calls",
    "team",
    "teams",
    "today",
    "tomorrow",
    "thanks",
    "thank",
    "hello",
    "discovery",
    "followup",
    "integration",
    "integrations",
}


@dataclass(frozen=True)
class ThemePattern:
    name: str
    category: str
    patterns: Tuple[str, ...]


@dataclass(frozen=True)
class TranscriptPayload:
    title: str
    source: str
    text: str
    external_id: str = ""
    call_time: str = ""
    metadata_json: str = "{}"


THEME_PATTERNS = [
    ThemePattern("Implementation timeline", "Repeated objections", ("timeline", "implementation time", "rollout", "too long")),
    ThemePattern("Security review", "Repeated objections", ("security", "infosec", "vendor review", "data privacy")),
    ThemePattern("Budget concerns", "Repeated objections", ("budget", "pricing", "cost", "too expensive")),
    ThemePattern("Change management", "Repeated objections", ("change management", "adoption", "internal buy-in", "stakeholder alignment")),
    ThemePattern("Data quality", "Common topics", ("data quality", "clean data", "transaction enrichment", "categorization")),
    ThemePattern("Personalization", "Common topics", ("personalization", "next best action", "insight", "recommendation")),
    ThemePattern("ROI and impact", "Common topics", ("roi", "business case", "impact", "engagement lift")),
    ThemePattern("Reporting needs", "Feature requests", ("dashboard", "reporting", "analytics view", "export")),
    ThemePattern("Alerting and notifications", "Feature requests", ("alert", "notification", "email digest", "slack alert")),
    ThemePattern("Workflow automation", "Feature requests", ("workflow", "approval flow", "automation", "orchestration")),
    ThemePattern("Salesforce", "Integrations mentioned", ("salesforce",)),
    ThemePattern("Slack", "Integrations mentioned", ("slack",)),
    ThemePattern("Microsoft Teams", "Integrations mentioned", ("microsoft teams",)),
    ThemePattern("Snowflake", "Integrations mentioned", ("snowflake",)),
    ThemePattern("Databricks", "Integrations mentioned", ("databricks",)),
    ThemePattern("Zendesk", "Integrations mentioned", ("zendesk",)),
]


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def initialize_database() -> None:
    conn = get_connection()
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS transcripts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            source TEXT NOT NULL,
            text TEXT NOT NULL,
            external_id TEXT,
            call_time TEXT,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS themes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            category TEXT NOT NULL,
            mention_count INTEGER NOT NULL DEFAULT 0,
            transcript_count INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS theme_mentions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            theme_id INTEGER NOT NULL,
            transcript_id INTEGER NOT NULL,
            snippet TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(theme_id) REFERENCES themes(id),
            FOREIGN KEY(transcript_id) REFERENCES transcripts(id)
        );

        CREATE TABLE IF NOT EXISTS daily_digest_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            digest_date TEXT NOT NULL UNIQUE,
            sent_at TEXT NOT NULL,
            recipient TEXT NOT NULL,
            subject TEXT NOT NULL
        );
        """
    )
    for statement in (
        "ALTER TABLE transcripts ADD COLUMN external_id TEXT",
        "ALTER TABLE transcripts ADD COLUMN call_time TEXT",
        "ALTER TABLE transcripts ADD COLUMN metadata_json TEXT NOT NULL DEFAULT '{}'",
    ):
        try:
            conn.execute(statement)
        except sqlite3.OperationalError:
            pass
    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_transcripts_external_id
        ON transcripts(external_id)
        WHERE external_id IS NOT NULL AND external_id != ''
        """
    )
    conn.commit()
    conn.close()


def normalize_whitespace(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def split_sentences(text: str) -> List[str]:
    cleaned = text.replace("\r", " ").replace("\n", " ")
    parts = re.split(r"(?<=[.!?])\s+", cleaned)
    return [normalize_whitespace(part) for part in parts if normalize_whitespace(part)]


def extract_candidate_phrases(text: str) -> List[str]:
    tokens = re.findall(r"[a-zA-Z][a-zA-Z\-']+", text.lower())
    filtered = [token for token in tokens if token not in STOPWORDS and len(token) > 3]
    phrases: List[str] = []
    for index, token in enumerate(filtered):
        phrases.append(token)
        if index < len(filtered) - 1:
            bigram = f"{token} {filtered[index + 1]}"
            phrases.append(bigram)
    return phrases


def create_theme(conn: sqlite3.Connection, name: str, category: str, snippets: List[Tuple[int, str]]) -> None:
    if not snippets:
        return

    transcript_ids = {transcript_id for transcript_id, _ in snippets}
    cursor = conn.execute(
        """
        INSERT INTO themes (name, category, mention_count, transcript_count)
        VALUES (?, ?, ?, ?)
        """,
        (name, category, len(snippets), len(transcript_ids)),
    )
    theme_id = cursor.lastrowid
    now = datetime.utcnow().isoformat()
    conn.executemany(
        """
        INSERT INTO theme_mentions (theme_id, transcript_id, snippet, created_at)
        VALUES (?, ?, ?, ?)
        """,
        [(theme_id, transcript_id, snippet, now) for transcript_id, snippet in snippets],
    )


def rebuild_analysis(conn: sqlite3.Connection) -> None:
    transcripts = conn.execute("SELECT id, title, text FROM transcripts ORDER BY created_at DESC").fetchall()
    conn.execute("DELETE FROM theme_mentions")
    conn.execute("DELETE FROM themes")

    if not transcripts:
        conn.commit()
        return

    known_theme_tokens = {
        token
        for theme in THEME_PATTERNS
        for token in re.findall(r"[a-zA-Z][a-zA-Z\-']+", theme.name.lower())
    }

    for theme in THEME_PATTERNS:
        snippets: List[Tuple[int, str]] = []
        for transcript in transcripts:
            seen_sentences = set()
            for sentence in split_sentences(transcript["text"]):
                lowered = sentence.lower()
                if sentence in seen_sentences:
                    continue
                if any(pattern in lowered for pattern in theme.patterns):
                    snippets.append((transcript["id"], sentence[:280]))
                    seen_sentences.add(sentence)
                    if len(seen_sentences) >= 2:
                        break
        create_theme(conn, theme.name, theme.category, snippets)

    phrase_docs: Dict[str, set] = defaultdict(set)
    phrase_examples: Dict[str, List[Tuple[int, str]]] = defaultdict(list)
    for transcript in transcripts:
        phrases = set(extract_candidate_phrases(transcript["text"]))
        for phrase in phrases:
            phrase_docs[phrase].add(transcript["id"])
            for sentence in split_sentences(transcript["text"]):
                if phrase in sentence.lower():
                    phrase_examples[phrase].append((transcript["id"], sentence[:280]))
                    break

    ranked = []
    for phrase, transcript_ids in phrase_docs.items():
        if len(phrase) < 5:
            continue
        words = phrase.split()
        if any(word in STOPWORDS for word in words):
            continue
        if any(word in COMMON_TOPIC_BLACKLIST for word in words):
            continue
        if all(word in known_theme_tokens for word in words):
            continue
        ranked.append((len(transcript_ids), len(words), phrase))

    ranked.sort(reverse=True)
    common_topic_count = 0
    for transcript_count, _, phrase in ranked:
        if common_topic_count >= 8:
            break
        if transcript_count < 2 and len(transcripts) > 1:
            continue
        examples = phrase_examples.get(phrase, [])
        if not examples:
            continue
        pretty_name = " ".join(word.capitalize() for word in phrase.split())
        create_theme(conn, pretty_name, "Common topics", examples[: min(3, len(examples))])
        common_topic_count += 1

    conn.commit()


def insert_transcript(conn: sqlite3.Connection, title: str, source: str, text: str) -> None:
    upsert_transcript(conn, TranscriptPayload(title=title, source=source, text=text))


def upsert_transcript(conn: sqlite3.Connection, payload: TranscriptPayload) -> None:
    normalized_text = normalize_whitespace(payload.text)
    if not payload.title or not normalized_text:
        return

    existing_id = None
    if payload.external_id:
        row = conn.execute(
            "SELECT id FROM transcripts WHERE external_id = ?",
            (payload.external_id,),
        ).fetchone()
        existing_id = row["id"] if row else None

    if existing_id:
        conn.execute(
            """
            UPDATE transcripts
            SET title = ?, source = ?, text = ?, call_time = ?, metadata_json = ?
            WHERE id = ?
            """,
            (
                payload.title,
                payload.source,
                normalized_text,
                payload.call_time or None,
                payload.metadata_json,
                existing_id,
            ),
        )
    else:
        conn.execute(
            """
            INSERT INTO transcripts (title, source, text, external_id, call_time, metadata_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                payload.title,
                payload.source,
                normalized_text,
                payload.external_id or None,
                payload.call_time or None,
                payload.metadata_json,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
    conn.commit()
    rebuild_analysis(conn)


def coalesce_text(value) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = [coalesce_text(item) for item in value]
        return "\n".join(part for part in parts if part)
    if isinstance(value, dict):
        for key in ("text", "content", "value", "body"):
            if key in value:
                return coalesce_text(value[key])
    return ""


def deep_find(node, key_names: Tuple[str, ...]):
    if isinstance(node, dict):
        for key, value in node.items():
            if key.lower() in key_names:
                return value
            found = deep_find(value, key_names)
            if found is not None:
                return found
    if isinstance(node, list):
        for item in node:
            found = deep_find(item, key_names)
            if found is not None:
                return found
    return None


def extract_krisp_transcript_payload(payload: dict) -> TranscriptPayload:
    transcript_value = deep_find(payload, ("transcript", "transcript_text", "full_transcript", "content"))
    notes_value = deep_find(payload, ("notes", "note", "outline"))
    title_value = deep_find(payload, ("title", "meeting_title", "name", "subject"))
    meeting_id = deep_find(payload, ("meeting_id", "call_id", "recording_id", "transcript_id", "event_id", "id"))
    call_time = deep_find(payload, ("call_time", "meeting_time", "started_at", "created_at", "timestamp"))
    source_name = deep_find(payload, ("source", "provider", "app"))

    transcript_text = coalesce_text(transcript_value)
    notes_text = coalesce_text(notes_value)
    if notes_text and notes_text not in transcript_text:
        transcript_text = f"{transcript_text}\n\nNotes:\n{notes_text}".strip()

    title = coalesce_text(title_value) or "Krisp call"
    source = "Krisp webhook"
    source_label = coalesce_text(source_name)
    if source_label:
        source = f"Krisp webhook ({source_label})"

    return TranscriptPayload(
        title=title,
        source=source,
        text=transcript_text,
        external_id=coalesce_text(meeting_id),
        call_time=coalesce_text(call_time),
        metadata_json=json.dumps(payload, ensure_ascii=True),
    )


def verify_webhook(headers) -> bool:
    if not WEBHOOK_AUTH_VALUE:
        return True
    return headers.get(WEBHOOK_AUTH_HEADER, "") == WEBHOOK_AUTH_VALUE


def get_day_bounds(target_day: date) -> Tuple[str, str]:
    start_local = datetime.combine(target_day, time.min, tzinfo=APP_TIMEZONE)
    end_local = start_local + timedelta(days=1)
    return start_local.astimezone(timezone.utc).isoformat(), end_local.astimezone(timezone.utc).isoformat()


def fetch_digest_rows(conn: sqlite3.Connection, target_day: date) -> List[sqlite3.Row]:
    start_utc, end_utc = get_day_bounds(target_day)
    return conn.execute(
        """
        SELECT
            themes.name,
            themes.category,
            COUNT(DISTINCT theme_mentions.transcript_id) AS transcript_count,
            COUNT(theme_mentions.id) AS mention_count
        FROM theme_mentions
        JOIN themes ON themes.id = theme_mentions.theme_id
        JOIN transcripts ON transcripts.id = theme_mentions.transcript_id
        WHERE COALESCE(NULLIF(transcripts.call_time, ''), transcripts.created_at) >= ?
          AND COALESCE(NULLIF(transcripts.call_time, ''), transcripts.created_at) < ?
        GROUP BY themes.id, themes.name, themes.category
        ORDER BY transcript_count DESC, mention_count DESC, themes.category ASC, themes.name ASC
        """,
        (start_utc, end_utc),
    ).fetchall()


def fetch_digest_snippets(conn: sqlite3.Connection, target_day: date, theme_name: str) -> List[sqlite3.Row]:
    start_utc, end_utc = get_day_bounds(target_day)
    return conn.execute(
        """
        SELECT theme_mentions.snippet, transcripts.title
        FROM theme_mentions
        JOIN themes ON themes.id = theme_mentions.theme_id
        JOIN transcripts ON transcripts.id = theme_mentions.transcript_id
        WHERE themes.name = ?
          AND COALESCE(NULLIF(transcripts.call_time, ''), transcripts.created_at) >= ?
          AND COALESCE(NULLIF(transcripts.call_time, ''), transcripts.created_at) < ?
        ORDER BY transcripts.created_at DESC
        LIMIT 3
        """,
        (theme_name, start_utc, end_utc),
    ).fetchall()


def build_digest_email(conn: sqlite3.Connection, target_day: date) -> Optional[Tuple[str, str]]:
    rows = fetch_digest_rows(conn, target_day)
    if not rows:
        return None

    local_label = target_day.strftime("%B %d, %Y")
    categories: Dict[str, List[sqlite3.Row]] = defaultdict(list)
    for row in rows:
        categories[row["category"]].append(row)

    lines = [f"Call theme summary for {local_label}", "", f"Timezone: {APP_TIMEZONE.key}", ""]
    ordered_categories = (
        "Common topics",
        "Repeated objections",
        "Feature requests",
        "Integrations mentioned",
    )
    for category in ordered_categories:
        category_rows = categories.get(category, [])
        if not category_rows:
            continue
        lines.append(category)
        lines.append("-" * len(category))
        for row in category_rows[:5]:
            lines.append(f"* {row['name']} ({row['transcript_count']} calls, {row['mention_count']} snippets)")
            for snippet in fetch_digest_snippets(conn, target_day, row["name"]):
                lines.append(f'  - {snippet["title"]}: "{snippet["snippet"]}"')
        lines.append("")

    subject = f"Daily call theme summary for {local_label}"
    return subject, "\n".join(lines).strip()


def send_email(subject: str, body: str) -> None:
    required = {
        "RESEND_API_KEY": RESEND_API_KEY,
        "RESEND_FROM_EMAIL": RESEND_FROM_EMAIL,
        "DIGEST_TO": DIGEST_TO,
    }
    missing = [name for name, value in required.items() if not value]
    if missing:
        raise RuntimeError(f"Missing Resend configuration: {', '.join(missing)}")

    payload = json.dumps(
        {
            "from": RESEND_FROM_EMAIL,
            "to": [DIGEST_TO],
            "subject": subject,
            "text": body,
        }
    ).encode("utf-8")

    request = Request(
        "https://api.resend.com/emails",
        data=payload,
        headers={
            "Authorization": f"Bearer {RESEND_API_KEY}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    with urlopen(request, timeout=30) as response:
        if response.status >= 400:
            raise RuntimeError(f"Resend API returned status {response.status}")


def has_digest_been_sent(conn: sqlite3.Connection, target_day: date) -> bool:
    return conn.execute(
        "SELECT 1 FROM daily_digest_log WHERE digest_date = ?",
        (target_day.isoformat(),),
    ).fetchone() is not None


def log_digest_sent(conn: sqlite3.Connection, target_day: date, subject: str) -> None:
    conn.execute(
        """
        INSERT OR REPLACE INTO daily_digest_log (digest_date, sent_at, recipient, subject)
        VALUES (?, ?, ?, ?)
        """,
        (target_day.isoformat(), datetime.now(timezone.utc).isoformat(), DIGEST_TO, subject),
    )
    conn.commit()


def send_daily_digest(target_day: date, force: bool = False) -> str:
    conn = get_connection()
    try:
        if not force and has_digest_been_sent(conn, target_day):
            return f"Digest already sent for {target_day.isoformat()}"
        digest = build_digest_email(conn, target_day)
        if not digest:
            return f"No transcripts found for {target_day.isoformat()}"
        subject, body = digest
        send_email(subject, body)
        log_digest_sent(conn, target_day, subject)
        return f"Digest sent for {target_day.isoformat()} to {DIGEST_TO}"
    finally:
        conn.close()


def fetch_dashboard_data(conn: sqlite3.Connection) -> Dict[str, Iterable[sqlite3.Row]]:
    return {
        "transcripts": conn.execute(
            "SELECT id, title, source, created_at, call_time, substr(text, 1, 180) AS preview FROM transcripts ORDER BY COALESCE(NULLIF(call_time, ''), created_at) DESC"
        ).fetchall(),
        "themes": conn.execute(
            "SELECT id, name, category, mention_count, transcript_count FROM themes ORDER BY transcript_count DESC, mention_count DESC, name ASC"
        ).fetchall(),
        "counts": conn.execute(
            """
            SELECT
                (SELECT COUNT(*) FROM transcripts) AS transcript_count,
                (SELECT COUNT(*) FROM themes) AS theme_count,
                (SELECT COUNT(*) FROM theme_mentions) AS mention_count
            """
        ).fetchone(),
    }


def fetch_theme_detail(conn: sqlite3.Connection, theme_id: int):
    theme = conn.execute(
        "SELECT id, name, category, mention_count, transcript_count FROM themes WHERE id = ?",
        (theme_id,),
    ).fetchone()
    mentions = conn.execute(
        """
        SELECT theme_mentions.snippet, transcripts.title, transcripts.source, transcripts.created_at, transcripts.call_time
        FROM theme_mentions
        JOIN transcripts ON transcripts.id = theme_mentions.transcript_id
        WHERE theme_mentions.theme_id = ?
        ORDER BY COALESCE(NULLIF(transcripts.call_time, ''), transcripts.created_at) DESC
        """,
        (theme_id,),
    ).fetchall()
    return theme, mentions


def render_layout(title: str, body: str) -> bytes:
    page = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{html.escape(title)}</title>
  <link rel="stylesheet" href="/static/styles.css" />
</head>
<body>
  <div class="shell">
    <header class="hero">
      <div>
        <p class="eyebrow">Transcript intelligence MVP</p>
        <h1>Call Theme Analyzer</h1>
        <p class="lede">Upload Krisp exports or paste transcript text, then cluster repeated discussion points across calls.</p>
      </div>
      <div class="hero-note">
        <p>Version 1 keeps the analysis intentionally tight so we can validate the workflow before adding LLM-powered enrichment.</p>
      </div>
    </header>
    {body}
  </div>
</body>
</html>"""
    return page.encode("utf-8")


def render_dashboard(data: Dict[str, Iterable[sqlite3.Row]], message: str = "") -> bytes:
    counts = data["counts"]
    message_html = f'<div class="flash">{html.escape(message)}</div>' if message else ""
    theme_cards = "".join(
        f"""
        <a class="theme-card" href="/themes/{theme['id']}">
          <span class="pill">{html.escape(theme['category'])}</span>
          <h3>{html.escape(theme['name'])}</h3>
          <p>{theme['transcript_count']} calls • {theme['mention_count']} snippets</p>
        </a>
        """
        for theme in data["themes"]
    ) or '<div class="empty">No themes yet. Add a transcript to generate analysis.</div>'

    transcript_rows = "".join(
        f"""
        <article class="transcript-row">
          <div>
            <h3>{html.escape(transcript['title'])}</h3>
            <p class="meta">{html.escape(transcript['source'])} • {html.escape((transcript['call_time'] or transcript['created_at'])[:19].replace('T', ' '))}</p>
            <p>{html.escape(transcript['preview'])}...</p>
          </div>
        </article>
        """
        for transcript in data["transcripts"]
    ) or '<div class="empty">No transcripts stored yet.</div>'

    body = f"""
    {message_html}
    <section class="stats">
      <div class="stat"><span>{counts['transcript_count']}</span><p>Transcripts</p></div>
      <div class="stat"><span>{counts['theme_count']}</span><p>Themes</p></div>
      <div class="stat"><span>{counts['mention_count']}</span><p>Supporting snippets</p></div>
    </section>

    <section class="grid">
      <div class="panel">
        <h2>Paste transcript</h2>
        <form method="post" action="/transcripts/paste">
          <label>Call title<input type="text" name="title" placeholder="Acme discovery call" required /></label>
          <label>Source<input type="text" name="source" value="Pasted transcript" required /></label>
          <label>Transcript text<textarea name="text" rows="12" placeholder="Paste the transcript here..." required></textarea></label>
          <button type="submit">Analyze transcript</button>
        </form>
      </div>

      <div class="panel">
        <h2>Upload transcript files</h2>
        <form method="post" action="/transcripts/upload" enctype="multipart/form-data">
          <label>Source label<input type="text" name="source" value="Krisp export" required /></label>
          <label class="file-input">Choose .txt or .md files<input type="file" name="files" accept=".txt,.md,.text" multiple required /></label>
          <button type="submit">Upload and analyze</button>
        </form>
        <p class="helper">Each file becomes one transcript record and triggers a full theme refresh.</p>
      </div>
    </section>

    <section class="panel panel-callout">
      <div class="panel-header">
        <h2>Krisp automation</h2>
        <p>Send Krisp transcript-created webhooks to <code>/webhooks/krisp</code> and run the daily email digest command on a schedule.</p>
      </div>
    </section>

    <section class="panel">
      <div class="panel-header">
        <h2>Theme dashboard</h2>
        <p>Click any theme to inspect the supporting call snippets.</p>
      </div>
      <div class="theme-grid">{theme_cards}</div>
    </section>

    <section class="panel">
      <div class="panel-header">
        <h2>Stored transcripts</h2>
        <p>These are the source calls currently included in the analysis.</p>
      </div>
      <div class="transcript-list">{transcript_rows}</div>
    </section>
    """
    return render_layout("Call Theme Analyzer", body)


def render_theme_page(theme: sqlite3.Row, mentions: Iterable[sqlite3.Row]) -> bytes:
    mention_html = "".join(
        f"""
        <article class="mention">
          <p class="quote">“{html.escape(mention['snippet'])}”</p>
          <p class="meta">{html.escape(mention['title'])} • {html.escape(mention['source'])} • {html.escape((mention['call_time'] or mention['created_at'])[:19].replace('T', ' '))}</p>
        </article>
        """
        for mention in mentions
    ) or '<div class="empty">No supporting snippets found for this theme.</div>'

    body = f"""
    <section class="panel">
      <a class="back-link" href="/">← Back to dashboard</a>
      <span class="pill">{html.escape(theme['category'])}</span>
      <h2>{html.escape(theme['name'])}</h2>
      <p class="helper">{theme['transcript_count']} calls • {theme['mention_count']} supporting snippets</p>
      <div class="mention-list">{mention_html}</div>
    </section>
    """
    return render_layout(theme["name"], body)


class AppHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/":
            conn = get_connection()
            data = fetch_dashboard_data(conn)
            params = parse_qs(parsed.query)
            message = params.get("message", [""])[0]
            self.respond_html(render_dashboard(data, message))
            conn.close()
            return

        if parsed.path.startswith("/themes/"):
            try:
                theme_id = int(parsed.path.split("/")[-1])
            except ValueError:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            conn = get_connection()
            theme, mentions = fetch_theme_detail(conn, theme_id)
            conn.close()
            if not theme:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            self.respond_html(render_theme_page(theme, mentions))
            return

        if parsed.path == "/send-test-digest":
            try:
                message = send_daily_digest(datetime.now(APP_TIMEZONE).date(), force=True)
            except Exception as exc:
                message = f"Digest test failed: {exc}"
            self.redirect(f"/?message={self.url_quote(message)}")
            return

        if parsed.path.startswith("/static/"):
            file_path = STATIC_DIR / Path(parsed.path).name
            if file_path.exists():
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "text/css; charset=utf-8")
                self.end_headers()
                self.wfile.write(file_path.read_bytes())
                return

        self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/webhooks/krisp":
            if not verify_webhook(self.headers):
                self.send_error(HTTPStatus.UNAUTHORIZED)
                return

            length = int(self.headers.get("Content-Length", "0"))
            raw_body = self.rfile.read(length).decode("utf-8")
            try:
                payload = json.loads(raw_body or "{}")
            except json.JSONDecodeError:
                self.send_error(HTTPStatus.BAD_REQUEST, "Invalid JSON payload")
                return

            transcript_payload = extract_krisp_transcript_payload(payload)
            if not transcript_payload.text.strip():
                self.send_error(HTTPStatus.BAD_REQUEST, "Transcript text not found in webhook payload")
                return

            conn = get_connection()
            upsert_transcript(conn, transcript_payload)
            conn.close()
            self.respond_json({"status": "ok", "title": transcript_payload.title})
            return

        if parsed.path == "/transcripts/paste":
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length).decode("utf-8")
            form = parse_qs(body)
            title = form.get("title", [""])[0]
            source = form.get("source", ["Pasted transcript"])[0]
            text = form.get("text", [""])[0]

            conn = get_connection()
            insert_transcript(conn, title, source, text)
            conn.close()
            self.redirect("/?message=Transcript+added+and+analyzed")
            return

        if parsed.path == "/transcripts/upload":
            form = cgi.FieldStorage(
                fp=self.rfile,
                headers=self.headers,
                environ={
                    "REQUEST_METHOD": "POST",
                    "CONTENT_TYPE": self.headers.get("Content-Type"),
                },
            )
            source = form.getfirst("source", "Uploaded transcript")
            files_field = form["files"] if "files" in form else []
            if not isinstance(files_field, list):
                files_field = [files_field]

            conn = get_connection()
            uploaded = 0
            for item in files_field:
                if not getattr(item, "filename", ""):
                    continue
                raw = item.file.read()
                text = raw.decode("utf-8", errors="ignore")
                title = Path(item.filename).stem.replace("_", " ").replace("-", " ").title()
                insert_transcript(conn, title, source, text)
                uploaded += 1
            conn.close()
            self.redirect(f"/?message=Uploaded+{uploaded}+transcript(s)")
            return

        self.send_error(HTTPStatus.NOT_FOUND)

    def redirect(self, location: str) -> None:
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", location)
        self.end_headers()

    def url_quote(self, value: str) -> str:
        safe = []
        for char in value:
            if char.isalnum() or char in "-_.~":
                safe.append(char)
            elif char == " ":
                safe.append("+")
            else:
                safe.append(f"%{ord(char):02X}")
        return "".join(safe)

    def respond_html(self, payload: bytes) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def respond_json(self, payload: dict) -> None:
        data = json.dumps(payload).encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format: str, *args) -> None:
        return


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Call Theme Analyzer")
    subparsers = parser.add_subparsers(dest="command")

    subparsers.add_parser("serve", help="Run the web server")

    digest_parser = subparsers.add_parser("send-digest", help="Send the daily digest email")
    digest_parser.add_argument("--date", dest="digest_date", help="Digest date in YYYY-MM-DD; defaults to today in app timezone")
    digest_parser.add_argument("--force", action="store_true", help="Send even if this date has already been logged")

    return parser


def run_server() -> None:
    print(f"BINDING SERVER ON {HOST}:{PORT}", flush=True)
    server = ThreadingHTTPServer((HOST, PORT), AppHandler)
    print(f"Serving on http://{HOST}:{PORT}", flush=True)
    server.serve_forever()


def main() -> None:
    print("INSIDE MAIN", flush=True)
    parser = build_parser()
    args = parser.parse_args()
    command = args.command or "serve"

    print("CREATING STATIC DIR", flush=True)
    STATIC_DIR.mkdir(exist_ok=True)

    print("INITIALIZING DATABASE", flush=True)
    initialize_database()

    if command == "send-digest":
        target_day = date.fromisoformat(args.digest_date) if args.digest_date else datetime.now(APP_TIMEZONE).date()
        print(send_daily_digest(target_day, force=args.force))
        return

    run_server()


if __name__ == "__main__":
    import traceback

    try:
        print("APP STARTING", flush=True)
        main()
    except Exception:
        traceback.print_exc()
        raise
```

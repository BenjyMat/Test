"""
GroupMe SMS Internet Browser — powered by Mistral AI (FREE)
─────────────────────────────
  - Any text       → searches DuckDuckGo & summarizes top result
  - A URL          → fetches & summarizes the page
  - !more          → more detail on last page
  - !links         → links from last page
  - !help          → show commands
"""

from flask import Flask, request, jsonify
import requests as req
from bs4 import BeautifulSoup
import sqlite3
import re
import os
from urllib.parse import quote_plus, urlparse, unquote

app = Flask(__name__)

BOT_ID          = os.environ.get("GROUPME_BOT_ID", "ec1fe761adc29359ba5b4d55b8")
MISTRAL_API_KEY = os.environ.get("MISTRAL_API_KEY", "")
DB_PATH         = "browser.db"
MAX_MSG         = 900

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}

# ── Database ──────────────────────────────────────────────────────────────────

def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                user_id    TEXT PRIMARY KEY,
                last_url   TEXT,
                last_text  TEXT,
                last_links TEXT
            )
        """)

def save_session(user_id, url, text, links):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            INSERT INTO sessions (user_id, last_url, last_text, last_links)
            VALUES (?,?,?,?)
            ON CONFLICT(user_id) DO UPDATE SET
                last_url=excluded.last_url,
                last_text=excluded.last_text,
                last_links=excluded.last_links
        """, (user_id, url, text[:5000], "|".join(links[:20])))

def get_session(user_id):
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            "SELECT last_url, last_text, last_links FROM sessions WHERE user_id=?",
            (user_id,)
        ).fetchone()
    if not row:
        return None, None, []
    links = row[2].split("|") if row[2] else []
    return row[0], row[1], links

# ── GroupMe ───────────────────────────────────────────────────────────────────

def send(text):
    chunks = [text[i:i+MAX_MSG] for i in range(0, len(text), MAX_MSG)]
    for chunk in chunks:
        req.post("https://api.groupme.com/v3/bots/post", json={
            "bot_id": BOT_ID,
            "text": chunk
        })

# ── Web helpers ───────────────────────────────────────────────────────────────

def is_url(text):
    return bool(re.match(r'https?://', text)) or bool(re.match(r'www\.', text))

def normalize_url(text):
    return "https://" + text if text.startswith("www.") else text

def fetch_page(url):
    try:
        resp = req.get(url, headers=HEADERS, timeout=12, allow_redirects=True)
        resp.raise_for_status()
        final_url = resp.url
        soup = BeautifulSoup(resp.text, "html.parser")
        for tag in soup(["script","style","nav","footer","header",
                          "aside","form","noscript","iframe","svg"]):
            tag.decompose()
        text = re.sub(r'\s+', ' ', soup.get_text(separator=" ", strip=True)).strip()
        base = f"{urlparse(final_url).scheme}://{urlparse(final_url).netloc}"
        links = []
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if href.startswith("http"):
                links.append(href)
            elif href.startswith("/"):
                links.append(base + href)
        return text[:6000], list(dict.fromkeys(links))[:20], final_url
    except Exception:
        return None, [], url

def duckduckgo_search(query):
    try:
        url = f"https://html.duckduckgo.com/html/?q={quote_plus(query)}"
        resp = req.get(url, headers=HEADERS, timeout=10)
        soup = BeautifulSoup(resp.text, "html.parser")
        results  = soup.select(".result__a")
        snippets = soup.select(".result__snippet")
        if not results:
            return None, None
        href = results[0].get("href", "")
        match = re.search(r'uddg=([^&]+)', href)
        top_url = unquote(match.group(1)) if match else (href if href.startswith("http") else None)
        snippet = snippets[0].get_text(strip=True) if snippets else ""
        return top_url, snippet
    except Exception:
        return None, None

# ── Mistral AI summarizer ─────────────────────────────────────────────────────

def summarize(page_text, url, mode="normal"):
    instruction = (
        "Give a more detailed summary. Plain text only, no markdown, no bullets. "
        "Under 800 characters. Include key facts, numbers, names, dates."
    ) if mode == "more" else (
        "Summarize this webpage in plain conversational text. No markdown, no bullets. "
        "Under 500 characters. Lead with the most important info. "
        "Write like you're texting a friend what the page says."
    )
    try:
        resp = req.post(
            "https://api.mistral.ai/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {MISTRAL_API_KEY}",
                "Content-Type": "application/json"
            },
            json={
                "model": "mistral-small-latest",
                "max_tokens": 400,
                "messages": [{
                    "role": "user",
                    "content": f"URL: {url}\n\nPAGE:\n{page_text[:4000]}\n\n{instruction}"
                }]
            },
            timeout=20
        )
        return resp.json()["choices"][0]["message"]["content"].strip()
    except Exception as e:
        return f"AI error: {e}"

# ── Handlers ──────────────────────────────────────────────────────────────────

def handle_url(user_id, url):
    send("🌐 Loading page...")
    text, links, final_url = fetch_page(url)
    if not text:
        send("❌ Couldn't load that page. Try a different search instead.")
        return
    summary = summarize(text, final_url)
    save_session(user_id, final_url, text, links)
    send(f"📄 {summary}\n\n-- Reply !more for more detail")

def handle_search(user_id, query):
    send(f"🔍 Searching: {query}...")
    top_url, snippet = duckduckgo_search(query)
    if not top_url:
        send(f"❌ No results found for '{query}'. Try different words.")
        return
    text, links, final_url = fetch_page(top_url)
    if not text:
        send(f"🔍 {query}\n\n{snippet}\n\n-- Reply !more for more detail")
        return
    summary = summarize(text, final_url)
    save_session(user_id, final_url, text, links)
    send(f"🔍 {query}\n\n{summary}\n\n-- Reply !more for more detail")

def handle_more(user_id):
    url, text, _ = get_session(user_id)
    if not text:
        send("No page loaded yet! Send a search first.")
        return
    send("📖 Getting more detail...")
    send(summarize(text, url, mode="more"))

def handle_help():
    send(
        "🌐 SMS BROWSER\n\n"
        "Type anything to search:\n"
        "  weather New York\n"
        "  latest news today\n"
        "  how to make pasta\n\n"
        "!more   - more detail on last result\n"
        "!help   - this menu\n\n"
        "Powered by Mistral AI (free!)"
    )

# ── Webhook ───────────────────────────────────────────────────────────────────

@app.route("/groupme", methods=["POST"])
def groupme_webhook():
    data = request.get_json(silent=True) or {}
    if data.get("sender_type") == "bot":
        return jsonify(ok=True)
    text    = (data.get("text") or "").strip()
    user_id = data.get("user_id", "unknown")
    if not text:
        return jsonify(ok=True)
    cmd = text.lower()
    if cmd == "!help":
        handle_help()
    elif cmd == "!more":
        handle_more(user_id)
    #elif cmd == "!links":
        #handle_links(user_id)
    elif is_url(text):
        handle_url(user_id, normalize_url(text))
    else:
        handle_search(user_id, text)
    return jsonify(ok=True)

@app.route("/")
def index():
    return "🌐 SMS Browser Bot is live!"

# Run at import time so gunicorn initializes the DB
init_db()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)

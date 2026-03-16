"""
GroupMe SMS Internet Browser — powered by Mistral AI (FREE)
Commands:
  Any text           → AI answers directly, searches web if needed
  https://...        → load a URL
  !more              → more detail
  !find something    → find specific info on page
  !links             → show numbered links
  !open N            → visit link number N
  !back              → go back to previous page
  !submit key=val    → fill & submit a form
  !help              → all commands
"""

from flask import Flask, request, jsonify
import requests as req
from bs4 import BeautifulSoup
import sqlite3, re, os
from urllib.parse import quote_plus, urlparse, unquote

app = Flask(__name__)

BOT_ID          = os.environ.get("GROUPME_BOT_ID", "ec1fe761adc29359ba5b4d55b8")
MISTRAL_API_KEY = os.environ.get("MISTRAL_API_KEY", "")
DB_PATH         = "browser.db"
MAX_MSG         = 900
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"}

# ── Database ──────────────────────────────────────────────────────────────────

def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS sessions (
            user_id TEXT PRIMARY KEY, last_url TEXT, last_text TEXT,
            last_links TEXT, prev_url TEXT, prev_text TEXT, prev_links TEXT, last_html TEXT)""")
        for col in ["prev_url","prev_text","prev_links","last_html"]:
            try: conn.execute(f"ALTER TABLE sessions ADD COLUMN {col} TEXT")
            except: pass

def save_session(user_id, url, text, links, html=""):
    s = get_session(user_id)
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""INSERT INTO sessions
            (user_id,last_url,last_text,last_links,prev_url,prev_text,prev_links,last_html)
            VALUES (?,?,?,?,?,?,?,?)
            ON CONFLICT(user_id) DO UPDATE SET
                prev_url=sessions.last_url, prev_text=sessions.last_text, prev_links=sessions.last_links,
                last_url=excluded.last_url, last_text=excluded.last_text,
                last_links=excluded.last_links, last_html=excluded.last_html""",
            (user_id, url, text[:5000], "|".join(links[:30]),
             s[0] or "", s[1] or "", "|".join(s[2]) if s[2] else "", html[:10000]))

def get_session(user_id):
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute("SELECT last_url,last_text,last_links,prev_url,prev_text,prev_links,last_html FROM sessions WHERE user_id=?",(user_id,)).fetchone()
    if not row: return None,None,[],None,None,[],"" 
    return row[0],row[1],(row[2].split("|") if row[2] else []),row[3],row[4],(row[5].split("|") if row[5] else []),(row[6] or "")

# ── GroupMe ───────────────────────────────────────────────────────────────────

def send(text):
    for chunk in [text[i:i+MAX_MSG] for i in range(0,len(text),MAX_MSG)]:
        req.post("https://api.groupme.com/v3/bots/post", json={"bot_id":BOT_ID,"text":chunk})

# ── Web ───────────────────────────────────────────────────────────────────────

def is_url(t): return bool(re.match(r'https?://',t)) or bool(re.match(r'www\.',t))
def normalize_url(t): return "https://"+t if t.startswith("www.") else t

def fetch_page(url):
    try:
        r = req.get(url, headers=HEADERS, timeout=12, allow_redirects=True)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        html = str(soup)[:10000]
        for tag in soup(["script","style","noscript","iframe","svg"]): tag.decompose()
        text = re.sub(r'\s+',' ', soup.get_text(separator=" ",strip=True)).strip()
        base = f"{urlparse(r.url).scheme}://{urlparse(r.url).netloc}"
        links = []
        for a in soup.find_all("a", href=True):
            h = a["href"].strip()
            if h.startswith("http"): links.append(h)
            elif h.startswith("//"): links.append("https:"+h)
            elif h.startswith("/"): links.append(base+h)
        seen=set(); deduped=[]
        for l in links:
            if l not in seen: seen.add(l); deduped.append(l)
        return text[:6000], deduped[:30], r.url, html
    except: return None,[],url,""

def ddg_search(query):
    """Try DuckDuckGo, fall back to a direct fetch if it fails."""
    try:
        r = req.get(f"https://html.duckduckgo.com/html/?q={quote_plus(query)}", headers=HEADERS, timeout=10)
        soup = BeautifulSoup(r.text,"html.parser")
        results = soup.select(".result__a")
        snippets = soup.select(".result__snippet")
        if results:
            href = results[0].get("href","")
            m = re.search(r'uddg=([^&]+)', href)
            top = unquote(m.group(1)) if m else (href if href.startswith("http") else None)
            snippet = snippets[0].get_text(strip=True) if snippets else ""
            if top: return top, snippet
    except: pass
    return None, None

# ── AI ────────────────────────────────────────────────────────────────────────

def ai(prompt, max_tokens=350):
    try:
        r = req.post("https://api.mistral.ai/v1/chat/completions",
            headers={"Authorization":f"Bearer {MISTRAL_API_KEY}","Content-Type":"application/json"},
            json={"model":"mistral-small-latest","max_tokens":max_tokens,"messages":[{"role":"user","content":prompt}]},
            timeout=20)
        return r.json()["choices"][0]["message"]["content"].strip()
    except Exception as e: return f"AI error: {e}"

def needs_web_search(query):
    """Decide if a query needs a live web search or can be answered by AI directly."""
    prompt = (
        f'Does this question require a current web search to answer accurately, '
        f'or can it be answered from general knowledge?\n'
        f'Question: "{query}"\n'
        f'Reply with just one word: SEARCH or ANSWER'
    )
    result = ai(prompt, max_tokens=5)
    return "SEARCH" in result.upper()

def direct_answer(query):
    """Answer a question directly with AI, short and conversational."""
    return ai(
        f"Answer this question directly and concisely. Plain text only, no markdown, no bullets. "
        f"Under 400 characters. Be direct like texting a friend.\n\nQuestion: {query}"
    )

def summarize(text, url, mode="normal"):
    ins = ("Give a detailed plain text summary, no markdown, no bullets. Under 800 chars. Key facts, numbers, names." if mode=="more"
           else "Summarize this webpage in plain text. No markdown. Under 400 chars. Most important info first. Like texting a friend.")
    return ai(f"URL: {url}\n\nPAGE:\n{text[:4000]}\n\n{ins}")

def find_in_page(text, query):
    return ai(f"From this webpage, find info about: {query}\n\nPAGE:\n{text[:4000]}\n\nDirect plain text answer, under 400 chars. If not found, say so clearly.")

# ── Handlers ──────────────────────────────────────────────────────────────────

def handle_url(user_id, url):
    send("🌐 Loading page...")
    text,links,final_url,html = fetch_page(url)
    if not text: send("❌ Couldn't load that page."); return
    save_session(user_id, final_url, text, links, html)
    send(f"📄 {summarize(text,final_url)}\n\n-- !more !find X !links !open N !back")

def handle_query(user_id, query):
    """Smart handler - answers directly if possible, searches web if needed."""
    # Very short/trivial inputs - ignore
    if len(query.strip()) <= 2:
        return

    send(f"🤔 Looking up: {query}...")

    # Ask AI if this needs a web search
    if needs_web_search(query):
        top_url, snippet = ddg_search(query)
        if top_url:
            text,links,final_url,html = fetch_page(top_url)
            if text:
                save_session(user_id, final_url, text, links, html)
                send(f"🔍 {summarize(text,final_url)}\n\n-- !more !find X !links !open N !back")
                return
        # DDG failed or page failed - fall back to AI with snippet hint
        hint = f"Search snippet hint: {snippet}\n\n" if snippet else ""
        answer = ai(f"{hint}Answer this directly in plain text, under 400 chars: {query}")
        send(answer)
    else:
        # AI can answer directly
        send(direct_answer(query))

def handle_more(user_id):
    url,text,_,_,_,_,_ = get_session(user_id)
    if not text: send("No page loaded yet!"); return
    send("📖 More detail..."); send(summarize(text,url,mode="more"))

def handle_find(user_id, query):
    url,text,_,_,_,_,_ = get_session(user_id)
    if not text: send("No page loaded yet!"); return
    send(f"🔎 Finding: {query}...")
    send(find_in_page(text, query))

def handle_links(user_id):
    _,_,links,_,_,_,_ = get_session(user_id)
    if not links: send("No links on this page."); return
    lines = ["🔗 Links (type !open N):"]
    for i,link in enumerate(links[:10],1):
        label = urlparse(link).path.strip("/").split("/")[-1] or urlparse(link).netloc
        lines.append(f"{i}. {(label or link)[:50]}")
    send("\n".join(lines))

def handle_open(user_id, n_str):
    _,_,links,_,_,_,_ = get_session(user_id)
    try:
        n = int(n_str.strip()); assert 1 <= n <= len(links)
    except: send("❌ Use !links to see links, then !open N with a valid number."); return
    handle_url(user_id, links[n-1])

def handle_back(user_id):
    _,_,_,prev_url,prev_text,prev_links,_ = get_session(user_id)
    if not prev_url: send("Nothing to go back to!"); return
    send("⬅️ Going back...")
    text,links,final_url,html = fetch_page(prev_url)
    if not text: text,links,final_url,html = prev_text,prev_links,prev_url,""
    save_session(user_id, final_url, text, links, html)
    send(f"📄 {summarize(text,final_url)}\n\n-- !more !find X !links !open N !back")

def handle_submit(user_id, form_data):
    url,_,_,_,_,_,html = get_session(user_id)
    if not html: send("❌ No page loaded."); return
    soup = BeautifulSoup(html,"html.parser")
    forms = soup.find_all("form")
    if not forms: send("❌ No forms found on this page."); return
    form = forms[0]
    action = form.get("action", url) or url
    method = form.get("method","get").lower()
    if not action.startswith("http"):
        base = f"{urlparse(url).scheme}://{urlparse(url).netloc}"
        action = base + (action if action.startswith("/") else "/"+action)
    fields = {}
    for part in form_data.split(","):
        part = part.strip()
        if "=" in part:
            k,v = part.split("=",1); fields[k.strip()] = v.strip()
    for inp in form.find_all(["input","textarea","select"]):
        name = inp.get("name")
        if name and name not in fields and inp.get("type","").lower() == "hidden":
            fields[name] = inp.get("value","")
    send(f"📤 Submitting form...")
    try:
        r = (req.post if method=="post" else req.get)(action, **{"data" if method=="post" else "params": fields}, headers=HEADERS, timeout=12, allow_redirects=True)
        soup2 = BeautifulSoup(r.text,"html.parser")
        html2 = str(soup2)[:10000]
        for tag in soup2(["script","style","noscript","iframe","svg"]): tag.decompose()
        text2 = re.sub(r'\s+',' ',soup2.get_text(separator=" ",strip=True)).strip()[:6000]
        base = f"{urlparse(r.url).scheme}://{urlparse(r.url).netloc}"
        links2 = [a["href"] if a["href"].startswith("http") else base+a["href"] for a in soup2.find_all("a",href=True) if a["href"].startswith(("/","http"))]
        save_session(user_id, r.url, text2, links2, html2)
        send(f"✅ Done!\n\n{summarize(text2,r.url)}\n\n-- !more !find X !open N !back")
    except Exception as e: send(f"❌ Submit failed: {e}")

def handle_help():
    send(
        "🌐 SMS BROWSER — Commands:\n\n"
        "Just type anything to get an answer\n"
        "  when are the oscars\n"
        "  what does finagle mean\n"
        "  lakers score tonight\n\n"
        "Load a page:\n"
        "  https://espn.com\n\n"
        "On a page:\n"
        "  !more — more detail\n"
        "  !find price — find specific info\n"
        "  !links — show page links\n"
        "  !open 3 — visit link 3\n"
        "  !back — go back\n"
        "  !submit email=x,pass=y\n\n"
        "!help — this menu\n"
        "Powered by Mistral AI (free)"
    )

# ── Webhook ───────────────────────────────────────────────────────────────────

@app.route("/groupme", methods=["POST"])
def groupme_webhook():
    data = request.get_json(silent=True) or {}
    if data.get("sender_type") == "bot": return jsonify(ok=True)
    text    = (data.get("text") or "").strip()
    user_id = data.get("user_id","unknown")
    if not text: return jsonify(ok=True)

    # Normalize command — lowercase, remove spaces after !
    lower = re.sub(r'^!\s+', '!', text.lower())

    if lower == "!help":               handle_help()
    elif lower == "!more":             handle_more(user_id)
    elif lower == "!back":             handle_back(user_id)
    elif lower == "!links":            handle_links(user_id)
    elif lower.startswith("!open "):   handle_open(user_id, text[6:].strip())
    elif lower.startswith("!find "):   handle_find(user_id, text[6:].strip())
    elif lower.startswith("!submit "): handle_submit(user_id, text[8:].strip())
    elif is_url(text):                 handle_url(user_id, normalize_url(text))
    else:                              handle_query(user_id, text)

    return jsonify(ok=True)

@app.route("/")
def index(): return "🌐 SMS Browser Bot is live!"

init_db()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT",5000)))

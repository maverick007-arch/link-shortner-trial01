import hmac
import os
import random
import re
import sqlite3
import string
import time
from urllib.parse import urlparse

from flask import Flask, redirect, render_template_string, request

app = Flask(__name__)

# STORAGE
# If DATABASE_URL is set (on Render, we set it to our free Neon Postgres address),
# links are saved in Postgres and survive redeploys and restarts.
# If it is not set (running on your own computer), we use a small SQLite file instead.
DATABASE_URL = os.environ.get("DATABASE_URL")
DB_FILE = os.environ.get("DB_FILE", "links.db")

# SETTINGS (all optional, set them as Environment Variables on Render)
# STATS_KEY        = secret password for the /stats page (stats stay off if unset)
# CREATE_PASSWORD  = password needed to MAKE a short link (anyone can still OPEN
#                    short links). If unset, anyone can make links.
# MAX_LINKS_PER_HOUR = most links the whole site will create per hour (default 30)
STATS_KEY = os.environ.get("STATS_KEY", "")
CREATE_PASSWORD = os.environ.get("CREATE_PASSWORD", "")
try:
    MAX_LINKS_PER_HOUR = int(os.environ.get("MAX_LINKS_PER_HOUR", "30"))
except ValueError:
    MAX_LINKS_PER_HOUR = 30

MAX_LINK_LENGTH = 2000  # longest long-link we accept
MAX_CODE_LENGTH = 32  # longest custom short code we accept

# Short codes that would clash with our own pages
RESERVED_CODES = {"stats"}

if DATABASE_URL:
    import psycopg2

    PLACEHOLDER = "%s"  # Postgres marks values with %s
else:
    PLACEHOLDER = "?"  # SQLite marks values with ?

_schema_ready = False  # we only need to prepare the tables once per app start


def get_db():
    # Open a connection. The first time, also make sure the tables are ready.
    global _schema_ready
    if DATABASE_URL:
        db = psycopg2.connect(DATABASE_URL)
    else:
        db = sqlite3.connect(DB_FILE)

    if not _schema_ready:
        cur = db.cursor()
        # Brand-new databases get the table with the clicks column included
        cur.execute(
            "CREATE TABLE IF NOT EXISTS links ("
            "short_code TEXT PRIMARY KEY, "
            "original_link TEXT NOT NULL, "
            "clicks INTEGER NOT NULL DEFAULT 0)"
        )
        # Databases made before the click counter existed need the column added.
        # Existing links are kept, and their click count starts at 0.
        if DATABASE_URL:
            cur.execute(
                "ALTER TABLE links ADD COLUMN IF NOT EXISTS "
                "clicks INTEGER NOT NULL DEFAULT 0"
            )
        else:
            try:
                cur.execute(
                    "ALTER TABLE links ADD COLUMN clicks INTEGER NOT NULL DEFAULT 0"
                )
            except sqlite3.OperationalError:
                pass  # column already exists, nothing to do
        # A tiny log of WHEN links were made (used for the hourly limit)
        cur.execute("CREATE TABLE IF NOT EXISTS creations (created BIGINT NOT NULL)")
        db.commit()
        _schema_ready = True
    return db


def save_link(short_code, original_link):
    # Save the pair (short_code -> original_link).
    # Returns True if saved, False if that short_code was already taken.
    db = get_db()
    cur = db.cursor()
    cur.execute(
        f"INSERT INTO links (short_code, original_link) "
        f"VALUES ({PLACEHOLDER}, {PLACEHOLDER}) ON CONFLICT DO NOTHING",
        (short_code, original_link),
    )
    saved = cur.rowcount == 1
    db.commit()
    db.close()
    return saved


def links_created_last_hour():
    # How many links the whole site has created in the last 60 minutes
    one_hour_ago = int(time.time()) - 3600
    db = get_db()
    cur = db.cursor()
    cur.execute(
        f"SELECT COUNT(*) FROM creations WHERE created > {PLACEHOLDER}",
        (one_hour_ago,),
    )
    count = cur.fetchone()[0]
    db.close()
    return count


def record_creation():
    # Note that a link was just made, and forget entries older than a day
    now = int(time.time())
    db = get_db()
    cur = db.cursor()
    cur.execute(f"INSERT INTO creations (created) VALUES ({PLACEHOLDER})", (now,))
    cur.execute(f"DELETE FROM creations WHERE created < {PLACEHOLDER}", (now - 86400,))
    db.commit()
    db.close()


def visit_link(short_code):
    # Someone visited a short link: add 1 to its click count, then return the
    # long link. Returns None if the code doesn't exist (exact match only).
    db = get_db()
    cur = db.cursor()
    cur.execute(
        f"UPDATE links SET clicks = clicks + 1 WHERE short_code = {PLACEHOLDER}",
        (short_code,),
    )
    if cur.rowcount == 0:
        db.close()
        return None
    cur.execute(
        f"SELECT original_link FROM links WHERE short_code = {PLACEHOLDER}",
        (short_code,),
    )
    original_link = cur.fetchone()[0]
    db.commit()
    db.close()
    return original_link


def get_stats():
    # The 100 most-clicked links, plus the total number of clicks
    db = get_db()
    cur = db.cursor()
    cur.execute(
        "SELECT short_code, original_link, clicks FROM links "
        "ORDER BY clicks DESC, short_code LIMIT 100"
    )
    rows = cur.fetchall()
    # Total clicks across ALL links (not just the 100 shown)
    cur.execute("SELECT COALESCE(SUM(clicks), 0) FROM links")
    total = int(cur.fetchone()[0])
    db.close()
    return rows, total


def make_random_code(length=6):
    # Random letters and digits, e.g. "a8Kd2x"
    chars = string.ascii_letters + string.digits
    return "".join(random.choice(chars) for _ in range(length))


def is_valid_link(link):
    # A very basic check that this looks like a real web address
    if len(link) > MAX_LINK_LENGTH or any(ch.isspace() for ch in link):
        return False
    try:
        return bool(urlparse(link).hostname)
    except ValueError:
        return False


def points_to_this_site(link):
    # Stop links that point back at our own site (they could loop forever)
    try:
        link_host = (urlparse(link).hostname or "").lower()
    except ValueError:
        return False
    our_host = request.host.split(":")[0].lower()
    return link_host == our_host


PAGE = """
<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>URL Shortener</title>
  <style>
    body { font-family: sans-serif; max-width: 520px; margin: 40px auto; padding: 0 16px; }
    input { width: 100%; padding: 10px; margin: 6px 0 14px; box-sizing: border-box; font-size: 16px; }
    button { padding: 10px 18px; font-size: 16px; }
    .error { color: #b00020; }
    .result { background: #eef7ee; padding: 12px; border-radius: 6px; word-break: break-all; }
  </style>
</head>
<body>
  <h1>URL Shortener</h1>
  <form method="post" action="/">
    <label>Paste a long link</label>
    <input name="original_link" placeholder="https://example.com/very/long/link">
    <label>Custom short code (optional, e.g. march)</label>
    <input name="custom_code" placeholder="leave blank for a random one">
    {% if needs_password %}
    <label>Password</label>
    <input name="password" type="password" autocomplete="current-password">
    {% endif %}
    <button type="submit">Shorten</button>
  </form>
  {% if error %}<p class="error">{{ error }}</p>{% endif %}
  {% if short_link %}
    <p>Your short link:</p>
    <p class="result"><a href="{{ short_link }}">{{ short_link }}</a></p>
  {% endif %}
</body>
</html>
"""

STATS_PAGE = """
<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="robots" content="noindex">
  <title>Link stats</title>
  <style>
    body { font-family: sans-serif; max-width: 720px; margin: 40px auto; padding: 0 16px; }
    table { border-collapse: collapse; width: 100%; }
    th, td { text-align: left; padding: 8px; border-bottom: 1px solid #ccc; vertical-align: top; }
    td.dest { word-break: break-all; }
    td.num, th.num { text-align: right; }
  </style>
</head>
<body>
  <h1>Link stats</h1>
  <p>{{ rows|length }} link(s) shown, {{ total }} total click(s).</p>
  <table>
    <tr><th>Code</th><th>Goes to</th><th class="num">Clicks</th></tr>
    {% for short_code, original_link, clicks in rows %}
    <tr>
      <td>{{ short_code }}</td>
      <td class="dest">{{ original_link }}</td>
      <td class="num">{{ clicks }}</td>
    </tr>
    {% endfor %}
  </table>
</body>
</html>
"""

NOT_FOUND_PAGE = "<h1>Link not found</h1><p><a href='/'>Make a new short link</a></p>"


def show_page(status=200, **values):
    # Show the home page (with the password box only if a password is set)
    html = render_template_string(PAGE, needs_password=bool(CREATE_PASSWORD), **values)
    return html, status


@app.route("/", methods=["GET", "POST"])
def home():
    if request.method == "GET":
        return show_page()

    # Step 0: if a password is required, check it before doing anything else
    if CREATE_PASSWORD:
        given_password = request.form.get("password", "").encode("utf-8")
        if not hmac.compare_digest(given_password, CREATE_PASSWORD.encode("utf-8")):
            return show_page(403, error="Wrong password")

    # Step 1: RECEIVE the link from the form
    original_link = request.form.get("original_link", "").strip()
    custom_code = request.form.get("custom_code", "").strip()

    # Step 2: CHECK the link
    if original_link == "":
        return show_page(error="Please paste a link")

    # Add https:// if the person forgot it, otherwise the redirect would break
    if not original_link.startswith(("http://", "https://")):
        original_link = "https://" + original_link

    if not is_valid_link(original_link):
        return show_page(error="That doesn't look like a valid link")
    if points_to_this_site(original_link):
        return show_page(error="You can't shorten a link to this site")

    if custom_code:
        if len(custom_code) > MAX_CODE_LENGTH:
            return show_page(error=f"Custom code can be at most {MAX_CODE_LENGTH} characters")
        if not re.fullmatch(r"[A-Za-z0-9_-]+", custom_code):
            return show_page(error="Custom code can only use letters, numbers, - and _")
        if custom_code.lower() in RESERVED_CODES:
            return show_page(error="That code is reserved")

    # Hourly limit for the whole site
    if links_created_last_hour() >= MAX_LINKS_PER_HOUR:
        return show_page(
            429, error="Too many links were made in the last hour. Please try again later."
        )

    # Steps 3 + 4: CREATE the short code and SAVE the pair
    if custom_code:
        short_code = custom_code
        if not save_link(short_code, original_link):
            return show_page(error="That code is already taken")
    else:
        short_code = make_random_code()
        # Very unlikely to clash, but if it does, try another random code
        while not save_link(short_code, original_link):
            short_code = make_random_code()
    record_creation()

    # Step 5: SHOW the finished short link
    short_link = request.host_url + short_code
    return show_page(short_link=short_link)


@app.route("/stats")
def stats():
    # Open it like this:  https://your-app.onrender.com/stats?key=YOUR_STATS_KEY
    # Wrong or missing key (or no STATS_KEY set) looks exactly like "not found".
    given_key = request.args.get("key", "").encode("utf-8")
    if not STATS_KEY or not hmac.compare_digest(given_key, STATS_KEY.encode("utf-8")):
        return NOT_FOUND_PAGE, 404

    rows, total = get_stats()
    return render_template_string(STATS_PAGE, rows=rows, total=total)


@app.route("/<short_code>")
def follow_short_link(short_code):
    # The browser asked for /<short_code>. Count the click and look it up.
    original_link = visit_link(short_code)

    if original_link is None:
        return NOT_FOUND_PAGE, 404

    # Found it: tell the browser to go to the saved long address
    return redirect(original_link)


if __name__ == "__main__":
    app.run(debug=True)

import os
import random
import re
import sqlite3
import string

from flask import Flask, redirect, render_template_string, request

app = Flask(__name__)

# STORAGE
# If DATABASE_URL is set (on Render, we set it to our free Neon Postgres address),
# links are saved in Postgres and survive redeploys and restarts.
# If it is not set (running on your own computer), we use a small SQLite file instead.
DATABASE_URL = os.environ.get("DATABASE_URL")
DB_FILE = os.environ.get("DB_FILE", "links.db")

if DATABASE_URL:
    import psycopg2

    PLACEHOLDER = "%s"  # Postgres marks values with %s
else:
    PLACEHOLDER = "?"  # SQLite marks values with ?


def get_db():
    # Open a connection and make sure the two-column table exists
    if DATABASE_URL:
        db = psycopg2.connect(DATABASE_URL)
    else:
        db = sqlite3.connect(DB_FILE)
    cur = db.cursor()
    cur.execute(
        "CREATE TABLE IF NOT EXISTS links ("
        "short_code TEXT PRIMARY KEY, original_link TEXT NOT NULL)"
    )
    db.commit()
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


def find_link(short_code):
    # Look up a code (exact match only). Returns the long link, or None.
    db = get_db()
    cur = db.cursor()
    cur.execute(
        f"SELECT original_link FROM links WHERE short_code = {PLACEHOLDER}",
        (short_code,),
    )
    row = cur.fetchone()
    db.close()
    return row[0] if row else None


def make_random_code(length=6):
    # Random letters and digits, e.g. "a8Kd2x"
    chars = string.ascii_letters + string.digits
    return "".join(random.choice(chars) for _ in range(length))


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

NOT_FOUND_PAGE = "<h1>Link not found</h1><p><a href='/'>Make a new short link</a></p>"


@app.route("/", methods=["GET", "POST"])
def home():
    if request.method == "GET":
        return render_template_string(PAGE)

    # Step 1: RECEIVE the link from the form
    original_link = request.form.get("original_link", "").strip()
    custom_code = request.form.get("custom_code", "").strip()

    # Step 2: CHECK that something was pasted
    if original_link == "":
        return render_template_string(PAGE, error="Please paste a link")

    # Add https:// if the person forgot it, otherwise the redirect would break
    if not original_link.startswith(("http://", "https://")):
        original_link = "https://" + original_link

    # Steps 3 + 4: CREATE the short code and SAVE the pair
    if custom_code:
        if not re.fullmatch(r"[A-Za-z0-9_-]+", custom_code):
            return render_template_string(
                PAGE, error="Custom code can only use letters, numbers, - and _"
            )
        short_code = custom_code
        if not save_link(short_code, original_link):
            return render_template_string(PAGE, error="That code is already taken")
    else:
        short_code = make_random_code()
        # Very unlikely to clash, but if it does, try another random code
        while not save_link(short_code, original_link):
            short_code = make_random_code()

    # Step 5: SHOW the finished short link
    short_link = request.host_url + short_code
    return render_template_string(PAGE, short_link=short_link)


@app.route("/<short_code>")
def follow_short_link(short_code):
    # The browser asked for /<short_code>. Look it up (exact match only).
    original_link = find_link(short_code)

    if original_link is None:
        return NOT_FOUND_PAGE, 404

    # Found it: tell the browser to go to the saved long address
    return redirect(original_link)


if __name__ == "__main__":
    app.run(debug=True)

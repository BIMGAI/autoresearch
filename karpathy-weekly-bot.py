#!/usr/bin/env python3
"""
Karpathy Weekly Bot — $0 automated X post pipeline.

Fetches RSS feeds, generates a funny weekly summary with a local LLM,
creates a social card image, and posts to X with the image attached.

Setup: see SETUP STEPS below or research-karpathy-ai.md

Requirements:
    pip install tweepy pillow requests

    No feedparser needed — uses Python's built-in xml.etree + urllib (zero deps for RSS).

Optional (for better summaries):
    curl -fsSL https://ollama.com/install.sh | sh && ollama pull llama3

Usage:
    python karpathy-weekly-bot.py              # dry run (prints post, no tweet)
    python karpathy-weekly-bot.py --post       # actually post to X
    python karpathy-weekly-bot.py --post --week 12  # override week number

Cron (every Monday 9am):
    0 9 * * 1 cd /path/to/autoresearch && python karpathy-weekly-bot.py --post

SETUP STEPS:
    1. Create X developer account    → developer.x.com (free tier, 1500 tweets/mo)
    2. Create an app                 → developer.x.com/en/portal/projects-and-apps
    3. Generate 4 keys               → Consumer Key, Consumer Secret, Access Token, Access Token Secret
    4. Copy .env.example to .env     → fill in the 4 keys
    5. pip install feedparser tweepy pillow requests
    6. python karpathy-weekly-bot.py → verify dry run works
    7. python karpathy-weekly-bot.py --post → send first tweet
    8. crontab -e → add the cron line above
"""

import argparse
import datetime
import json
import os
import sys
import textwrap
from pathlib import Path

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------

FEEDS = [
    # --- Karpathy ---
    "https://karpathy.bearblog.dev/feed/",
    "https://github.com/karpathy.atom",
    "https://github.com/karpathy/autoresearch/releases.atom",
    "https://github.com/karpathy/nanochat/releases.atom",
    "https://github.com/karpathy/llm.c/releases.atom",
    # --- Trending AI / Tech ---
    "https://hnrss.org/best?count=20",                          # Hacker News best
    "https://rsshub.app/twitter/user/OpenAI",                   # OpenAI
    "https://rsshub.app/twitter/user/AnthropicAI",              # Anthropic
    "https://blog.google/technology/ai/rss/",                   # Google AI blog
    "https://openai.com/blog/rss.xml",                          # OpenAI blog
    "https://www.anthropic.com/feed.xml",                       # Anthropic blog
    "https://simonwillison.net/atom/everything/",               # Simon Willison
    "https://lilianweng.github.io/index.xml",                   # Lilian Weng (OpenAI)
    "https://www.tldrai.com/feed.xml",                          # TLDR AI newsletter
    "https://github.com/trending.atom",                         # GitHub trending
    "https://arxiv.org/rss/cs.AI",                              # arXiv AI papers
]

OLLAMA_URL = "http://localhost:11434/api/generate"
OLLAMA_MODEL = "llama3"  # change to mistral, phi3, etc.

FUNNY_PROMPT = """You are a funny weekly AI and tech commentator on X/Twitter.
Your style: SNL Weekend Update meets tech Twitter. Deadpan, absurd analogies,
self-deprecating humor. Genuine insight under the jokes. Never cringe.

Here are this week's top AI and tech updates:
{items}

Write a single X post (under 270 chars so there's room for hashtags).
Or if there's a lot of news, write a thread (2-3 tweets, each under 270 chars,
separated by ---).

Pick the most impactful 2-3 items from the list. Prioritize:
1. Anything genuinely new or surprising
2. Big releases, papers, or product launches
3. Karpathy updates (always include if present)
4. Trending repos or viral discussions

Format options:
- "This Week in AI" (bullets with punchlines)
- "ai.diff" (git diff +/- jokes)
- "BREAKING" (deadpan fake news anchor)
- "Scoreboard" (AI labs vs. rest of us)

Be genuinely funny. Real info + humor = shareable + valuable.
End with a one-liner that lands. Week {week_num}."""

FALLBACK_TEMPLATE = """This Week in AI, Vol. {week_num}:

{bullets}

The machines are busy. Are you?
#AI #Tech #LLM"""

# ---------------------------------------------------------------------------
# STEP 1: FETCH RSS FEEDS
# ---------------------------------------------------------------------------

def fetch_weekly_items(days=7):
    """Fetch items from all feeds published in the last N days."""
    import xml.etree.ElementTree as ET
    from urllib.request import urlopen, Request
    from urllib.error import URLError

    cutoff = datetime.datetime.now() - datetime.timedelta(days=days)
    items = []

    for url in FEEDS:
        try:
            req = Request(url, headers={"User-Agent": "KarpathyWeeklyBot/1.0"})
            with urlopen(req, timeout=15) as resp:
                raw = resp.read()
            root = ET.fromstring(raw)

            # Handle both RSS and Atom feeds
            ns = {"atom": "http://www.w3.org/2005/Atom"}
            entries = root.findall(".//item")  # RSS
            if not entries:
                entries = root.findall(".//atom:entry", ns)  # Atom

            for entry in entries:
                # Title
                title_el = entry.find("title")
                if title_el is None:
                    title_el = entry.find("atom:title", ns)
                title = title_el.text.strip() if title_el is not None and title_el.text else ""

                # Link
                link_el = entry.find("link")
                if link_el is None:
                    link_el = entry.find("atom:link", ns)
                link = ""
                if link_el is not None:
                    link = link_el.text or link_el.get("href", "")

                # Date — try multiple fields
                date_str = None
                for tag in ["pubDate", "published", "updated",
                            "atom:published", "atom:updated"]:
                    el = entry.find(tag)
                    if el is None:
                        el = entry.find(tag, ns)
                    if el is not None and el.text:
                        date_str = el.text.strip()
                        break

                published = None
                if date_str:
                    for fmt in [
                        "%a, %d %b %Y %H:%M:%S %z",   # RSS pubDate
                        "%a, %d %b %Y %H:%M:%S %Z",
                        "%Y-%m-%dT%H:%M:%S%z",         # Atom ISO
                        "%Y-%m-%dT%H:%M:%SZ",
                        "%Y-%m-%dT%H:%M:%S.%f%z",
                        "%Y-%m-%d",
                    ]:
                        try:
                            published = datetime.datetime.strptime(date_str, fmt)
                            if published.tzinfo:
                                published = published.replace(tzinfo=None)
                            break
                        except ValueError:
                            continue

                if published and published > cutoff and title:
                    items.append({
                        "title": title,
                        "link": link.strip(),
                        "date": published.strftime("%b %d"),
                        "source": url.split("/")[2],
                    })
        except (URLError, ET.ParseError, Exception) as e:
            print(f"  WARN: failed to fetch {url}: {e}")

    # Deduplicate by title similarity
    seen = set()
    unique = []
    for item in items:
        key = item["title"][:60].lower()
        if key not in seen:
            seen.add(key)
            unique.append(item)

    # Sort by date descending
    unique.sort(key=lambda x: x["date"], reverse=True)
    return unique[:15]  # cap at 15 items


# ---------------------------------------------------------------------------
# STEP 2: GENERATE FUNNY POST (Ollama or fallback)
# ---------------------------------------------------------------------------

def generate_with_ollama(items, week_num):
    """Generate funny post using local Ollama. Returns None if unavailable."""
    import requests

    items_text = "\n".join(f"- {it['title']} ({it['source']})" for it in items)
    prompt = FUNNY_PROMPT.format(items=items_text, week_num=week_num)

    try:
        resp = requests.post(OLLAMA_URL, json={
            "model": OLLAMA_MODEL,
            "prompt": prompt,
            "stream": False,
        }, timeout=120)
        if resp.status_code == 200:
            return resp.json().get("response", "").strip()
    except requests.ConnectionError:
        print("  INFO: Ollama not running, using fallback template")
    except Exception as e:
        print(f"  WARN: Ollama error: {e}, using fallback")

    return None


def generate_fallback(items, week_num):
    """Simple template fallback when Ollama isn't available."""
    bullets = "\n".join(f"• {it['title'][:50]}" for it in items[:3])
    return FALLBACK_TEMPLATE.format(week_num=week_num, bullets=bullets).strip()


def generate_post(items, week_num):
    """Generate the weekly post text."""
    post = generate_with_ollama(items, week_num)
    if not post:
        post = generate_fallback(items, week_num)
    return post


# ---------------------------------------------------------------------------
# STEP 3: GENERATE SOCIAL CARD IMAGE
# ---------------------------------------------------------------------------

def create_social_card(bullets, week_label, output_path="card.png"):
    """Generate a 1200x675 branded social card."""
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        print("  WARN: pip install pillow — skipping card generation")
        return None

    W, H = 1200, 675
    img = Image.new("RGB", (W, H), color="#1a1a2e")
    draw = ImageDraw.Draw(img)

    # Try system fonts, fall back to default
    title_font = body_font = None
    for font_path in [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
    ]:
        if Path(font_path).exists():
            try:
                from PIL import ImageFont as IF
                title_font = IF.truetype(font_path, 36)
                body_font = IF.truetype(font_path.replace("Bold", ""), 24)
            except Exception:
                pass
            break

    if not title_font:
        title_font = ImageFont.load_default()
        body_font = ImageFont.load_default()

    # Header
    draw.text((60, 40), f"AI Weekly — {week_label}", fill="#e94560", font=title_font)
    draw.line([(60, 90), (W - 60, 90)], fill="#e94560", width=2)

    # Bullets
    y = 115
    for bullet in bullets[:5]:
        wrapped = textwrap.fill(f"• {bullet}", width=60)
        draw.text((60, y), wrapped, fill="#ffffff", font=body_font)
        line_count = len(wrapped.split("\n"))
        y += line_count * 30 + 10

    # Footer
    draw.text((60, H - 50), "AI Weekly Digest", fill="#888888", font=body_font)

    img.save(output_path)
    print(f"  Card saved: {output_path}")
    return output_path


# ---------------------------------------------------------------------------
# STEP 4: POST TO X
# ---------------------------------------------------------------------------

def load_x_credentials():
    """Load X API credentials from .env file or environment variables."""
    creds = {}
    env_file = Path(__file__).parent / ".env"

    # Try .env file first
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, val = line.split("=", 1)
                creds[key.strip()] = val.strip().strip('"').strip("'")

    # Environment variables override .env
    for key in ["X_CONSUMER_KEY", "X_CONSUMER_SECRET", "X_ACCESS_TOKEN", "X_ACCESS_TOKEN_SECRET"]:
        if os.environ.get(key):
            creds[key] = os.environ[key]

    required = ["X_CONSUMER_KEY", "X_CONSUMER_SECRET", "X_ACCESS_TOKEN", "X_ACCESS_TOKEN_SECRET"]
    missing = [k for k in required if k not in creds]
    if missing:
        return None, missing
    return creds, []


def post_to_x(text, image_path=None):
    """Post a tweet, optionally with an image. Returns tweet URL or None."""
    try:
        import tweepy
    except ImportError:
        print("ERROR: pip install tweepy")
        return None

    creds, missing = load_x_credentials()
    if not creds:
        print(f"ERROR: Missing X credentials: {missing}")
        print("  Create a .env file with: X_CONSUMER_KEY, X_CONSUMER_SECRET,")
        print("  X_ACCESS_TOKEN, X_ACCESS_TOKEN_SECRET")
        return None

    client = tweepy.Client(
        consumer_key=creds["X_CONSUMER_KEY"],
        consumer_secret=creds["X_CONSUMER_SECRET"],
        access_token=creds["X_ACCESS_TOKEN"],
        access_token_secret=creds["X_ACCESS_TOKEN_SECRET"],
    )

    media_ids = []
    if image_path and Path(image_path).exists():
        try:
            auth = tweepy.OAuth1UserHandler(
                consumer_key=creds["X_CONSUMER_KEY"],
                consumer_secret=creds["X_CONSUMER_SECRET"],
                access_token=creds["X_ACCESS_TOKEN"],
                access_token_secret=creds["X_ACCESS_TOKEN_SECRET"],
            )
            api = tweepy.API(auth)
            media = api.media_upload(image_path)
            media_ids = [media.media_id]
        except Exception as e:
            print(f"  WARN: Image upload failed ({e}), posting text only")

    # Handle thread (split on ---)
    tweets = [t.strip() for t in text.split("---") if t.strip()]
    reply_to = None

    for i, tweet_text in enumerate(tweets):
        kwargs = {"text": tweet_text}
        if i == 0 and media_ids:
            kwargs["media_ids"] = media_ids
        if reply_to:
            kwargs["in_reply_to_tweet_id"] = reply_to

        response = client.create_tweet(**kwargs)
        tweet_id = response.data["id"]
        if i == 0:
            reply_to = tweet_id
            tweet_url = f"https://x.com/i/status/{tweet_id}"

    return tweet_url


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

def get_week_number():
    """ISO week number."""
    return datetime.date.today().isocalendar()[1]


def main():
    parser = argparse.ArgumentParser(description="Karpathy Weekly Bot")
    parser.add_argument("--post", action="store_true", help="Actually post to X (default: dry run)")
    parser.add_argument("--week", type=int, default=None, help="Override week number")
    parser.add_argument("--days", type=int, default=7, help="Look back N days (default: 7)")
    args = parser.parse_args()

    week_num = args.week or get_week_number()
    week_label = f"Week {week_num}, {datetime.date.today().year}"

    print(f"=== Karpathy Weekly Bot — {week_label} ===\n")

    # Step 1: Fetch
    print("[1/4] Fetching RSS feeds...")
    items = fetch_weekly_items(days=args.days)
    if not items:
        print("  No items found this week. Nothing to post.")
        return
    print(f"  Found {len(items)} items")
    for it in items[:5]:
        print(f"    {it['date']} | {it['title'][:60]}")

    # Step 2: Generate funny post
    print("\n[2/4] Generating funny post...")
    post_text = generate_post(items, week_num)
    print(f"\n--- POST TEXT ---\n{post_text}\n-----------------\n")

    # Step 3: Generate social card
    print("[3/4] Generating social card...")
    card_bullets = [it["title"] for it in items[:5]]
    card_path = create_social_card(card_bullets, week_label)

    # Step 4: Post or dry run
    if args.post:
        print("[4/4] Posting to X...")
        url = post_to_x(post_text, card_path)
        if url:
            print(f"\n  Posted! {url}")
        else:
            print("\n  Failed to post. Check credentials and try again.")
    else:
        print("[4/4] DRY RUN — add --post to actually tweet")
        print("  To test: python karpathy-weekly-bot.py --post")

    print("\nDone.")


if __name__ == "__main__":
    main()

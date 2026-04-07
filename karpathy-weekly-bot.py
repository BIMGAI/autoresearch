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

FUNNY_PROMPT = """You are a sharp weekly AI commentator on X/Twitter.
Your voice: witty, opinionated, insightful. Like a tech journalist who
actually builds things. You make complex AI news accessible and interesting.

Here are this week's AI and tech updates:
{items}

Write an X thread (2-3 tweets, separated by ---).

TWEET 1 (the hook — max 280 chars):
- Open with a bold take, surprising stat, or provocative question
- Must make someone stop scrolling
- Reference the biggest story of the week

TWEET 2 (the meat — max 280 chars):
- Cover 2-3 more items with brief WHY-IT-MATTERS context
- Use "→" arrows to connect headline to implication
- Show the pattern: what do these stories mean together?

TWEET 3 (the closer — max 280 chars):
- Your personal take or prediction
- End with a question to drive replies
- Include #AI #Tech

Pick the 3-4 most impactful items. For each, add ONE sentence of
context explaining why it matters — don't just list titles.

Prioritize:
1. Things that change how people build or use AI
2. Big releases, papers, or product launches
3. Karpathy updates (always include if present)
4. Surprising or counterintuitive developments

Tone: confident, slightly irreverent, genuinely helpful. Not cringe.
Week {week_num}."""

FALLBACK_TEMPLATES = [
    """The biggest AI story you missed this week:

{main_item}

{context}

Also shipping:
{secondary_bullets}

What are you building with this? Reply below.

#AI #Tech #LLM""",

    """AI is moving fast. Here's what actually matters from this week:

{bullets_with_context}

The gap between "keeping up" and "falling behind" is now about 7 days.

What caught your eye? #AI #LLM""",

    """Week {week_num} in AI — the signal, not the noise:

{bullets_with_context}

Most people will scroll past this.
The ones building will bookmark it.

#AI #Tech""",
]

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


def shorten(title, max_len=60):
    """Shorten title at word boundary, add ... if truncated."""
    title = title.strip()
    if len(title) <= max_len:
        return title
    truncated = title[:max_len].rsplit(" ", 1)[0]
    return truncated.rstrip(",.;:") + "..."


SOURCE_CONTEXT = {
    "blog.google": "Google",
    "openai.com": "OpenAI",
    "anthropic.com": "Anthropic",
    "github.com": "GitHub",
    "hnrss.org": "Hacker News",
    "simonwillison.net": "Simon Willison",
    "lilianweng.github.io": "Lilian Weng",
    "arxiv.org": "arXiv",
    "karpathy.bearblog.dev": "Karpathy",
}


def source_label(source):
    """Get a clean label for a source domain."""
    return SOURCE_CONTEXT.get(source, source.split(".")[0].title())


def generate_fallback(items, week_num):
    """Generate a rich, detailed post from the week's items."""
    import random

    # Pick items with source diversity
    seen_sources = set()
    picked = []
    for it in items:
        src = it["source"]
        if src not in seen_sources:
            picked.append(it)
            seen_sources.add(src)
        if len(picked) == 5:
            break
    # Fill remaining slots if needed
    for it in items:
        if it not in picked and len(picked) < 5:
            picked.append(it)

    if not picked:
        return "No AI news this week. That's the news. #AI"

    main = picked[0]
    rest = picked[1:4]

    # Build rich bullets with source attribution
    bullets_with_context = "\n".join(
        f"→ {shorten(it['title'], 70)} [{source_label(it['source'])}]"
        for it in picked[:4]
    )
    secondary_bullets = "\n".join(
        f"→ {shorten(it['title'], 70)} [{source_label(it['source'])}]"
        for it in rest
    )

    template = random.choice(FALLBACK_TEMPLATES)
    return template.format(
        week_num=week_num,
        main_item=shorten(main["title"], 80),
        context=f"via {source_label(main['source'])} — {main['date']}",
        bullets="\n".join(f"• {shorten(it['title'])}" for it in picked[:3]),
        secondary_bullets=secondary_bullets,
        bullets_with_context=bullets_with_context,
    ).strip()


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
    for key in ["X_CONSUMER_KEY", "X_CONSUMER_SECRET", "X_ACCESS_TOKEN", "X_ACCESS_TOKEN_SECRET", "X_BEARER_TOKEN"]:
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
    tweet_url = None

    for i, tweet_text in enumerate(tweets):
        kwargs = {"text": tweet_text}
        if i == 0 and media_ids:
            kwargs["media_ids"] = media_ids
        if reply_to:
            kwargs["in_reply_to_tweet_id"] = reply_to

        try:
            response = client.create_tweet(**kwargs)
            tweet_id = response.data["id"]
            if i == 0:
                reply_to = tweet_id
                tweet_url = f"https://x.com/i/status/{tweet_id}"
        except Exception as e:
            print(f"  ERROR posting tweet: {e}")
            # Try with Bearer Token (OAuth 2.0 App-Only) as fallback
            if "X_BEARER_TOKEN" in creds or os.environ.get("X_BEARER_TOKEN"):
                try:
                    bearer = creds.get("X_BEARER_TOKEN") or os.environ["X_BEARER_TOKEN"]
                    client2 = tweepy.Client(bearer_token=bearer)
                    response = client2.create_tweet(**kwargs)
                    tweet_id = response.data["id"]
                    if i == 0:
                        reply_to = tweet_id
                        tweet_url = f"https://x.com/i/status/{tweet_id}"
                    print(f"  OK: Posted with Bearer Token fallback")
                except Exception as e2:
                    print(f"  ERROR Bearer Token fallback also failed: {e2}")
                    return None
            else:
                print("  TIP: Add X_BEARER_TOKEN to secrets for fallback auth")
                return None

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

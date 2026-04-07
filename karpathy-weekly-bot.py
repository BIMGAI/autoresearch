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
    # --- High-signal: pre-filtered by popularity ---
    "https://hnrss.org/best?count=15&points=100",               # HN best, 100+ points
    "https://hnrss.org/newest?q=AI+OR+LLM+OR+GPT&points=50",   # HN AI posts, 50+ points
    # --- Karpathy (always include, boosted) ---
    "https://karpathy.bearblog.dev/feed/",
    "https://github.com/karpathy.atom",
    "https://github.com/karpathy/autoresearch/releases.atom",
    # --- Major AI labs (official = big announcements) ---
    "https://blog.google/technology/ai/rss/",                    # Google AI
    "https://openai.com/blog/rss.xml",                           # OpenAI
    # --- From Karpathy's curated 92 feeds (AI/tech picks) ---
    "https://simonwillison.net/atom/everything/",                # Simon Willison
    "https://lilianweng.github.io/index.xml",                    # Lilian Weng
    "https://minimaxir.com/index.xml",                           # Max Woolf
    "https://garymarcus.substack.com/feed",                      # Gary Marcus
    "https://geohot.github.io/blog/feed.xml",                    # George Hotz
    "https://gwern.substack.com/feed",                           # Gwern
    "https://dynomight.net/feed.xml",                            # Dynomight
    "https://pluralistic.net/feed/",                             # Cory Doctorow
    "https://mitchellh.com/feed.xml",                            # Mitchell Hashimoto
    "https://lucumr.pocoo.org/feed.atom",                        # Armin Ronacher
    "https://overreacted.io/rss.xml",                            # Dan Abramov
    "https://www.dwarkeshpatel.com/feed",                        # Dwarkesh Patel
    "https://eli.thegreenplace.net/feeds/all.atom.xml",          # Eli Bendersky
    "https://berthub.eu/articles/index.xml",                     # Bert Hubert
    # --- BIM / AEC / Construction Tech ---
    "https://www.autodesk.com/blogs/aec/feed/",                  # Autodesk AEC blog
    "https://www.autodesk.com/blogs/construction/feed/",         # Autodesk Construction
    "https://aec-business.com/feed/",                            # AEC Business
    "https://www.bimcommunity.com/feed/",                        # BIM Community
    "https://bimchapters.blogspot.com/feeds/posts/default",      # BIM Chapters
    "https://hnrss.org/newest?q=BIM+OR+AEC+OR+construction+AI&points=30",  # HN BIM/AEC
    # --- Research ---
    "https://arxiv.org/rss/cs.AI",                               # arXiv AI papers
]

OLLAMA_URL = "http://localhost:11434/api/generate"
OLLAMA_MODEL = "llama3"  # change to mistral, phi3, etc.

# ---------------------------------------------------------------------------
# PROMPTS — X (TLDR: 1 top story) and LinkedIn (rich deep dive)
# ---------------------------------------------------------------------------

X_OLLAMA_PROMPT = """You are a sharp AI commentator on X/Twitter.
One story. One take. Make it count.

Here are this week's AI updates:
{items}

Pick THE single most important or surprising story.
Write ONE tweet (max 280 chars) that:
1. Names the story clearly
2. Explains WHY it matters in one sentence
3. Ends with a bold take or question
4. Includes #AI

Do NOT list multiple items. One story, one take, one tweet.
Week {week_num}."""

LINKEDIN_OLLAMA_PROMPT = """You are a thoughtful AI industry analyst on LinkedIn.
You write posts that busy professionals actually read.

Here are this week's AI updates:
{items}

Write a LinkedIn post (1,500-2,500 chars):

LINE 1 (the hook — under 130 chars, must stop the scroll):
A bold claim, surprising stat, or counterintuitive take about the #1 story.

THEN blank line, then the body:
- Lead with the #1 story: what happened, who shipped it, why it matters
- Cover 3-4 more stories with one paragraph each
- For each: what happened → why it matters → who should care
- Connect the dots: what pattern do these stories reveal together?
- Close with a forward-looking insight or question

Tone: authoritative but accessible. No jargon for jargon's sake.
Use line breaks generously — LinkedIn rewards scannable posts.
End with 3-5 hashtags on their own line.

Week {week_num}."""

# --- X fallback templates (TLDR: 1 story + link) ---
# NOTE: X counts any URL as 23 chars regardless of length.
# Link goes last — clean read, then the click.

# NOTE: X algorithm penalizes external links 50-90% reach reduction.
# Link goes in a REPLY, not the main post. Main post = pure text.
# Max 2 hashtags — more signals spam to the algorithm.

X_FALLBACK_TEMPLATE = """Not this ONE.

{main_item} [{source}]

{why_it_matters}

#AI #{weekly_keyword}"""

X_REPLY_TEMPLATE = """Source: {link}"""

# --- LinkedIn fallback templates (rich body) ---

LINKEDIN_FALLBACK_TEMPLATE = """{hook}

This week in AI — the 4 stories that actually matter:

1/ {item1_title}
{item1_source}
{item1_why}
{item1_link}

2/ {item2_title}
{item2_source}
{item2_why}
{item2_link}

3/ {item3_title}
{item3_source}
{item3_why}
{item3_link}

4/ {item4_title}
{item4_source}
{item4_why}
{item4_link}

The pattern: {pattern}

What are you seeing from your side? Drop your observations below — the best insights come from the comments.

#AI #{weekly_keyword} #Tech"""

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

                # --- SCORING SYSTEM ---
                # Principle: let the crowd decide. HN points are the only
                # reliable free signal for virality. Everything else is a
                # small tiebreaker, not an override.
                #
                # AUDIT NOTES (why this design):
                # - HN points = community-validated. Thousands of technical
                #   people voted. Trust the crowd over keyword heuristics.
                # - Comments = engagement depth. High comments = conversation
                #   starter = better post material.
                # - Recency bonus = engagement velocity matters. A 200-point
                #   post from today beats a 300-point post from 5 days ago.
                # - Source bonus is SMALL (max 15% of a decent HN score).
                #   Prevents one source from always dominating.
                # - NO keyword bonuses. Keywords are gameable and arbitrary.
                #   "breakthrough" in title != actual breakthrough.

                import re
                score = 0
                desc_el = entry.find("description")
                desc_text = desc_el.text if desc_el is not None and desc_el.text else ""
                source_domain = url.split("/")[2]

                # 1. COMMUNITY SIGNAL (primary — 80% of score weight)
                #    HN points = upvotes from technical community
                #    Comments = depth of discussion (weighted less)
                pts_match = re.search(r'Points:\s*(\d+)', desc_text)
                cmts_match = re.search(r'Comments:\s*(\d+)', desc_text)
                hn_points = int(pts_match.group(1)) if pts_match else 0
                hn_comments = int(cmts_match.group(1)) if cmts_match else 0
                score += hn_points + (hn_comments // 3)

                # 2. RECENCY BONUS (rewards engagement velocity)
                #    Today's post gets +50, yesterday -25, 2 days ago +0
                #    Prevents stale high-point posts from always winning
                if published:
                    days_ago = (datetime.datetime.now() - published).days
                    recency = max(0, 50 - (days_ago * 25))
                    score += recency

                # 3. SOURCE TRUST (small tiebreaker — max +40)
                #    Not "authority boost" — just breaks ties between
                #    similar-scored items from different sources
                source_trust = {
                    "karpathy": 40,
                    "blog.google": 30, "openai.com": 30,
                    "www.autodesk.com": 25,
                    "aec-business.com": 20, "www.bimcommunity.com": 20,
                }
                for key, trust in source_trust.items():
                    if key in url:
                        score += trust
                        break

                # Non-HN sources without points get a base score
                # so they can still appear if no HN posts this week
                if hn_points == 0 and source_domain not in ("hnrss.org",):
                    score += 30  # base visibility for blog/lab posts

                if published and published > cutoff and title:
                    items.append({
                        "title": title,
                        "link": link.strip(),
                        "date": published.strftime("%b %d"),
                        "source": source_domain,
                        "score": score,
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

    # Sort by score (virality) descending, then date
    unique.sort(key=lambda x: (-x["score"], x["date"]))

    if unique:
        print(f"  Top ranked:")
        for it in unique[:5]:
            print(f"    [{it['score']:>4}pts] {it['title'][:55]} ({it['source']})")

    return unique[:15]


# ---------------------------------------------------------------------------
# STEP 2: GENERATE FUNNY POST (Ollama or fallback)
# ---------------------------------------------------------------------------

def ollama_generate(prompt):
    """Call local Ollama. Returns response text or None."""
    import requests
    try:
        resp = requests.post(OLLAMA_URL, json={
            "model": OLLAMA_MODEL,
            "prompt": prompt,
            "stream": False,
        }, timeout=120)
        if resp.status_code == 200:
            return resp.json().get("response", "").strip()
    except requests.ConnectionError:
        print("  INFO: Ollama not running, using fallback")
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
    "github.com": "GitHub",
    "hnrss.org": "Hacker News",
    "simonwillison.net": "Simon Willison",
    "lilianweng.github.io": "Lilian Weng",
    "arxiv.org": "arXiv",
    "karpathy.bearblog.dev": "Karpathy",
    "minimaxir.com": "Max Woolf",
    "garymarcus.substack.com": "Gary Marcus",
    "geohot.github.io": "George Hotz",
    "gwern.substack.com": "Gwern",
    "dynomight.net": "Dynomight",
    "pluralistic.net": "Cory Doctorow",
    "mitchellh.com": "Mitchell Hashimoto",
    "lucumr.pocoo.org": "Armin Ronacher",
    "overreacted.io": "Dan Abramov",
    "www.dwarkeshpatel.com": "Dwarkesh Patel",
    "eli.thegreenplace.net": "Eli Bendersky",
    "berthub.eu": "Bert Hubert",
    "www.autodesk.com": "Autodesk",
    "aec-business.com": "AEC Business",
    "www.bimcommunity.com": "BIM Community",
    "bimchapters.blogspot.com": "BIM Chapters",
}

# Why-it-matters one-liners by source (used in fallback when no LLM)
WHY_CONTEXT = {
    "Google": "Google is shipping AI infrastructure others will build on for years.",
    "OpenAI": "OpenAI continues to push the frontier of what LLMs can do.",
    "Hacker News": "The builder community is paying attention — and building.",
    "Karpathy": "When Karpathy ships, the whole field takes notes.",
    "Simon Willison": "The tools layer is maturing fast.",
    "arXiv": "New research is closing the gap between theory and production.",
    "GitHub": "Open source is moving faster than most companies.",
    "Gary Marcus": "The AI skeptic the industry can't ignore.",
    "George Hotz": "Building from scratch — no frameworks, no excuses.",
    "Gwern": "The deepest research you'll read this month.",
    "Dwarkesh Patel": "The conversations shaping how we think about AI.",
    "Max Woolf": "Practical AI that actually ships.",
    "Cory Doctorow": "Tech policy meets reality.",
    "Mitchell Hashimoto": "Infrastructure that scales.",
    "Dan Abramov": "Frontend is getting an AI upgrade.",
    "Dynomight": "Data-driven takes that cut through the noise.",
    "Autodesk": "The AEC industry's AI transformation starts here.",
    "AEC Business": "Construction tech is having its moment.",
    "BIM Community": "BIM + AI is reshaping how we design and build.",
    "BIM Chapters": "Practical BIM workflows that save real hours.",
}


def source_label(source):
    """Get a clean label for a source domain."""
    return SOURCE_CONTEXT.get(source, source.split(".")[0].title())


def pick_diverse_items(items, count=5):
    """Pick items with source diversity."""
    seen_sources = set()
    picked = []
    for it in items:
        src = it["source"]
        if src not in seen_sources:
            picked.append(it)
            seen_sources.add(src)
        if len(picked) == count:
            break
    for it in items:
        if it not in picked and len(picked) < count:
            picked.append(it)
    return picked


def extract_weekly_keyword(items):
    """Extract the dominant topic keyword from this week's top items for #weeklykeyword."""
    if not items:
        return "Tech"

    # Combine top item titles
    combined = " ".join(it["title"].lower() for it in items[:5])

    # Keyword → hashtag mapping, checked in priority order
    keyword_map = [
        # BIM / AEC
        (["bim", "revit", "autodesk"], "BIM"),
        (["construction", "aec", "building"], "ConstructionTech"),
        (["digital twin"], "DigitalTwin"),
        (["architecture", "architect"], "Architecture"),
        # AI categories
        (["gpt", "chatgpt", "openai"], "GPT"),
        (["claude", "anthropic"], "Claude"),
        (["gemini", "google ai"], "Gemini"),
        (["agent", "autonomous", "agentic"], "AIAgents"),
        (["open source", "open-source"], "OpenSource"),
        (["robot", "humanoid", "embodied"], "Robotics"),
        (["vision", "image", "video", "veo", "sora"], "GenAI"),
        (["llm", "language model", "transformer"], "LLM"),
        (["funding", "raises", "valuation", "ipo"], "AIFunding"),
        (["safety", "alignment", "regulation"], "AISafety"),
        (["benchmark", "eval"], "AIBenchmarks"),
        (["code", "coding", "developer", "vibe"], "VibeCoding"),
        # Broad
        (["ai"], "AI"),
    ]

    for keywords, hashtag in keyword_map:
        for kw in keywords:
            if kw in combined:
                return hashtag

    return "Tech"


def generate_x_post(items, week_num):
    """Generate X post + reply link. Returns (post_text, reply_text)."""

    items_text = "\n".join(f"- {it['title']} ({it['source']})" for it in items)
    post = ollama_generate(X_OLLAMA_PROMPT.format(items=items_text, week_num=week_num))
    if post:
        link = items[0].get("link", "") if items else ""
        return post, f"Source: {link}" if link else ""

    # Fallback
    picked = pick_diverse_items(items, 5)
    if not picked:
        return "Quiet week in AI. Enjoy it while it lasts. #AI", ""

    main = picked[0]
    src = source_label(main["source"])
    why = WHY_CONTEXT.get(src, "This one's worth your attention.")
    link = main.get("link", "")
    weekly_keyword = extract_weekly_keyword(items)

    post_text = X_FALLBACK_TEMPLATE.format(
        main_item=shorten(main["title"], 120),
        source=src,
        why_it_matters=why,
        weekly_keyword=weekly_keyword,
    ).strip()

    reply_text = X_REPLY_TEMPLATE.format(link=link).strip() if link else ""

    return post_text, reply_text


def generate_linkedin_post(items, week_num):
    """Generate LinkedIn post: 130-char hook + rich body."""
    import random

    items_text = "\n".join(f"- {it['title']} ({it['source']})" for it in items)
    post = ollama_generate(LINKEDIN_OLLAMA_PROMPT.format(items=items_text, week_num=week_num))
    if post:
        return post

    # Fallback
    picked = pick_diverse_items(items, 5)
    if not picked:
        return "Quiet week in AI. That might be the most surprising thing of all."

    # Build the hook (under 130 chars for "see more" visibility)
    main = picked[0]
    hook = "Not this ONE. Here's what you almost scrolled past this week."

    # Build 4 item blocks
    def item_block(it, num):
        src = source_label(it["source"])
        why = WHY_CONTEXT.get(src, "Worth watching.")
        link = it.get("link", "")
        return {
            f"item{num}_title": it["title"],
            f"item{num}_source": f"via {src} — {it['date']}",
            f"item{num}_why": why,
            f"item{num}_link": f"Read: {link}" if link else "",
        }

    blocks = {}
    for i, it in enumerate(picked[:4], 1):
        blocks.update(item_block(it, i))

    # Fill missing slots if less than 4 items
    for i in range(len(picked) + 1, 5):
        blocks[f"item{i}_title"] = "—"
        blocks[f"item{i}_source"] = ""
        blocks[f"item{i}_why"] = ""
        blocks[f"item{i}_link"] = ""

    patterns = [
        "AI is moving from research demos to production infrastructure — fast.",
        "The tools are getting simpler, but what you can build with them is getting more complex.",
        "We're past the 'will AI work?' phase. We're in the 'how fast can we ship?' phase.",
        "The gap between AI haves and have-nots is widening every week.",
    ]

    weekly_keyword = extract_weekly_keyword(items)

    return LINKEDIN_FALLBACK_TEMPLATE.format(
        hook=hook,
        pattern=random.choice(patterns),
        weekly_keyword=weekly_keyword,
        **blocks,
    ).strip()


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


def post_to_linkedin(text):
    """Post to LinkedIn. Returns True on success. Requires LINKEDIN_ACCESS_TOKEN."""
    import requests as req

    token = os.environ.get("LINKEDIN_ACCESS_TOKEN")
    person_urn = os.environ.get("LINKEDIN_PERSON_URN")

    if not token or not person_urn:
        print("  SKIP LinkedIn: LINKEDIN_ACCESS_TOKEN or LINKEDIN_PERSON_URN not set")
        return False

    post_data = {
        "author": person_urn,
        "lifecycleState": "PUBLISHED",
        "specificContent": {
            "com.linkedin.ugc.ShareContent": {
                "shareCommentary": {"text": text},
                "shareMediaCategory": "NONE"
            }
        },
        "visibility": {"com.linkedin.ugc.MemberNetworkVisibility": "PUBLIC"}
    }

    try:
        resp = req.post(
            "https://api.linkedin.com/v2/ugcPosts",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json=post_data,
        )
        if resp.status_code in (200, 201):
            print("  LinkedIn: posted successfully")
            return True
        else:
            print(f"  LinkedIn ERROR: {resp.status_code} {resp.text[:200]}")
            return False
    except Exception as e:
        print(f"  LinkedIn ERROR: {e}")
        return False


def main():
    parser = argparse.ArgumentParser(description="AI Weekly Bot — X + LinkedIn")
    parser.add_argument("--post", action="store_true", help="Actually post (default: dry run)")
    parser.add_argument("--x-only", action="store_true", help="Post to X only")
    parser.add_argument("--linkedin-only", action="store_true", help="Post to LinkedIn only")
    parser.add_argument("--week", type=int, default=None, help="Override week number")
    parser.add_argument("--days", type=int, default=7, help="Look back N days (default: 7)")
    args = parser.parse_args()

    do_x = not args.linkedin_only
    do_linkedin = not args.x_only

    week_num = args.week or get_week_number()
    week_label = f"Week {week_num}, {datetime.date.today().year}"

    print(f"=== AI Weekly Bot — {week_label} ===\n")

    # Step 1: Fetch
    print("[1/5] Fetching RSS feeds...")
    items = fetch_weekly_items(days=args.days)
    if not items:
        print("  No items found this week. Nothing to post.")
        return
    print(f"  Found {len(items)} items")
    for it in items[:5]:
        print(f"    {it['date']} | {it['title'][:65]}")

    # Step 2: Generate X post (TLDR — 1 story)
    x_text = None
    x_reply = None
    if do_x:
        print("\n[2/5] Generating X post (TLDR)...")
        x_text, x_reply = generate_x_post(items, week_num)
        print(f"\n--- X POST ({len(x_text)} chars) ---\n{x_text}\n---")
        if x_reply:
            print(f"--- X REPLY (link) ---\n{x_reply}\n---\n")

    # Step 3: Generate LinkedIn post (rich body)
    li_text = None
    if do_linkedin:
        print("[3/5] Generating LinkedIn post (deep dive)...")
        li_text = generate_linkedin_post(items, week_num)
        print(f"\n--- LINKEDIN POST ({len(li_text)} chars) ---\n{li_text}\n---\n")

    # Step 4: Generate social card
    print("[4/5] Generating social card...")
    card_bullets = [it["title"] for it in items[:5]]
    card_path = create_social_card(card_bullets, week_label)

    # Step 5: Post or dry run
    if args.post:
        print("[5/5] Posting...")
        if do_x and x_text:
            print("  Posting to X (no link in main post — avoids algo penalty)...")
            # Post main text (no link = full reach)
            url = post_to_x(x_text, card_path)
            if url and x_reply:
                # Reply with source link (doesn't hurt parent post reach)
                print("  Posting reply with source link...")
                post_to_x(x_reply)
            if url:
                print(f"  X: {url}")
            else:
                print("  X: failed")

        if do_linkedin and li_text:
            print("  Posting to LinkedIn...")
            post_to_linkedin(li_text)
    else:
        print("[5/5] DRY RUN — add --post to publish")

    print("\nDone.")


if __name__ == "__main__":
    main()

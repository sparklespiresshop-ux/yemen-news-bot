#!/usr/bin/env python3
"""Yemen war news for a Telegram channel.

Reads Arabic news feeds, keeps the items about the war in Yemen, and posts each new
one to a Telegram channel. It is meant to run every 15 minutes on GitHub Actions
(.github/workflows/yemen-news.yml); state/seen.json remembers what was posted.

Modes:
  run      post new items (needs TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)
  check    test every feed and show what would be posted; posts nothing
  chat-id  list the chats the bot has seen (to find a private channel's id)

Feeds, keywords and settings are in config.json.
"""
import argparse
import calendar
import html
import http.client
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

import feedparser

ROOT = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(ROOT, "config.json")
STATE_PATH = os.path.join(ROOT, "state", "seen.json")
USER_AGENT = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/124.0 Safari/537.36")
YEMEN_TIME = timezone(timedelta(hours=3))  # Asia/Aden, no daylight saving
GOOGLE_NEWS = "https://news.google.com/rss/search"
TOKEN_FORMAT = re.compile(r"^\d+:[A-Za-z0-9_-]+$")
# Invisible direction marks that come along when text is copied from an Arabic screen.
_INVISIBLE = re.compile("[\u200b-\u200f\u202a-\u202e\u2066-\u2069\ufeff]")

DEFAULT_SETTINGS = {
    "max_age_hours": 12,           # ignore items older than this
    "max_posts_per_run": 8,        # more new items than this in one run: keep the newest
    "first_run_posts": 3,          # the very first run posts only this many (the rest are marked seen)
    "match_summary": True,         # look for the keywords in the summary too, not just the headline
    "include_summary": True,       # show a short summary under the headline
    "link_preview": False,         # let Telegram show a preview card for the link
    "similar_title_threshold": 0.6,  # headlines this similar (0-1) count as the same story
    "same_story_hours": 3,         # ... when they come within this many hours of each other
    "seconds_between_posts": 3,
    "keep_days": 7,                # how long state/seen.json remembers a posted item
    "google_news_edition": {"hl": "ar", "gl": "EG", "ceid": "EG:ar"},
}


class TelegramError(Exception):
    def __init__(self, message, code=None, unsure=False):
        super().__init__(message)
        self.code = code      # the HTTP / Telegram error code, when there is one
        self.unsure = unsure  # the request reached Telegram but no answer came back


class FeedsDown(Exception):
    """No enabled feed could be read in this run."""


# ---------------------------------------------------------------- Arabic text
_MARKS = re.compile(r"[ؐ-ًؚ-ٰٟۖ-ۭ]")  # diacritics, Quranic marks
_NOT_WORD = re.compile(r"[^\w]+")
_TAGS = re.compile(r"<[^>]+>")
# Small words left out when comparing two headlines (written as normalize() leaves them).
_STOPWORDS = set("""
في علي من الي عن مع بعد قبل خلال اثر بين ان او ما لا هذا هذه التي الذي الذين ذلك تلك كما حيث
منذ حتي عند لدي اليوم امس قد لم لن كان يوم وسط ضد نحو عبر اي
""".split())


def normalize(text):
    """Lower case; no diacritics, tatweel or punctuation; one form of alef, ta marbuta, ya.

    Alef madda (آ) is kept apart: مآرب (aims) is not مأرب (the city).
    """
    t = html.unescape(text or "")
    t = _MARKS.sub("", t).replace("ـ", "")
    t = re.sub("[أإٱ]", "ا", t)
    t = t.replace("ى", "ي").replace("ة", "ه").replace("ؤ", "و").replace("ئ", "ي")
    t = _NOT_WORD.sub(" ", t.lower())
    return " ".join(t.split())


def tokens(text):
    return normalize(text).split()


def _variants(token):
    """The word, and the word without its prefixes: و or ف, then ب ل ك and/or ال.

    للحج is ل + الحج (never لحج); لليمن is ل + اليمن.
    """
    out = []

    def add(v):
        if len(v) >= 2 and v not in out:
            out.append(v)

    add(token)
    rests = [token] + ([token[1:]] if token[:1] in ("و", "ف") else [])
    for rest in rests:
        add(rest)
        if rest.startswith("لل"):
            add("ال" + rest[2:])
            add(rest[2:])
            continue
        if rest[:1] in ("ب", "ل", "ك"):
            add(rest[1:])
            if rest[1:3] == "ال":
                add(rest[3:])
        if rest.startswith("ال"):
            add(rest[2:])
    return out


def base(token):
    """The word without its prefixes (the shortest form of three letters or more)."""
    best = token
    for v in _variants(token):
        if len(v) >= 3 and len(v) < len(best):
            best = v
    return best


def title_words(title):
    """A headline's words for comparing stories: no prefixes or small words; numbers as #n."""
    words = set()
    for t in tokens(title):
        if t.isdigit():
            words.add("#" + str(int(t)))
        elif t not in _STOPWORDS:
            words.add(base(t))
    return words


# Words that say a headline is a new event, not a rewrite: "غارات جديدة", "سفينة ثانية".
_NEW_EVENT_STEMS = ("جديد", "مجدد", "ثاني", "ثالث")
_NEW_EVENT_WORDS = {"اخري", "اخر", "مره"}


def _says_new(words):
    return any(w in _NEW_EVENT_WORDS or w.startswith(_NEW_EVENT_STEMS) for w in words)


class Matcher:
    """Finds any of a list of terms among a text's words.

    A term may be several words ("وقف إطلاق النار"). A trailing * matches any ending
    ("حوثي*" finds الحوثيين). The usual prefixes و ب ل ف ك ال may come before a
    term's first word ("بعدن" finds "عدن"), but "عدن" does not find "معدن" or "عدنان".
    """

    def __init__(self, terms):
        self.terms = []
        for term in terms:
            term = term.strip()
            words = tokens(term.rstrip("*"))
            if words:
                self.terms.append((words, term.endswith("*")))

    @staticmethod
    def _word(token, word, stem, first):
        for v in (_variants(token) if first else (token,)):
            if v == word or (stem and v.startswith(word)):
                return True
        return False

    def find(self, toks):
        """The first term found in the word list toks (as normalized words), or None."""
        for words, stem in self.terms:
            n = len(words)
            for i in range(len(toks) - n + 1):
                if all(self._word(toks[i + j], words[j], stem and j == n - 1, j == 0) for j in range(n)):
                    return " ".join(words)
        return None


class Filter:
    """War news about Yemen: a Yemen term, a war term, and no exclude term."""

    def __init__(self, config):
        self.yemen = Matcher(config["yemen_terms"])
        self.war = Matcher(config["war_terms"])
        self.exclude = Matcher(config.get("exclude_terms", []))
        # Single-word places (صنعاء, مأرب ...): two headlines about different places are
        # different stories, however alike the rest of the words are.
        self.places = {base(tokens(t)[0]) for t in config["yemen_terms"]
                       if "*" not in t and len(tokens(t)) == 1} - {"يمن", "يمني"}

    def why(self, text):
        toks = tokens(text)
        bad = self.exclude.find(toks)
        if bad:
            return False, f"excluded ({bad})"
        place = self.yemen.find(toks)
        if not place:
            return False, "not about Yemen"
        war = self.war.find(toks)
        if not war:
            return False, "not about the war"
        return True, f"{place} + {war}"

    def keeps(self, text):
        return self.why(text)[0]


def _jaccard(a, b):
    return len(a & b) / len(a | b) if a and b else 0.0


def same_story(a, b, threshold, flt):
    """Whether two headlines (as title_words) tell the same story."""
    na, nb = {w for w in a if w[:1] == "#"}, {w for w in b if w[:1] == "#"}
    a, b = a - na, b - nb
    if not a or not b:
        return False
    if na and nb and not (na & nb):
        return False  # "مقتل 3" and "مقتل 7": different events
    pa, pb = a & flt.places, b & flt.places
    if pa and pb and not (pa & pb):
        return False  # different places
    if _says_new(a) != _says_new(b):
        return False  # "غارات على صنعاء", then "غارات جديدة على صنعاء"
    small = min(len(a), len(b))
    # A rewrite often adds or drops words, so compare against the shorter headline.
    score = len(a & b) / small if small >= 4 else _jaccard(a, b)
    return score >= threshold


# ---------------------------------------------------------------- feeds
@dataclass
class Item:
    feed: str
    source: str
    title: str
    link: str
    key: str
    summary: str
    published: Optional[float]  # UTC timestamp; None when the feed gives no date


def _clean(text):
    return " ".join(html.unescape(_TAGS.sub(" ", text or "")).split())


def _key(link_or_id):
    """A stable id for an item: its id or link, without utm_ tracking parameters."""
    s = (link_or_id or "").strip()
    parts = urllib.parse.urlsplit(s)
    if parts.scheme in ("http", "https") and parts.query:
        query = [(k, v) for k, v in urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
                 if not k.lower().startswith("utm_")]
        s = urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(query)))
    return s


def _summary_adds(summary, title, source):
    """False when a summary only repeats the headline and the outlet (as Google News does)."""
    if len(summary) < 40:
        return False
    words = set(tokens(summary))
    known = set(tokens(title)) | set(tokens(source))
    return len(words - known) >= 3 and _jaccard(words, known) < 0.5


def feed_url(feed, settings):
    if feed.get("google_news"):
        ed = settings["google_news_edition"]
        return (f"{GOOGLE_NEWS}?q={urllib.parse.quote(feed['google_news'], safe='')}"
                f"&hl={ed['hl']}&gl={ed['gl']}&ceid={ed['ceid']}")
    return feed["url"]


def fetch(url, timeout=25):
    req = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml;q=0.9, */*;q=0.8",
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def parse_feed(data, feed_name, now):
    """The items of an RSS 2.0, RSS 1.0 or Atom feed."""
    parsed = feedparser.parse(data)
    items = []
    for e in parsed.entries:
        title = _clean(e.get("title"))
        link = (e.get("link") or "").strip()
        if not title or not link:
            continue
        source = ""
        src = e.get("source")
        if src and hasattr(src, "get"):
            source = _clean(src.get("title"))
        # Google News headlines end with " - <outlet>"
        if source and title.endswith(" - " + source):
            title = title[: -len(source) - 3].rstrip()
        source = source or feed_name
        when = e.get("published_parsed") or e.get("updated_parsed")
        published = float(calendar.timegm(when)) if when else None
        if published is not None and published > now + 600:  # a feed with a wrong time zone
            published = now
        summary = _clean(e.get("summary"))
        if not _summary_adds(summary, title, source):
            summary = ""  # so the outlet's name is never read as part of the story
        items.append(Item(feed_name, source, title, link, _key(e.get("id") or link), summary, published))
    return items


def collect(config, now, log=print):
    """Every item of every enabled feed. A feed that fails is reported and skipped."""
    items, tried, read = [], 0, 0
    for feed in config["feeds"]:
        if not feed.get("enabled", True):
            continue
        tried += 1
        try:
            got = parse_feed(fetch(feed_url(feed, config["settings"])), feed["name"], now)
        except Exception as e:  # one broken feed must not stop the others
            log(f"{feed['name']}: failed ({type(e).__name__}: {e})")
            continue
        read += 1
        log(f"{feed['name']}: {len(got)} items")
        items.extend(got)
    if tried and not read:
        raise FeedsDown()
    return items


# ---------------------------------------------------------------- state
def load_state(path):
    try:
        with open(path, encoding="utf-8") as f:
            state = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        state = {}
    state.setdefault("seeded", False)
    state.setdefault("posted", {})   # item key -> time it was posted (or passed over)
    state.setdefault("titles", [])   # [time, normalized headline] of recent posts
    return state


def save_state(path, state):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1, sort_keys=True)
        f.write("\n")
    os.replace(tmp, path)


def remember(state, item, now):
    state["posted"][item.key] = now
    state["titles"].append([now, normalize(item.title)])


def prune(state, now, keep_days):
    cutoff = now - keep_days * 86400
    state["posted"] = {k: t for k, t in state["posted"].items() if t >= cutoff}
    state["titles"] = [x for x in state["titles"] if x[0] >= cutoff]


# ---------------------------------------------------------------- choosing
def select(items, flt, state, settings, now):
    """The new items worth posting, oldest first, without repeats of the same story."""
    max_age = settings["max_age_hours"] * 3600
    threshold = settings["similar_title_threshold"]
    window = settings["same_story_hours"] * 3600
    # Rewrites of a story come within a few hours; after that a like headline is a new event.
    recent = [title_words(t) for ts, t in state["titles"] if now - ts <= window]
    chosen, chosen_words, keys = [], [], set()  # chosen_words: (published, words)
    dated = [i for i in items if i.published is not None]  # an undated item could be years old
    for it in sorted(dated, key=lambda i: (i.published, i.link)):
        if it.key in state["posted"] or it.key in keys or now - it.published > max_age:
            continue
        text = it.title + (" " + it.summary if settings["match_summary"] else "")
        if not flt.keeps(text):
            continue
        words = title_words(it.title)
        if any(same_story(words, w, threshold, flt) for w in recent):
            continue
        if any(abs(it.published - t) <= window and same_story(words, w, threshold, flt)
               for t, w in chosen_words):
            continue
        chosen.append(it)
        chosen_words.append((it.published, words))
        keys.add(it.key)
    return chosen


# ---------------------------------------------------------------- Telegram
def _shorten(text, limit):
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0]
    return cut + "…"


def format_message(item, settings, now):
    esc = lambda s: html.escape(s, quote=False)  # noqa: E731
    lines = [f"<b>{esc(_shorten(item.title, 300))}</b>"]
    if settings["include_summary"] and item.summary:
        lines += ["", esc(_shorten(item.summary, 280))]
    when = datetime.fromtimestamp(item.published if item.published is not None else now, YEMEN_TIME)
    today = datetime.fromtimestamp(now, YEMEN_TIME).date()
    stamp = when.strftime("%H:%M") if when.date() == today else when.strftime("%d/%m %H:%M")
    lines += [
        "",
        f"📰 {esc(_shorten(item.source, 80))} · 🕒 {stamp} بتوقيت اليمن",
        f'🔗 <a href="{html.escape(item.link, quote=True)}">اقرأ الخبر</a>',
    ]
    return "\n".join(lines)


def telegram_call(token, method, payload, attempts=3):
    """Calls the Telegram Bot API. Waits out rate limits; errors never contain the token.

    A failure to connect is retried. A request that reached Telegram but got no answer is
    retried only for reading methods: a message might have gone out, and must not go twice.
    """
    url = f"https://api.telegram.org/bot{token}/{method}"
    reads_only = method.startswith("get")
    data = json.dumps(payload).encode("utf-8")
    for attempt in range(attempts):
        last = attempt == attempts - 1
        try:
            req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as e:
            try:
                body = json.loads(e.read().decode("utf-8"))
            except Exception:
                body = {}
            if e.code == 429 and not last:
                time.sleep(min(int(body.get("parameters", {}).get("retry_after", 5)), 60))
                continue
            if e.code >= 500 and reads_only and not last:
                time.sleep(5)
                continue
            raise TelegramError(f"{e.code} {body.get('description', e.reason)}", code=e.code) from None
        except http.client.InvalidURL:
            # spaces or other text around the token
            raise TelegramError("the bot token is not valid", code=401) from None
        except urllib.error.URLError as e:  # could not connect or send: nothing went out
            if not last:
                time.sleep(5)
                continue
            raise TelegramError(f"cannot reach Telegram ({type(e.reason).__name__})") from None
        except (http.client.HTTPException, OSError) as e:  # sent, but the answer was lost
            if reads_only and not last:
                time.sleep(5)
                continue
            raise TelegramError(f"no answer from Telegram ({type(e).__name__})",
                                unsure=not reads_only) from None
        try:
            body = json.loads(raw.decode("utf-8"))
        except ValueError:
            raise TelegramError("Telegram sent an answer that could not be read") from None
        if body.get("ok"):
            return body
        raise TelegramError(str(body.get("description", "unknown error")), code=body.get("error_code"))
    raise TelegramError("no answer from Telegram")


def send(token, chat_id, text, settings):
    telegram_call(token, "sendMessage", {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "link_preview_options": {"is_disabled": not settings["link_preview"]},
    })


# Errors that mean the token or the channel is wrong: no other post can succeed either.
SETUP_PROBLEMS = ("unauthorized", "chat not found", "not a member", "not enough rights",
                  "need administrator rights", "have no rights", "bot was kicked",
                  "chat_write_forbidden", "token")


def is_setup_problem(error):
    text = str(error).lower()
    return error.code in (None, 401, 403, 404) or any(p in text for p in SETUP_PROBLEMS)


HINTS = {
    "unauthorized": "The bot token is wrong: copy it again from @BotFather into the TELEGRAM_BOT_TOKEN secret.",
    "token": "The bot token is wrong: copy it again from @BotFather into the TELEGRAM_BOT_TOKEN secret.",
    "chat not found": "TELEGRAM_CHAT_ID is wrong, or the bot is not in the channel. Use @yourchannel for a public "
                      "channel, or run the workflow in chat-id mode to find a private channel's number.",
    "not a member": "Add the bot to the channel as an administrator.",
    "bot was kicked": "The bot was removed from the channel: add it again as an administrator.",
    "not enough rights": "In the channel's administrators, give the bot the right to post messages.",
    "have no rights": "In the channel's administrators, give the bot the right to post messages.",
    "need administrator rights": "Make the bot an administrator of the channel, with the right to post messages.",
    "cannot reach": "Telegram could not be reached this time. The next run tries again.",
    "no answer": "Telegram did not answer this time. The next run carries on.",
}


def hint(error):
    text = str(error).lower()
    return next((h for k, h in HINTS.items() if k in text), "")


def chat_id_from(value):
    """The chat id to post to: @name, or a private channel's number.

    Accepts a channel link (https://t.me/name) and a private channel's post link
    (https://t.me/c/1234567890/5). A private invite link (t.me/+...) has no usable id:
    returns None.
    """
    v = _INVISIBLE.sub("", value or "").strip()
    m = re.match(r"^(?:https?://)?(?:www\.)?(?:t|telegram)\.me/(.*)$", v, re.IGNORECASE)
    if m:
        parts = [p for p in re.split(r"[/?#]", m.group(1)) if p]
        if len(parts) > 1 and parts[0] == "c" and parts[1].isdigit():
            return "-100" + parts[1]  # a private channel's post link: t.me/c/<number>/<post>
        if parts and parts[0] == "s" and len(parts) > 1:  # t.me/s/name, the web view
            parts = parts[1:]
        if not parts or parts[0].startswith("+") or parts[0].lower() == "joinchat":
            return None
        return "@" + parts[0]
    if not v or v.startswith("@") or re.fullmatch(r"-?\d+", v):
        return v
    return "@" + v


# ---------------------------------------------------------------- modes
def run(config, state, token, chat_id, now=None, log=print, save=None):
    """Posts the new items. Returns how many were posted."""
    now = now or time.time()
    settings = config["settings"]
    fresh = select(collect(config, now, log), Filter(config), state, settings, now)
    if not state["seeded"]:
        keep = settings["first_run_posts"]
        older, fresh = (fresh[:-keep], fresh[-keep:]) if keep > 0 else (fresh, [])
        for it in older:
            remember(state, it, now)
        state["seeded"] = True
        log(f"First run: {len(older) + len(fresh)} current items; posting the newest {len(fresh)}, "
            f"the others are marked as already seen.")
    elif len(fresh) > settings["max_posts_per_run"]:
        cap = settings["max_posts_per_run"]
        for it in fresh[:-cap]:
            remember(state, it, now)
        log(f"{len(fresh) - cap} older items passed over (more than {cap} new in one run).")
        fresh = fresh[-cap:]
    posted = 0
    for i, it in enumerate(fresh):
        if i:
            time.sleep(settings["seconds_between_posts"])
        try:
            send(token, chat_id, format_message(it, settings, now), settings)
        except TelegramError as e:
            if e.unsure:
                remember(state, it, now)  # it may have gone out: never send it twice
                log(f"No answer from Telegram for: {it.title} (it may have been posted)")
                raise
            if is_setup_problem(e) or e.code != 400:
                raise  # the token or channel is wrong, or Telegram is down: stop here
            # Telegram refused this one message (a bad character, say): skip it for good.
            log(f"Telegram refused this item, skipped: {it.title} ({e})")
            remember(state, it, now)
            continue
        remember(state, it, now)
        posted += 1
        log(f"posted: {it.title} ({it.source})")
        if save:
            save()
    prune(state, now, settings["keep_days"])
    return posted


def check(config, now=None, log=print):
    """Tests every feed, enabled or not, and shows what would be posted. Posts nothing."""
    now = now or time.time()
    settings = config["settings"]
    flt = Filter(config)
    hours = settings["max_age_hours"]
    log("Checking every feed (nothing is posted).")
    for feed in config["feeds"]:
        status = "on " if feed.get("enabled", True) else "off"
        url = feed_url(feed, settings)
        try:
            items = parse_feed(fetch(url), feed["name"], now)
        except Exception as e:
            log(f"[{status}] {feed['name']}: FAILED ({type(e).__name__}: {e})")
            log(f"      {url}")
            continue
        undated = [i for i in items if i.published is None]
        recent = [i for i in items if i.published is not None and now - i.published <= hours * 3600]
        text = (lambda i: i.title + (" " + i.summary if settings["match_summary"] else ""))
        kept = [i for i in recent if flt.keeps(text(i))]
        log(f"[{status}] {feed['name']}: OK, {len(items)} items, {len(recent)} from the last {hours} h, "
            f"{len(kept)} about the war in Yemen")
        if not items:
            log("      the feed answered but had no items: check its address")
        if undated:
            log(f"      {len(undated)} items have no date and are never posted")
        for i in sorted(kept, key=lambda i: -i.published)[:5]:
            log(f"      - {i.title} ({i.source})")
    log("Feeds marked [off] are not used until you set \"enabled\": true for them in config.json.")


def list_chats(token, log=print):
    body = telegram_call(token, "getUpdates", {})
    chats = {}
    for update in body.get("result", []):
        for kind in ("channel_post", "edited_channel_post", "message", "my_chat_member"):
            chat = (update.get(kind) or {}).get("chat")
            if chat:
                chats[chat["id"]] = chat
    if not chats:
        log("No chats seen yet. Make the bot an administrator of your channel, post any message "
            "in the channel, then run this again within 24 hours.")
        return
    log("Chats the bot has seen (use the number as TELEGRAM_CHAT_ID):")
    for chat in chats.values():
        name = chat.get("title") or chat.get("username") or ""
        log(f"  {chat['id']}  {chat.get('type', '')}  {name}")


def load_config(path):
    with open(path, encoding="utf-8") as f:
        config = json.load(f)
    for key in ("feeds", "yemen_terms", "war_terms"):
        if not config.get(key):
            raise SystemExit(f"config.json: '{key}' is missing or empty")
    for feed in config["feeds"]:
        if not feed.get("name") or not (feed.get("url") or feed.get("google_news")):
            raise SystemExit(f"config.json: every feed needs a name and a url or google_news query: {feed}")
    config["settings"] = {**DEFAULT_SETTINGS, **config.get("settings", {})}
    return config


def main(argv=None):
    ap = argparse.ArgumentParser(description="Yemen war news for a Telegram channel")
    ap.add_argument("--mode", choices=["run", "check", "chat-id"], default="run")
    ap.add_argument("--config", default=CONFIG_PATH)
    ap.add_argument("--state", default=STATE_PATH)
    args = ap.parse_args(argv)
    config = load_config(args.config)

    if args.mode == "check":
        check(config)
        return 0

    token = _INVISIBLE.sub("", os.environ.get("TELEGRAM_BOT_TOKEN", "")).strip()
    chat = _INVISIBLE.sub("", os.environ.get("TELEGRAM_CHAT_ID", "")).strip()
    if not token or (args.mode == "run" and not chat):
        if os.environ.get("GITHUB_EVENT_NAME") == "schedule":
            # A new copy of the repository runs on schedule before anyone has added the secrets.
            print("The bot is not set up yet: add the TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID secrets "
                  "(Settings > Secrets and variables > Actions). Nothing was done.")
            return 0
        missing = "TELEGRAM_BOT_TOKEN" if not token else "TELEGRAM_CHAT_ID"
        print(f"{missing} is not set. Add it in the repository under "
              "Settings > Secrets and variables > Actions.")
        return 1
    if not TOKEN_FORMAT.match(token):
        print("TELEGRAM_BOT_TOKEN does not look like a bot token. Put only the token in the secret, "
              "copied from @BotFather: digits, a colon, then letters and digits, with no other text.")
        return 1

    if args.mode == "chat-id":
        try:
            list_chats(token)
        except TelegramError as e:
            print(f"Telegram error: {e}\n{hint(e)}")
            return 1
        return 0

    chat_id = chat_id_from(chat)
    if not chat_id:
        print("TELEGRAM_CHAT_ID is an invite link, which the bot cannot post to. For a private channel, "
              "use the link of one of its posts (t.me/c/...), or run the workflow in chat-id mode "
              "and use the number it shows (-100...).")
        return 1

    state = load_state(args.state)
    try:
        posted = run(config, state, token, chat_id, save=lambda: save_state(args.state, state))
    except FeedsDown:
        print("No feed could be read this time (see the lines above). If this keeps happening, "
              "run the workflow in check mode.")
        return 1
    except TelegramError as e:
        save_state(args.state, state)
        print(f"Telegram error: {e}\n{hint(e)}")
        return 1
    save_state(args.state, state)
    print(f"Done: {posted} new item(s) posted.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

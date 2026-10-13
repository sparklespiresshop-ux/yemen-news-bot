"""Tests for bot.py. No network: feeds come from tests/fixtures, Telegram is faked.

Run with:  pip install -r requirements.txt pytest  &&  python -m pytest -q
"""
import io
import json
import os
import sys
import urllib.error
from datetime import datetime, timezone

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import bot  # noqa: E402

NOW = datetime(2026, 10, 12, 21, 0, tzinfo=timezone.utc).timestamp()


def fixture(name):
    with open(os.path.join(HERE, "fixtures", name), "rb") as f:
        return f.read()


def make_config(**settings):
    cfg = bot.load_config(os.path.join(ROOT, "config.json"))
    cfg["feeds"] = [
        {"name": "Google News", "url": "https://gn.test/feed", "enabled": True},
        {"name": "BBC عربي", "url": "https://bbc.test/feed", "enabled": True},
        {"name": "DW عربية", "url": "https://dw.test/feed", "enabled": True},
    ]
    cfg["settings"].update(settings)
    return cfg


@pytest.fixture
def net(monkeypatch):
    pages = {
        "https://gn.test/feed": fixture("google_news.xml"),
        "https://bbc.test/feed": fixture("bbc.xml"),
        "https://dw.test/feed": fixture("dw.rdf"),
    }
    sent = []

    def fake_fetch(url, timeout=25):
        if url not in pages:
            raise urllib.error.URLError("unreachable")
        return pages[url]

    def fake_telegram(token, method, payload, attempts=3):
        sent.append((method, payload))
        return {"ok": True, "result": []}

    monkeypatch.setattr(bot, "fetch", fake_fetch)
    monkeypatch.setattr(bot, "telegram_call", fake_telegram)
    monkeypatch.setattr(bot.time, "sleep", lambda s: None)
    return pages, sent


def seeded():
    state = bot.load_state("/nonexistent/seen.json")
    state["seeded"] = True
    return state


def titles(sent):
    return [p["text"].split("\n")[0] for m, p in sent if m == "sendMessage"]


# ---------------------------------------------------------------- Arabic text
def test_normalize_unifies_letter_forms_and_drops_diacritics():
    assert bot.normalize("مسيّرة") == bot.normalize("مسيرة")
    assert bot.normalize("مأرب") == "مارب"
    assert bot.normalize("الحديدة") == "الحديده"
    assert bot.normalize("إطلاق النار!") == "اطلاق النار"
    assert bot.normalize("الحـــوثي") == "الحوثي"


def test_matcher_allows_arabic_prefixes_but_not_other_words():
    m = bot.Matcher(["عدن", "يمني*", "أنصار الله", "حوثي*"])
    assert m.find(bot.tokens("وصل الوفد إلى عدن"))
    assert m.find(bot.tokens("انفجار بعدن"))
    assert m.find(bot.tokens("والقوات اليمنية"))
    assert m.find(bot.tokens("مقاتلو وأنصار الله"))
    assert m.find(bot.tokens("بيان للحوثيين"))
    assert not m.find(bot.tokens("أسعار المعدن"))
    assert not m.find(bot.tokens("قال عدنان"))
    assert not m.find(bot.tokens("الأنصار يفوزون"))


def test_filter_keeps_war_news_about_yemen_only():
    f = bot.Filter(make_config())
    assert f.keeps("غارات أمريكية على مواقع للحوثيين في صنعاء")
    assert f.keeps("مقتل 3 جنود في هجوم بطائرة مسيّرة على شبوة")
    assert f.keeps("الأمم المتحدة تدعو إلى تمديد الهدنة في اليمن")
    assert not f.keeps("أسعار الذهب في عدن اليوم")             # about Yemen, not the war
    assert not f.keeps("عدنان يعلن حرب الأسعار على المعادن")   # neither
    assert not f.keeps("منتخب اليمن يفوز على عمان في كأس الخليج")  # sport
    ok, why = f.why("غارات أمريكية على مواقع للحوثيين في صنعاء")
    assert ok and "+" in why


# ---------------------------------------------------------------- feeds
def test_google_news_title_suffix_is_removed_and_the_outlet_kept():
    items = bot.parse_feed(fixture("google_news.xml"), "Google News", NOW)
    first = items[0]
    assert first.title == "غارات أمريكية على مواقع للحوثيين في صنعاء"
    assert first.source == "الجزيرة نت"
    assert first.link == "https://news.google.com/rss/articles/CBMiAAA1?oc=5"
    assert first.published == datetime(2026, 10, 12, 18, 0, tzinfo=timezone.utc).timestamp()


def test_rss1_and_utm_tracking_are_handled():
    dw = bot.parse_feed(fixture("dw.rdf"), "DW عربية", NOW)
    assert len(dw) == 1 and dw[0].source == "DW عربية"
    assert dw[0].published == datetime(2026, 10, 12, 19, 45, tzinfo=timezone.utc).timestamp()
    assert "&" in dw[0].summary
    bbc = bot.parse_feed(fixture("bbc.xml"), "BBC عربي", NOW)
    assert "utm_source" not in bbc[0].key


def test_google_news_feed_address_is_built_from_the_query():
    url = bot.feed_url({"name": "x", "google_news": "اليمن when:1d"}, make_config()["settings"])
    assert url.startswith("https://news.google.com/rss/search?q=%D8%A7%D9%84%D9%8A%D9%85%D9%86%20when%3A1d")
    assert url.endswith("&hl=ar&gl=EG&ceid=EG:ar")


def test_default_config_is_complete():
    cfg = bot.load_config(os.path.join(ROOT, "config.json"))
    assert cfg["feeds"] and all(f.get("name") and (f.get("url") or f.get("google_news")) for f in cfg["feeds"])
    assert any(f.get("enabled", True) for f in cfg["feeds"])
    for key in ("yemen_terms", "war_terms"):
        assert all(bot.tokens(t.rstrip("*")) for t in cfg[key]), key
    for key in bot.DEFAULT_SETTINGS:
        assert key in cfg["settings"], key


# ---------------------------------------------------------------- posting
def test_first_run_posts_only_the_newest_few_and_remembers_the_rest(net):
    _, sent = net
    state = bot.load_state("/nonexistent/seen.json")
    posted = bot.run(make_config(), state, "TOKEN", "@chan", now=NOW, log=lambda *a: None)
    assert posted == 3 and state["seeded"]
    # oldest first, newest last; the 3-day-old item and non-war items never appear
    assert titles(sent) == [
        "<b>اشتباكات عنيفة بين القوات الحكومية والحوثيين في مأرب</b>",
        "<b>الأمم المتحدة تدعو إلى تمديد الهدنة في اليمن</b>",
        "<b>مقتل 3 جنود في هجوم بطائرة مسيّرة على شبوة</b>",
    ]
    # the older match was remembered without being posted
    assert "CBMiAAA1" in state["posted"]
    sent.clear()
    assert bot.run(make_config(), state, "TOKEN", "@chan", now=NOW + 900, log=lambda *a: None) == 0
    assert sent == []


def test_the_same_story_from_two_feeds_is_posted_once(net):
    _, sent = net
    posted = bot.run(make_config(), seeded(), "TOKEN", "@chan", now=NOW, log=lambda *a: None)
    assert posted == 4
    t = titles(sent)
    assert t.count("<b>غارات أمريكية على مواقع للحوثيين في صنعاء</b>") == 1
    assert not any("الذهب" in x or "منتخب" in x or "المناخ" in x or "تعز" in x for x in t)


def test_too_many_new_items_keeps_the_newest(net):
    _, sent = net
    state = seeded()
    posted = bot.run(make_config(max_posts_per_run=2), state, "TOKEN", "@chan", now=NOW, log=lambda *a: None)
    assert posted == 2
    assert titles(sent)[-1] == "<b>مقتل 3 جنود في هجوم بطائرة مسيّرة على شبوة</b>"
    assert "CBMiAAA1" in state["posted"]  # skipped, but remembered


def test_a_failing_feed_does_not_stop_the_others(net):
    pages, sent = net
    del pages["https://bbc.test/feed"]
    lines = []
    posted = bot.run(make_config(), seeded(), "TOKEN", "@chan", now=NOW, log=lines.append)
    assert posted == 4
    assert any("BBC" in line and "failed" in line for line in lines)


def test_message_escapes_html_and_names_the_source_and_yemen_time(net):
    item = bot.Item("BBC عربي", "BBC <عربي>", "هجوم <كبير> & قصف", "https://x.test/a?b=1&c=2", "k",
                    "ملخص طويل بما يكفي لعرضه تحت العنوان: قصفت طائرات مواقع في محافظة الحديدة غرب اليمن.", NOW)
    text = bot.format_message(item, make_config()["settings"], NOW)
    assert text.startswith("<b>هجوم &lt;كبير&gt; &amp; قصف</b>")
    assert "ملخص طويل" in text
    assert "BBC &lt;عربي&gt;" in text
    assert "00:00 بتوقيت اليمن" in text  # 21:00 UTC = midnight in Aden
    assert 'href="https://x.test/a?b=1&amp;c=2"' in text


def test_summary_that_repeats_the_title_is_left_out():
    item = bot.parse_feed(fixture("google_news.xml"), "Google News", NOW)[0]
    text = bot.format_message(item, make_config()["settings"], NOW)
    assert text.count("غارات أمريكية") == 1


def test_link_previews_follow_the_setting(net):
    _, sent = net
    bot.run(make_config(link_preview=True), seeded(), "TOKEN", "@chan", now=NOW, log=lambda *a: None)
    method, payload = sent[0]
    assert payload["chat_id"] == "@chan" and payload["parse_mode"] == "HTML"
    assert payload["link_preview_options"] == {"is_disabled": False}


def test_a_refused_post_stops_the_run_and_stays_unposted(net, monkeypatch):
    def refuse(token, method, payload, attempts=3):
        raise bot.TelegramError("Bad Request: chat not found")
    monkeypatch.setattr(bot, "telegram_call", refuse)
    state = seeded()
    with pytest.raises(bot.TelegramError):
        bot.run(make_config(), state, "TOKEN", "@chan", now=NOW, log=lambda *a: None)
    assert state["posted"] == {}


def test_state_forgets_old_entries():
    state = seeded()
    state["posted"] = {"old": NOW - 10 * 86400, "new": NOW - 3600}
    state["titles"] = [[NOW - 10 * 86400, "قديم"], [NOW - 3600, "جديد"]]
    bot.prune(state, NOW, keep_days=7)
    assert list(state["posted"]) == ["new"] and state["titles"] == [[NOW - 3600, "جديد"]]


def test_state_survives_a_round_trip(tmp_path):
    path = tmp_path / "state" / "seen.json"
    state = seeded()
    state["posted"]["k"] = NOW
    bot.save_state(str(path), state)
    again = bot.load_state(str(path))
    assert again["seeded"] and again["posted"] == {"k": NOW}


# ---------------------------------------------------------------- Telegram
class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_telegram_rate_limit_is_waited_out(monkeypatch):
    calls, waits = [], []

    def urlopen(req, timeout=30):
        calls.append(req.full_url)
        if len(calls) == 1:
            body = json.dumps({"ok": False, "description": "Too Many Requests", "parameters": {"retry_after": 7}}).encode()
            raise urllib.error.HTTPError(req.full_url, 429, "Too Many Requests", {}, io.BytesIO(body))
        return FakeResponse(json.dumps({"ok": True, "result": {}}).encode())

    monkeypatch.setattr(bot.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(bot.time, "sleep", waits.append)
    assert bot.telegram_call("SECRET", "sendMessage", {"chat_id": 1, "text": "x"})["ok"]
    assert len(calls) == 2 and waits == [7]


def test_telegram_errors_never_show_the_token(monkeypatch):
    def urlopen(req, timeout=30):
        body = json.dumps({"ok": False, "description": "Unauthorized"}).encode()
        raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {}, io.BytesIO(body))

    monkeypatch.setattr(bot.urllib.request, "urlopen", urlopen)
    with pytest.raises(bot.TelegramError) as err:
        bot.telegram_call("SECRET-TOKEN", "sendMessage", {})
    assert "SECRET-TOKEN" not in str(err.value) and "Unauthorized" in str(err.value)


def test_chat_id_mode_lists_the_channel(net, monkeypatch):
    def updates(token, method, payload, attempts=3):
        assert method == "getUpdates"
        return {"ok": True, "result": [
            {"update_id": 1, "my_chat_member": {"chat": {"id": -1001234567890, "type": "channel", "title": "أخبار اليمن"}}},
        ]}
    monkeypatch.setattr(bot, "telegram_call", updates)
    lines = []
    bot.list_chats("TOKEN", log=lines.append)
    assert any("-1001234567890" in line and "أخبار اليمن" in line for line in lines)


# ---------------------------------------------------------------- modes
def test_check_mode_reports_every_feed_and_posts_nothing(net):
    pages, sent = net
    cfg = make_config()
    cfg["feeds"].append({"name": "Broken", "url": "https://broken.test/feed", "enabled": False})
    lines = []
    bot.check(cfg, now=NOW, log=lines.append)
    out = "\n".join(lines)
    assert "Google News: OK" in out and "Broken: FAILED" in out and "[off]" in out
    assert "غارات أمريكية على مواقع للحوثيين في صنعاء" in out
    assert sent == []


def test_main_explains_missing_secrets(monkeypatch, capsys, tmp_path):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    monkeypatch.delenv("GITHUB_EVENT_NAME", raising=False)
    assert bot.main(["--mode", "run", "--state", str(tmp_path / "seen.json")]) == 1
    assert "TELEGRAM_BOT_TOKEN" in capsys.readouterr().out


def test_main_saves_state_even_when_telegram_refuses(net, monkeypatch, tmp_path):
    def refuse(token, method, payload, attempts=3):
        raise bot.TelegramError("Forbidden: bot is not a member of the channel chat")
    monkeypatch.setattr(bot, "telegram_call", refuse)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:ABC-def_GH")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "@chan")
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps(make_config(), ensure_ascii=False), encoding="utf-8")
    state_path = tmp_path / "seen.json"
    assert bot.main(["--mode", "run", "--config", str(cfg_path), "--state", str(state_path)]) == 1
    assert state_path.exists()


# ---------------------------------------------------------------- review fixes
def test_the_outlet_name_is_not_read_as_part_of_the_story():
    xml = fixture("google_news.xml").replace(
        "منتخب اليمن يفوز على عمان في كأس الخليج - العربي الجديد".encode(),
        "الجيش السوداني يعلن السيطرة على مدينة الفاشر - عدن الغد".encode()).replace(
        '<source url="https://www.alaraby.co.uk">العربي الجديد</source>'.encode(),
        '<source url="https://adengad.net">عدن الغد</source>'.encode()).replace(
        "منتخب اليمن يفوز على عمان في كأس الخليج&lt;/a&gt;".encode(),
        "الجيش السوداني يعلن السيطرة على مدينة الفاشر&lt;/a&gt;&amp;nbsp;&amp;nbsp;&lt;font color=\"#6f6f6f\"&gt;عدن الغد&lt;/font&gt;".encode())
    assert "منتخب".encode() not in xml and "&lt;font color=\"#6f6f6f\"&gt;عدن الغد".encode() in xml
    items = [i for i in bot.parse_feed(xml, "Google News", NOW) if "الفاشر" in i.title]
    assert items and items[0].summary == ""
    f = bot.Filter(make_config())
    assert not f.keeps(items[0].title + " " + items[0].summary)


def test_matcher_reads_lil_and_keeps_madd_alef_apart():
    f = bot.Filter(make_config())
    assert f.keeps("المبعوث الأممي لليمن يحذر من التصعيد العسكري")       # لليمن = ل + اليمن
    assert not f.keeps("قوات الأمن السعودية تنتشر في مكة استعدادا للحج")  # للحج = ل + الحج, not لحج
    assert not f.keeps("روسيا تستغل الحرب لتحقيق مآرب سياسية")          # مآرب (aims) is not مأرب
    assert f.keeps("الحوثيون يقصفون أحياء سكنية في تعز")
    assert f.keeps("مقتل مدنيين في غارتين على صعدة")
    assert not f.keeps("هجوم مسلح في الدار البيضاء")
    assert not f.keeps("جبهة المقاومة الوطنية تعلن هجوما على طالبان")
    assert f.keeps("الرئيس الأمريكي المنتخب يتوعد الحوثيين بضربات جديدة")
    assert not f.keeps("المنتخب اليمني يتأهل إلى نهائي كأس آسيا")


def test_rewrites_of_one_story_are_posted_once_but_other_places_are_not_merged():
    same = bot.same_story(bot.title_words("مقتل 3 جنود في هجوم بطائرة مسيّرة على شبوة"),
                          bot.title_words("مقتل ثلاثة جنود بهجوم بمسيرة حوثية في شبوة"), 0.6, bot.Filter(make_config()))
    assert same
    other_city = bot.same_story(bot.title_words("غارات أمريكية على مواقع للحوثيين في صنعاء"),
                                bot.title_words("غارات أمريكية على مواقع للحوثيين في الحديدة"), 0.6, bot.Filter(make_config()))
    assert not other_city
    other_event = bot.same_story(bot.title_words("مقتل 3 جنود في هجوم بطائرة مسيّرة على شبوة"),
                                 bot.title_words("انفجار عبوة ناسفة يستهدف دورية عسكرية في شبوة"), 0.6, bot.Filter(make_config()))
    assert not other_event


def test_a_message_telegram_rejects_is_skipped_and_the_rest_still_go_out(net, monkeypatch):
    _, sent = net
    calls = []

    def picky(token, method, payload, attempts=3):
        calls.append(payload)
        if len(calls) == 1:
            raise bot.TelegramError("Bad Request: can't parse entities", code=400)
        sent.append((method, payload))
        return {"ok": True}

    monkeypatch.setattr(bot, "telegram_call", picky)
    state = seeded()
    lines = []
    posted = bot.run(make_config(), state, "TOKEN", "@chan", now=NOW, log=lines.append)
    assert posted == 3 and len(calls) == 4
    assert "CBMiAAA1" in state["posted"]  # the rejected one is not tried again
    assert any("refused" in line for line in lines)


def test_a_chat_or_token_problem_stops_the_run():
    for err in (bot.TelegramError("Unauthorized", code=401),
                bot.TelegramError("Bad Request: chat not found", code=400),
                bot.TelegramError("Forbidden: bot is not a member of the channel chat", code=403)):
        assert bot.is_setup_problem(err), err
    assert not bot.is_setup_problem(bot.TelegramError("Bad Request: message is too long", code=400))


def test_very_long_headlines_are_shortened(net):
    item = bot.Item("x", "src", "هجوم " * 2000, "https://x.test/a", "k", "", NOW)
    text = bot.format_message(item, make_config()["settings"], NOW)
    assert len(text) < 1000


def test_items_without_a_date_are_not_posted(net):
    pages, sent = net
    pages["https://dw.test/feed"] = fixture("dw.rdf").replace(b"<dc:date>2026-10-12T19:45:00Z</dc:date>", b"")
    bot.run(make_config(), seeded(), "TOKEN", "@chan", now=NOW, log=lambda *a: None)
    assert not any("الهدنة" in p["text"] for m, p in sent)


def test_telegram_connection_failures_are_retried_then_reported(monkeypatch):
    calls = []

    def urlopen(req, timeout=30):
        calls.append(1)
        raise urllib.error.URLError(TimeoutError("timed out"))  # could not connect

    monkeypatch.setattr(bot.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(bot.time, "sleep", lambda s: None)
    with pytest.raises(bot.TelegramError) as err:
        bot.telegram_call("SECRET", "sendMessage", {})
    assert len(calls) == 3 and "SECRET" not in str(err.value) and not err.value.unsure


def test_a_message_that_may_have_gone_out_is_never_sent_twice(monkeypatch):
    calls = []

    def urlopen(req, timeout=30):
        calls.append(req.full_url)
        raise TimeoutError("timed out")  # the request was sent, the answer never came

    monkeypatch.setattr(bot.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(bot.time, "sleep", lambda s: None)
    with pytest.raises(bot.TelegramError) as err:
        bot.telegram_call("SECRET", "sendMessage", {})
    assert len(calls) == 1 and err.value.unsure
    calls.clear()
    with pytest.raises(bot.TelegramError):
        bot.telegram_call("SECRET", "getUpdates", {})
    assert len(calls) == 3  # reading is safe to repeat


def test_a_run_remembers_a_message_that_may_have_gone_out(net, monkeypatch):
    monkeypatch.setattr(bot, "telegram_call", lambda *a, **k: (_ for _ in ()).throw(
        bot.TelegramError("no answer from Telegram (TimeoutError)", unsure=True)))
    state = seeded()
    with pytest.raises(bot.TelegramError):
        bot.run(make_config(), state, "TOKEN", "@chan", now=NOW, log=lambda *a: None)
    assert list(state["posted"]) == ["CBMiAAA1"]  # the first item, and only that one


def test_a_malformed_token_is_reported_without_showing_it(monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "Use this token 123:ABC to access the HTTP API")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "@chan")
    assert bot.main(["--mode", "run", "--state", str(tmp_path / "s.json")]) == 1
    out = capsys.readouterr().out
    assert "123:ABC" not in out and "token" in out.lower()


def test_chat_id_accepts_a_channel_link():
    assert bot.chat_id_from("https://t.me/yemen_news") == "@yemen_news"
    assert bot.chat_id_from("t.me/yemen_news") == "@yemen_news"
    assert bot.chat_id_from("yemen_news") == "@yemen_news"
    assert bot.chat_id_from("@yemen_news") == "@yemen_news"
    assert bot.chat_id_from("-1001234567890") == "-1001234567890"
    assert bot.chat_id_from("https://t.me/c/1234567890/55") == "-1001234567890"
    assert bot.chat_id_from("https://t.me/+AbCdEf123") is None
    assert bot.chat_id_from("\u200f@yemen_news\u200e") == "@yemen_news"  # copied from an Arabic screen


def test_a_run_where_every_feed_fails_is_reported_as_failed(net, monkeypatch, tmp_path, capsys):
    pages, sent = net
    pages.clear()
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:ABC-def_GH")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "@chan")
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps(make_config(), ensure_ascii=False), encoding="utf-8")
    assert bot.main(["--mode", "run", "--config", str(cfg_path), "--state", str(tmp_path / "s.json")]) == 1
    assert "no feed could be read" in capsys.readouterr().out.lower()


def test_scheduled_runs_wait_quietly_until_the_secrets_are_added(monkeypatch, capsys, tmp_path):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    monkeypatch.setenv("GITHUB_EVENT_NAME", "schedule")
    assert bot.main(["--mode", "run", "--state", str(tmp_path / "s.json")]) == 0
    assert "not set up yet" in capsys.readouterr().out


# ---------------------------------------------------------------- second review
def test_follow_up_events_are_not_taken_for_rewrites():
    f = bot.Filter(make_config())
    same = lambda x, y: bot.same_story(bot.title_words(x), bot.title_words(y), 0.6, f)  # noqa: E731
    assert not same("غارات أمريكية على صنعاء", "غارات أمريكية جديدة على صنعاء")
    assert not same("الحوثيون يعلنون استهداف سفينة في البحر الأحمر",
                    "الحوثيون يعلنون استهداف سفينة ثانية في البحر الأحمر")
    assert not same("مقتل 2 من الجنود في هجوم حوثي على مأرب", "مقتل 5 من الجنود في هجوم حوثي على مأرب")
    assert same("غارات أمريكية جديدة على صنعاء", "غارات أمريكية جديدة تستهدف صنعاء")


def test_a_like_headline_hours_later_is_a_new_event(net):
    _, sent = net
    state = seeded()
    # the Shabwa drone attack was posted 4 hours ago (outside same_story_hours), Marib 1 hour ago
    state["titles"] = [[NOW - 4 * 3600, bot.normalize("مقتل 3 جنود في هجوم بطائرة مسيّرة على شبوة")],
                       [NOW - 3600, bot.normalize("اشتباكات عنيفة بين القوات الحكومية والحوثيين في مأرب")]]
    bot.run(make_config(), state, "TOKEN", "@chan", now=NOW, log=lambda *a: None)
    t = titles(sent)
    assert "<b>مقتل 3 جنود في هجوم بطائرة مسيّرة على شبوة</b>" in t
    assert "<b>اشتباكات عنيفة بين القوات الحكومية والحوثيين في مأرب</b>" not in t


def test_more_look_alikes_are_left_out():
    f = bot.Filter(make_config())
    for text in ("مقتل شخصين في هجوم بالأسلحة البيضاء في ليون",
                 "جنود أوكرانيون يرفعون الراية البيضاء وقوات روسية تتقدم",
                 "الشرطة تعتقل المتهم الضالع في الهجوم",
                 "عاصفة مدارية ضربت سقطرى",
                 "رحيل الفنان اليمني بعد مسيرة فنية حافلة",
                 "أهلي صنعاء يحسم معركة الصدارة في الدوري اليمني",
                 "منتخب الناشئين اليمني يخوض معركة التأهل"):
        assert not f.keeps(text), text
    for text in ("الحوثيون يطلقون مسيرتين باتجاه مأرب",
                 "التحالف ينفذ ضربتين على مواقع في الحديدة",
                 "قصف حوثي على مواقع القوات الحكومية في الضالع"):
        assert f.keeps(text), text

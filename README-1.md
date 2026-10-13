# Yemen war news → Telegram channel

A small bot that checks Arabic news feeds every 15 minutes, keeps the stories about the war in
Yemen, and posts each new one to your Telegram channel: the headline, a short summary when the
feed has one, the outlet, the time in Yemen, and a link to the full article. It can also forward
the war posts of public Telegram channels, with their photos and videos. It runs on GitHub
Actions, so nothing has to stay switched on at home.

```
اشتباكات عنيفة بين القوات الحكومية والحوثيين في مأرب

📰 العربية · 🕒 22:30 بتوقيت اليمن
🔗 اقرأ الخبر
```

## Setup (about 15 minutes)

### 1. Create the bot

1. In Telegram, open **@BotFather** and send `/newbot`.
2. Give it a name (for example *أخبار اليمن*) and a username ending in `bot`.
3. BotFather replies with a **token** like `123456789:AAH...`. Keep it private: anyone with it
   can post as your bot.

### 2. Create the channel and add the bot

1. In Telegram: **New Channel**. Public (it gets an `@name`) or private, as you like.
2. Open the channel, tap its name → **Edit** (on a phone, the pencil; on desktop, **Manage
   Channel**) → **Administrators** → **Add Admin** → search for your bot's username.
3. Leave **Post Messages** switched on and save.

### 3. Create the repository and add the secrets

1. Sign in at github.com → **New repository**. A name like `yemen-news-bot`.
   **Public** is recommended: GitHub Actions is free without limit for public repositories, and
   your token stays secret either way (secrets are never shown, even in a public repository).
   For a private repository, see *Cost and limits* below.
2. In the new repository: **Settings → Secrets and variables → Actions → New repository
   secret**. Add:

| Name | Value |
|---|---|
| `TELEGRAM_BOT_TOKEN` | the token from BotFather, and nothing else |
| `TELEGRAM_CHAT_ID` | **public channel:** `@yourchannel` (a link like `https://t.me/yourchannel` also works). **Private channel:** see step 5 |

### 4. Upload the files

1. In the repository: **Add file → Upload files** (on an empty repository, the link *uploading
   an existing file*). Unzip the download and drag **everything inside the folder** into the
   page, including the `.github` folder, then **Commit changes**.
   - On a Mac, Finder hides folders whose name starts with a dot: press **Cmd + Shift + .** to
     show `.github`.
2. Open the **Actions** tab. If GitHub asks, click to enable workflows. You should see
   **Yemen news** on the left.
   - If it is not there, the `.github` folder did not upload: **Add file → Create new file**,
     type the name `.github/workflows/yemen-news.yml`, paste in that file's contents, and commit.

Until both secrets are in place, the scheduled runs do nothing and say *The bot is not set up
yet*, so it is fine to upload first and add the secrets later.

### 5. Private channel only: tell the bot which channel

A private channel has no `@name`. Either of these works:

- **The link of a post (easiest).** Post any message in the channel, then right-click it (on a
  phone: press and hold) → **Copy Post Link**. It looks like `https://t.me/c/1234567890/2`.
  Put that link in a `TELEGRAM_CHAT_ID` secret. You can delete the message afterwards.
- **The channel's number.** With the bot already an admin, post any message in the channel.
  Within 24 hours (Telegram keeps the record that long): **Actions → Yemen news → Run workflow →
  chat-id → Run workflow**. Open the run and the **Run the bot** step: it lists the chats the
  bot has seen, such as `-1001234567890  channel  أخبار اليمن`. Put that number (with the minus
  sign) in a `TELEGRAM_CHAT_ID` secret.

An invite link (`t.me/+...`) does not work as the chat id.

### 6. First run

1. **Actions → Yemen news → Run workflow** → choose **check** → **Run workflow**. Open the run
   and the **Run the bot** step: every feed is listed as `OK` or `FAILED`, with the headlines
   it would post. Nothing is posted.
2. Run it again with **run**. The 3 newest stories appear in your channel; the older ones are
   marked as seen, so the channel does not start with a flood.
3. From now on it runs by itself every 15 minutes.

## Changing what it posts

Everything is in **`config.json`**. You can edit it on GitHub: open the file, click the pencil,
change it, **Commit changes**. The next run uses it.

### Feeds

Each feed has a `name`, `enabled` (`true` or `false`), and a `url`, a `google_news` search or
a `telegram` channel (see *Forwarding from Telegram channels* below):

```json
{ "name": "Google News: الحوثيون", "google_news": "(الحوثي OR الحوثيين OR \"أنصار الله\") when:1d", "enabled": true }
```

- Google News searches: `OR` between words (in capitals), `"..."` for an exact phrase,
  `when:1d` for the last day. They cover hundreds of Arabic outlets.
- Direct feeds: any RSS or Atom address.
- France 24 and DW are included but switched off. Run **check** to see whether they work from
  GitHub, then set `"enabled": true`.
- Run **check** after any change to feeds.

### Forwarding from Telegram channels

A public channel can be added like a feed, by its name (a `t.me` link also works):

```json
{ "name": "قناة المسيرة", "telegram": "almasirah2", "enabled": true }
```

On each run the bot reads the channel's public web page (`t.me/s/almasirah2`, its latest
posts) and **forwards** each new post whose text is about the war in Yemen (the same keywords as
the news). A forwarded post shows "Forwarded from …" with the original photos and videos; an
album is forwarded as one album.

- Posts without text (a photo or video alone) are skipped: there is nothing to check.
- Posts older than `max_age_hours` are skipped, and at most `max_posts_per_run` are forwarded per
  channel per run.
- **The first time a channel is added, nothing is forwarded:** the bot notes the latest post and
  starts from the next one, so your channel is not flooded with old posts.
- If Telegram will not let the bot forward from that channel, the bot posts the first lines of the
  text, the channel's name and a link to the original post instead; Telegram shows that link with
  its preview and media. The run's log says "posting a link instead" when that happens.
- Only public channels that allow forwarding work. Run **check**: the channel shows `OK` with the
  posts it would forward.
- A channel run by one side of the war gives that side's version of events. Adding channels from
  other sides keeps your channel balanced.

### Keywords

A story is posted when its headline (or summary) has **a Yemen term** and **a war term**, and no
**exclude term**. When several outlets carry the same story, it is posted once; headlines that
say it is a new event (جديدة، ثانية، مجددا) or give a different number or place are kept apart.

- `yemen_terms`: places and parties (اليمن، صنعاء، عدن، الحوثي، المجلس الانتقالي ...).
- `war_terms`: (غارة، قصف، اشتباكات، هدنة، مفاوضات ...).
- `exclude_terms`: things to drop: sport (المنتخب اليمني، الدوري اليمني، كأس ...) and phrases
  that contain a Yemen or war word but mean something else (الدار البيضاء، الأسلحة البيضاء،
  مسيرة فنية ...).

Write them as normal Arabic; spelling variants are handled (أ إ → ا, ة → ه, ى → ي, diacritics
ignored; آ stays apart, so `مأرب` the city is not confused with `مآرب`). The usual prefixes are
allowed (`عدن` also finds `بعدن` and `وعدن`, `اليمن` finds `لليمن`), but not other words (`عدن`
does not find `معدن` or `عدنان`, `لحج` does not find `للحج`). End a term with `*` to allow any
ending (`حوثي*` finds `الحوثيين`, `حوثيون`). A term may be several words (`وقف إطلاق النار`).

### Settings

| Setting | Default | Meaning |
|---|---|---|
| `max_age_hours` | 12 | ignore stories older than this |
| `max_posts_per_run` | 8 | if more are new at once, post the newest 8 and skip the rest |
| `first_run_posts` | 3 | the very first run posts only this many |
| `match_summary` | true | also look for keywords in the summary; `false` = headline only (stricter) |
| `include_summary` | true | show a short summary under the headline |
| `link_preview` | false | let Telegram show a preview card for each link (for Google News items the card shows Google News, not the outlet) |
| `similar_title_threshold` | 0.6 | how alike two headlines must be (0 to 1) to count as the same story; lower = fewer repeats |
| `same_story_hours` | 3 | look for repeats among stories this many hours apart; a like headline later than that is a new event (a new wave of raids, say) |
| `keep_days` | 7 | how long it remembers what it posted |

### Too many posts, or too few

Expect somewhere between a few and a few dozen posts a day, depending on the news.

- **Too many:** set `match_summary` to `false`, switch off the *المدن اليمنية* Google News
  search, or lower `similar_title_threshold` to `0.5` so more rewrites of a story count as one.
- **Too few:** run **check** to see which feeds answer and what they find; add a war term or a
  feed.

### How often

The `cron` line in `.github/workflows/yemen-news.yml`: `"7,22,37,52 * * * *"` means minutes 7,
22, 37 and 52 of every hour. Every 30 minutes would be `"7,37 * * * *"`. GitHub allows at most
every 5 minutes.

### Pause or stop

**Actions → Yemen news → … → Disable workflow.** Enable it again the same way.

## Cost and limits

- **Public repository:** free.
- **Private repository:** GitHub Free includes 2,000 Actions minutes a month for private
  repositories. Every 15 minutes is about 2,900 runs a month, which is more than that, so use
  every 30 minutes (`"7,37 * * * *"`).
- GitHub can delay scheduled runs by some minutes when it is busy, and occasionally skip one.
- GitHub switches off scheduled workflows in a public repository after 60 days without
  activity in the repository. If that happens, the Actions tab shows a button to switch it back
  on.
- The bot posts what the outlets publish, including claims from either side that are not yet
  confirmed. It posts headlines and links, not whole articles.
- News sites sometimes change their feed addresses. If a feed starts to show `FAILED` in
  **check**, replace or switch it off.

## When something goes wrong

A failed run shows a red cross in the Actions tab (GitHub can email you about it). Open the run
and the **Run the bot** step:

| Message | What to do |
|---|---|
| `The bot is not set up yet` | add the secrets (step 3); not an error |
| `TELEGRAM_BOT_TOKEN is not set` / `does not look like a bot token` | put only the token from BotFather in the secret |
| `401 Unauthorized` | the token is wrong; copy it again from BotFather |
| `chat not found` | `TELEGRAM_CHAT_ID` is wrong, or the bot is not in the channel |
| `not enough rights` / `need administrator rights` | make the bot an admin with *Post Messages* |
| `TELEGRAM_CHAT_ID is an invite link` | use a post link or the channel's number instead (step 5) |
| `<feed>: failed (...)` | that feed could not be read this time; the others still work |
| `<channel>: first look; forwarding starts after post N` | a newly added channel: normal, forwarding starts with its next post |
| `Telegram would not forward post N ...; posting a link instead` | Telegram does not let the bot forward from that channel; links are posted instead |
| `No feed could be read this time` | usually a passing network problem; if it repeats, run **check** |
| `Telegram refused this item, skipped` | that one message was rejected; the others still go out |
| `No answer from Telegram` | Telegram was slow; that item may or may not have gone out, and is not sent again; the next run carries on |
| nothing posted for a long time | run **check** to see which feeds answer and what they find |

The bot remembers what it posted in `state/seen.json`, which it saves back to the repository
after each run. Deleting that file makes it start again like the first run: it posts the 3
newest stories, which may repeat recent posts.

## Files

| File | |
|---|---|
| `bot.py` | the bot |
| `config.json` | feeds, keywords, settings |
| `.github/workflows/yemen-news.yml` | the schedule |
| `requirements.txt` | the one library it needs (feedparser) |
| `state/seen.json` | created by the bot: what was already posted |
| `tests/` | tests with sample feeds: `pip install -r requirements.txt pytest`, then `python -m pytest -q` |

You can also run it on your own computer: `pip install -r requirements.txt`, then
`python bot.py --mode check` (no token needed).

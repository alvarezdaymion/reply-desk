# Setting up Reply Desk (for a coding agent)

You are helping the owner of a small business set up Reply Desk for their cold-email replies. Work through the steps in order, ask before each choice that is theirs to make, and run the check at the end of each step. Never write credentials into `reply-desk.toml`, into the repo or into chat; they go in `.env` (git-ignored, read automatically from the folder that holds the config) or in the environment of the service. Leave `[sending] mode = "dry-run"` until step 7.

## 1. Run the demo

```bash
uv run reply-desk demo
```

Walk the owner through the output: replies sorted into interested, question, not now and unsubscribe; the one-page site for the interested lead; a card for each reply; a send confirmed on its second readback; a send locked because it never showed up; a crash mid-send recovered without a second email; follow-ups planned from what people wrote. Done when the summary shows "Repeats: 0".

## 2. Write the config

Run `uv run reply-desk init` to create `reply-desk.toml`, then ask:

1. **Business name and signature.** The signature ends every draft.
2. **Booking link** (optional). Drafts and pages show it when it is set.
3. **The page.** What an interested lead should see: a headline, one or two short paragraphs, a few bullet points. Placeholders `{first_name}`, `{company}` and `{business}` work in each.
4. **Canned answers.** The questions leads ask most (price, process, location, timing) and the owner's answer to each, as `[[answers]]` with keywords and complete plain text, without placeholders. A question with no match gets a draft that must be edited before it can be sent.
5. **Draft wording.** Show them `[drafts]`. Keep each placeholder on the line it belongs to: a line whose placeholder is empty is left out.
6. **Follow-ups.** Default gaps are 2, 4 and 7 days; `gaps_days = []` turns them off, cancels existing scheduled follow-ups and closes their pending approval cards. Set `send_hour` and `timezone` (UTC by default, or an IANA name such as `"America/Chicago"` to follow daylight-saving changes). An empty time zone is rejected.
7. **Campaigns.** Watch all Instantly campaigns, or only the ones listed in `[inbox] campaign_ids`.

Done when `uv run reply-desk check` prints "Config OK" (it also lists which credentials are still missing).

Use an empty state directory for a new installation. The application initializes an empty database as schema 3 and rejects every earlier schema, including versions 1 and 2, as well as newer schemas. Unsupported state is left unchanged; saved bodies and ledgers are not migrated automatically.

## 3. Connect Instantly

The owner creates an API v2 key in Instantly's settings with the emails scopes for reading and sending (`emails:read`, `emails:create`), plus `leads:read` for names. Block list access is needed only if they turn on `[inbox] blocklist_unsubscribes`. Put it in `.env` as `INSTANTLY_API_KEY`.

## 4. Connect Telegram

1. The owner opens a chat with @BotFather, sends `/newbot`, and puts the token in `.env` as `TELEGRAM_BOT_TOKEN`.
2. They send any message to the new bot from the phone that will approve replies.
3. Run `uv run reply-desk check --online`. With the token set and no chat ID, it lists the chats that messaged the bot. Put the owner's ID in `.env` as `TELEGRAM_CHAT_ID`.
4. If someone else shares that chat, list the Telegram user IDs allowed to tap in `[telegram] allowed_user_ids`.

Done when `uv run reply-desk check --online` shows every credential as set and both "works" lines.

## 5. Host the pages

Each interested lead gets `site/<slug>/index.html`, and its URL is `<base_url>/<slug>/`. Ask where the pages should live:

- **A folder their web host already serves.** Set `publisher = "command"` with a copy command, for example `["rsync", "-a", "{site_dir}/", "deploy@host:/srv/pages/"]`, and `base_url` to that folder's public URL.
- **A static host with a CLI** (Netlify, Cloudflare Pages, GitHub Pages and the like): use its deploy command, with `{site_dir}` where the folder goes.
- **No pages.** Set `[pages] enabled = false`. Interested replies then get a draft without a page link.

Done when `uv run reply-desk check --page` publishes a test page and prints "HTTP 200 and the page marker matched". It leaves one noindex test page on the host.

The desk saves each lead page's URL and marker before running a deploy command, and never repeats an uncertain command automatically. If a deploy fails or is interrupted, later ticks only check the saved URL. Check the deployment by hand if it never becomes live.

## 6. Run in dry-run

Start `uv run reply-desk run --loop` in a terminal and have the owner reply to one of their own campaign emails from a personal address (or wait for a real reply). Check together:

- the card arrives in Telegram with a sensible label, reason and draft;
- Edit, then replying to the bot's message, changes the draft; wait for the updated card before tapping its new Send button. Drafts over 2,500 characters and unresolved named placeholders are refused;
- after Send, the card says "Dry run: every check passed and nothing was sent."

Then install the service from `examples/scheduling/` (systemd on Linux, launchd on macOS, or cron for one tick a minute), replacing `/path/to/reply-desk` and `/path/to/uv` (`which uv`). Done when `uv run reply-desk status` shows the replies and cards you expect.

Incomplete reply bodies wait for retrieval before any draft is made. A failed or interrupted Telegram card post is locked and never reposted automatically, since Telegram may have accepted it. `status` lists these cards; check Telegram and answer the lead by hand. API rate limits stop the tick immediately and save a cooldown across restarts; later ticks make no API calls until the delay expires. Other loop errors use exponential backoff.

Cooldowns honor valid `Retry-After` response headers from both the HTTP connectors and the model SDK, in seconds or as an HTTP date. If the body and header specify different delays, the longer valid delay applies.

## 7. Go live, only when the owner asks

Set `[sending] mode = "live"` and restart the service. Explain before they tap Send for real:

- each reply and each follow-up step is sent at most once; a second tap does nothing;
- after a send, the card says "Sent once and confirmed" once a readback matches;
- a send the desk can't confirm says LOCKED. Nothing is retried. Stop the service (or pause its scheduler) first, since `run --loop` holds the state-directory lock. Check the sent folder in Instantly and run `uv run reply-desk resolve <id> --sent` or `--not-sent`, or `uv run reply-desk resolve --recheck` to read every locked send back once more, then restart the service. If it did not go out and still needs an answer, reply by hand.
- follow-up dates retain the ledger's original confirmation or resolution time across restarts. A date already past becomes a card for approval. Skipped follow-ups remain stopped even if the process was interrupted while recording the skip.

Do not change `src/reply_desk/sender.py` or the checks in `desk.py` to get a send through. A locked send is the system working as designed.

## 8. Optional: model drafts

If the owner wants drafts that read the whole reply: `uv sync --extra llm`, put `ANTHROPIC_API_KEY` in `.env`, set `[model] enabled = true` and write `[business] facts` (the only facts the model may state). It costs one model call per new reply and one per due follow-up; tell them that is the only metered cost. Model drafts go to the same cards and pass the same checks, and the rules take over whenever an answer looks wrong.

Rule decisions and follow-up drafts are saved before model calls, and SDK retries are disabled. If the process stops during a call, recovery uses the rules instead of repeating the paid request.

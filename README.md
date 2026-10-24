# Reply Desk

Reply Desk answers replies to cold email so none get lost and none get sent twice. It reads replies from every sending inbox in Instantly, sorts each one (interested, question, not now, unsubscribe), drafts an answer and sends it to your phone as a Telegram card with three buttons: Send, Edit, Skip. For an interested business it first builds a one-page site made for them and checks the page is live before the reply can go out. After a reply is sent, the next follow-up is timed to what the person wrote ("this weekend" means Monday) and comes back as a card. Every send goes through a ledger that saves the email before the API call, reads the sent message back at 3, 10 and 20 seconds and never retries: a send it can't confirm is locked until you check it.

Project page: [daymionalvarez.com/portfolio/reply-desk](https://daymionalvarez.com/portfolio/reply-desk)

## Quickstart

Needs Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/alvarezdaymion/reply-desk.git
cd reply-desk
uv run reply-desk demo        # a synthetic inbox, no keys, no network
```

To use your own inboxes, run `uv run reply-desk init`, edit `reply-desk.toml`, put credentials in `.env`, then `uv run reply-desk check --online` and `uv run reply-desk run --loop`. It starts in dry-run mode, where Send runs every check and emails nobody. Or hand the repo to your coding agent and say "set up Reply Desk for my business": [SETUP.md](SETUP.md) is written for it.

## What the demo shows

The demo runs the real desk, ledger, classifier and page builder against a fake inbox, a fake Telegram (cards print in the terminal), a page host that serves the local site folder, and a simulated clock, so the 3, 10 and 20 second waits take no time. State goes to `reply-desk-demo/state/reply-desk.db`.

1. Six replies arrive across three sending inboxes: one interested, two questions, two "not now", one unsubscribe.
2. The interested business gets a one-page site in `reply-desk-demo/site/`. Its card is posted only after a GET of the page returns 200 with that page's marker.
3. The unsubscribe gets no card. The address is suppressed, so the desk never sends to it.
4. Send on the interested card. Instantly accepts it, the readback at 3 s doesn't find it yet, the one at 10 s matches, and the send is confirmed. A second tap on the same card sends nothing.
5. Send on a question. Instantly accepts it, but the message never shows up. After the readback at 20 s the send is locked, not retried.
6. A question with no canned answer can't be sent until someone taps Edit and writes the answer.
7. The process dies right after Instantly accepts a send, before the message ID is saved. On restart the desk finds that exact message in the thread and confirms it. Telegram delivers the unacknowledged tap again, and it sends nothing.
8. Two days later a lead who went quiet gets a follow-up card. The one who said "this weekend" is planned for Monday; the one who said "after the holidays", for 4 January.
9. The summary counts the provider's send calls: one per approved card, zero repeats.

## How it runs

`reply-desk run` is one tick. `reply-desk run --loop` keeps ticking and picks up taps within seconds. Each tick:

1. **Recover.** Anything a stopped process left in flight is settled first, without sending (see below).
2. **Poll.** New replies are read from Instantly and stored once each, keyed by Instantly's message ID. Each poll reads up to three pages and saves its continuation and original time window, so a backlog resumes across ticks and restarts. Auto-replies are recorded and get no card. Truncated or missing bodies wait for a complete read before drafting; retrieval is retried on later ticks.
3. **Sort and draft.** Plain rules label each reply (or the optional model does, and its answer is checked). An interested reply gets its page built and published, and its card waits until the page is live. Unsubscribes are suppressed. Every other reply becomes a card with a draft.
4. **Taps.** Send, Edit and Skip arrive by long polling, so there is no public webhook to expose. Edit asks you to reply to a message with the new text.
5. **Follow-ups.** A follow-up that is due becomes a card, unless the person has written since.

A step that fails is reported and the rest of the tick still runs, except for rate limits. A rate limit stops the tick immediately and saves a cooldown; later ticks make no API calls until it expires, including after a restart. In loop mode, errors trigger exponential backoff from one to 60 seconds; a longer provider retry delay is honored. A throttled email call is locked before deferring, and accepted message IDs are retained for later readback. Uncertain external writes are never retried.

When you tap Send, the desk checks that the draft has nothing left to fill in, that the address hasn't unsubscribed, that a linked page still answers 200 with its marker, and that the person hasn't written again since the card was made (if they have, the card is closed as stale). In dry-run mode, the default, the checks run and nothing is sent.

Thread checks read every page and block sending if any page or required message metadata cannot be read. Drafts are limited to 2,500 characters, the size shown on a card. Each Send button identifies that draft; edits require a successful card update and a new tap on the updated button. Telegram messages are checked for length after decoding their markup and are never sliced.

Card posts are claimed before contacting Telegram. If the post fails, returns no message ID or is interrupted, its outcome is locked and it is never posted again automatically. `reply-desk status` lists these cards; check Telegram and answer the lead by hand. Unposted cards whose delivery was never attempted can be posted during recovery. Before a deploy command, the page URL and marker are saved; recovery checks that URL without repeating the command. If the page never appears, check the deployment and answer by hand.

Edit prompts are claimed by callback ID before posting, so a redelivered tap cannot repeat an uncertain prompt. If no prompt arrives, tap Edit again to request a new one. Optional model calls have SDK retries disabled; saved rule decisions and drafts provide the fallback after an interruption.

## Never the same email twice

Email APIs sometimes accept a send and then fail to confirm it: the request times out, the connection drops, or a 5xx comes back after the message was queued. A retry then emails the person a second time. Reply Desk never retries a send. `src/reply_desk/sender.py` is the only code that calls Instantly's send endpoint, and it works in four steps:

1. **Intent first.** The exact email (reply target, sender, recipient, subject, body) is written to the SQLite ledger before the API call, under a key that is unique per reply and per follow-up step. The ledger also refuses the same text to the same address under any key. Both rules are UNIQUE constraints, so they hold across restarts and processes.
2. **Save the ID.** The message ID Instantly returns is saved the moment it comes back.
3. **Read it back.** That message is fetched by its ID at 3, 10 and 20 seconds after acceptance. It counts as sent only if sender, recipient, subject and body all match.
4. **Otherwise lock.** An error or timeout on the send call, a response with no ID, or no matching readback by 20 seconds leaves the send locked. The card says so, and `reply-desk status` lists it with what to check.

| Ledger state | Meaning |
|---|---|
| `intent` | saved; the provider is being called |
| `accepted` | the provider returned a message ID |
| `confirmed` | a readback matched |
| `locked` | the outcome is unclear; waiting for a person |
| `resolved_sent`, `resolved_not_sent` | a person checked |

No state leads back to a send. After a restart, an `accepted` send is read back by its ID. An `intent` with no ID means the process stopped during the send call: the desk looks for the exact message in the thread on the same 3, 10, 20 second schedule, confirms it if it is there and locks it if it is not. Taps are claimed with a conditional update, so a double tap, or a tap Telegram delivers again after a restart, finds the card already handled.

To settle a locked send, stop the running service (or pause its scheduler) to release the state-directory lock. Check the sent folder in Instantly and run `reply-desk resolve <id> --sent` or `--not-sent`, or `reply-desk resolve --recheck` to read every locked send back once more, then restart the service. None of these commands sends anything; if the email did not go out and still needs an answer, reply by hand. Recovery reconciles a settled ledger row with its card and restores any missing follow-up using the ledger's original confirmation or human-resolution timestamp, even if the process stopped before settling the card. A recovered due date may already be past; it still becomes an approval card.

`tests/test_sender.py` covers a send accepted but not yet visible, confirmed on the second readback, never confirmed, a timeout on the send call, a restart mid-send with and without the message in the thread, a restart after acceptance and the same-text refusal. `tests/test_desk.py` covers double taps, a tap redelivered after a restart and a crash mid-send, end to end.

## Follow-ups timed to what they wrote

After a confirmed send, the next follow-up is planned from the person's own words, read relative to when they wrote them. The rule: the first business day after the time they named has passed, at `send_hour`.

| They wrote | Follow-up goes out |
|---|---|
| "this weekend", "over the weekend" | the Monday after |
| "tomorrow" | the first business day after tomorrow |
| a weekday, such as "Friday" or "by Thursday" | the first business day after that day |
| "end of the week", "later this week" | the Monday after this week |
| "next week" | the Monday after next week |
| "next month" | the first business day after next month |
| "after the holidays", "in the new year" | the first business day after 1 January |
| "in two weeks", "in 10 days", "in a couple of months" | that day (a weekend moves to Monday) |

When several phrases match, the latest wins. A date more than 90 days out is ignored as a misread. With no phrase, follow-ups use `gaps_days` (2, 4 and 7 days by default). Setting `gaps_days = []` also cancels existing scheduled follow-ups and closes their pending cards before taps are processed. A plain "no thanks" gets a polite close and no follow-ups. Each follow-up is a card like any other and is dropped if the person writes first. The time zone defaults to UTC; set an IANA name to preserve the chosen local hour across daylight-saving changes. Empty time zones are rejected. The parser is `src/reply_desk/timing.py`, tested against a fixed clock in `tests/test_timing.py`.

Skipping a follow-up ends its sequence. Recovery completes interrupted transitions for skipped, stale, closed and dry-run cards without creating another follow-up or sending anything.

## Connectors

| System | Status | What it does |
|---|---|---|
| Instantly API v2 | Real connector | Lists replies, reads a thread, looks up a lead's name and company, sends a reply in the thread, reads a sent message back, adds unsubscribes to the block list (optional, off by default) |
| Telegram Bot API | Real connector | Cards with inline buttons, taps by long polling, edit replies, notices |
| Page publisher | Real | `local` writes `site/<slug>/index.html` for any static host to serve; `command` also runs your deploy command (rsync, a static host's CLI). The live check is a real HTTP GET |
| Anthropic API | Optional (`llm` extra) | Labels and drafts, checked before use. The rules take over on any problem |
| Demo | Synthetic | All of the above, for `reply-desk demo` |

There is no CRM sync and no webhook intake: replies are polled (every 60 seconds with `run --loop`; Instantly's list endpoint allows 20 requests a minute) and taps come by long polling. To support another email platform, write a client with the same methods as `connectors/instantly.py` and test it with a fake transport (see `tests/test_connectors.py`).

## Safety rules

- **Dry-run is the default.** In `[sending] mode = "dry-run"` the provider's send method is wrapped so it can't be called at all.
- **Nothing is sent without a tap**, and every tap passes the checks above first. Follow-ups are cards too.
- **One send per reply and per follow-up step, never a retry.** Unclear sends are locked for a person.
- **Unsubscribes are final.** The rules decide them before the model is asked, and a suppressed address can't be sent to.
- **The model only drafts.** Its labels and drafts are checked (no links of its own, only the tokens for the page and booking links; no unknown placeholders; a signature) and a bad answer falls back to the rules.
- **Pages are noindex** and named `<company>-<hash>`; lead data on them is HTML-escaped.
- **Secrets stay in the environment.** Errors name the API call, never the URL or headers, so tokens can't reach logs or cards.

## Configuration

Everything lives in `reply-desk.toml` (start from `uv run reply-desk init`; defaults are in `src/reply_desk/config.py`):

- `[business]`: name, signature, booking link, and the facts the optional model may use.
- `[sending]`: `mode` (`dry-run` or `live`) and the readback schedule.
- `[inbox]`: campaigns to watch, first-poll lookback, poll interval, block-list option.
- `[telegram]`: who may tap the buttons.
- `[pages]`: base URL, publisher, and the page's headline, paragraphs and bullet points.
- `[followups]`: default gaps, send hour, time zone, how far out a named date may be.
- `[[answers]]`: plain-text canned answers for common questions, matched by keyword; placeholders are rejected.
- `[drafts]`: every draft template. A line whose placeholder is empty is left out.

Credentials come from environment variables or a `.env` file next to the config (see `.env.example`), never from this file.

## Layout

```
src/reply_desk/
  cli.py          demo, init, check, run, status, resolve
  config.py       reply-desk.toml schema and defaults
  desk.py         one tick, and the Send, Edit and Skip handlers
  sender.py       the send ledger: intent, readback, lock (the only send path)
  store.py        SQLite state; cards, sends and follow-ups change only from an expected state
  classify.py     the rules that sort replies
  drafts.py       draft templates and the checks before a send
  timing.py       follow-up dates from what people wrote
  pages.py        one-page site: render, publish, live check
  llm.py          optional model labels and drafts
  cards.py        card text
  status.py       what `status` prints
  demo.py         the synthetic inbox
  connectors/     instantly.py, telegram.py
  templates/      page.html, reply-desk.example.toml
tests/            fake inbox, fake Telegram, fake transports; no test calls a real API
examples/scheduling/   systemd, launchd and cron examples
```

## Limitations

- One email platform (Instantly) and one approval channel (Telegram).
- The rules classifier matches phrases. It is predictable, not clever; the optional model reads intent better.
- Business days are Monday to Friday. Public holidays are not modelled.
- A send blocks the tick for up to 20 seconds while it is read back.
- One `run` process per state folder (a lock file enforces it on macOS and Linux).
- Only the current state schema is supported. Empty databases are initialized; older, newer and unversioned populated databases are rejected without automatic migration.

## Development

```bash
uv run pytest
uv run ruff check
```

Runtime code uses only the Python standard library. The `llm` extra adds the Anthropic SDK.

## License

MIT. See [LICENSE](LICENSE).

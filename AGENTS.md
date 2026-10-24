# AGENTS.md

Reply Desk reads replies to cold email from Instantly, sorts and drafts them, sends Telegram approval cards, builds a one-page site for interested leads, and sends each approved email at most once through a ledger that reads it back and locks anything unclear.

If you were asked to set Reply Desk up for someone, follow [SETUP.md](SETUP.md). The rules below are for changing the code.

## Layout

- `src/reply_desk/sender.py`: `SafeSender`, the only code that calls a provider's `send_reply`.
- `src/reply_desk/store.py`: SQLite schema and state changes. Every card, send and follow-up state change is a conditional UPDATE.
- `src/reply_desk/desk.py`: one tick (recover, poll, triage, taps, follow-ups) and the Send, Edit and Skip handlers.
- `src/reply_desk/classify.py`, `drafts.py`, `timing.py`: rules, templates and follow-up dates. Pure functions.
- `src/reply_desk/messages.py`: shared auto-reply flag parser for ingestion, storage and freshness checks.
- `src/reply_desk/pages.py`: local page preparation, publishers and the live check.
- `src/reply_desk/llm.py`: optional model labels and drafts, checked before use.
- `src/reply_desk/connectors/`: Instantly and Telegram clients over `http.py`.
- `src/reply_desk/demo.py`: fakes for the demo. `cli.py`: commands. `config.py`: schema and defaults, mirrored by `templates/reply-desk.example.toml`.

## Rules

- **One send path.** Only `SafeSender.send` may call `send_reply`. Do not add another caller, a retry, or a "resend" command.
- **Intent before the call, ID right after it.** The ledger row is written before the provider is called, and the message ID is saved before anything else happens. Any error, timeout or missing ID locks the send.
- **Locked means a person decides.** Nothing moves a send out of `locked` except a matching readback (`recheck`) or `resolve`, and neither sends.
- **Recovery only reads.** `recover` may read a message by ID or search a thread. It never sends.
- **State changes are conditional.** Change rows only through the `Store` methods, which update `... WHERE state IN (expected)`, so double taps, restarts and second processes can't move a row twice. Keep the UNIQUE constraints on `sends.intent_key` and `(recipient, body_hash)`.
- **Dry-run stays the default**, and in dry-run the provider is wrapped in `NoSend`.
- **Checks before every send:** nothing left to fill in, not suppressed, linked page live (200 and its marker), no newer message from the person. Do not skip one to get a send through.
- **Complete reads before approval.** Save each fetched batch before optional lead lookup. Persist poll pagination and resume failed or unfinished polls before handling approvals, even between scheduled polls. Exhaust thread pagination and hold incomplete reply bodies for retrieval before drafting. Missing or malformed thread metadata blocks sending.
- **Freshness covers the recipient.** Cancel older scheduled and carded follow-ups and close their pending cards before body retrieval or model work. Check stored replies and all provider threads for the recipient before sending or creating a follow-up. Save each poll window before its first fetch or item is processed. Preserve fractional timestamps in fixed microsecond UTC form for storage, cursors and ordering.
- **One supported state schema.** Initialize empty databases as schema 3. Reject every earlier or newer schema without migrating or marking saved bodies complete.
- **Approve the displayed revision.** Send buttons identify the exact draft shown. An edit is sendable only after Telegram confirms its display. Drafts over 2,500 characters are refused.
- **Check page link boundaries.** A page link must use the exact saved URL followed by whitespace, end of text, or plain sentence punctuation (`.,;:!?`) ending at whitespace or end of text. Reject every other suffix before a live check or send. Links on the page host must obey this rule; the configured booking link is exempt. Check the saved page URL with its marker. Model link tokens stand alone with whitespace around them.
- **External work is claimed first.** Save card-post intent and page markers before calling Telegram or the publisher, and claim Edit callbacks before posting prompts. Uncertain card posts stay locked for a person; page recovery only checks the saved URL. Save rule decisions or drafts before optional model calls and disable SDK retries so a restart never repeats an uncertain paid call.
- **Follow-up recovery is idempotent.** Reconcile terminal sends, cards and follow-ups on restart using the ledger's original confirmation or resolution timestamp, even when the card was not settled before a crash. Reconcile skipped, stale, closed and dry-run follow-up cards too. Empty `gaps_days` cancels scheduled follow-ups and closes pending follow-up cards before taps.
- **Rate limits stop the tick.** Preserve and validate Retry-After headers from SDK errors and the standard-library HTTP transport, using the longest valid body or header delay. Propagate delays through fallback handlers and persist the cooldown across restarts. Lock an uncertain email call before deferring; keep accepted message IDs for later readback. Never retry an uncertain external write.
- Page-check rate limits use the same cooldown and do not consume a page-failure attempt. The setup page check stops after a rate limit without another GET.
- **Use an explicit time zone.** Follow-ups default to UTC; other zones use IANA names. Empty time zones are rejected. Stop the service before `resolve`, then restart it.
- **Unsubscribes are final.** Rules decide them before the model is asked; suppressed addresses are never sent to.
- **Normalize before classification.** Apply NFKC, fold typographic quotes and apostrophes to ASCII, and case-fold once before quoted-history detection and every classification rule. Keep stored reply bodies unchanged; matched reasons quote normalized text.
- **Parse auto-reply flags once consistently.** Use `messages.is_auto_reply` for provider and stored flags. Only affirmative flags (`true`, `1`, `"true"`, `"1"`) skip replies; other values count as human messages for freshness checks.
- **The model drafts, never sends.** Every model answer goes through `llm.draft_problem` and falls back to the rules.
- **Secrets** come from environment variables. Errors name the call, never the URL or headers (`http.py`). Never commit `.env`, `reply-desk.toml`, `state/` or `site/`.
- Runtime code is standard library only; the Anthropic SDK is an optional extra.

## Development

- Python 3.11+, `uv`. Run `uv run pytest` and `uv run ruff check` before a PR.
- Add a failing test with each behavior change. Network code is tested with fake transports and a fake inbox (`tests/conftest.py`); tests never call real APIs.
- Test data is synthetic: example.com and example.org addresses, short fake IDs, no real names.

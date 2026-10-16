# nova-autopilot

Comment-to-DM for Nova Beauty Clinic's Instagram, the same way dana-autopilot
does it for @danestani.ruzz: a GitHub Actions job every 15 minutes, talking to
Instagram's own API. No Blotato, no ManyChat, no browser.

- `dm_rules.json` the keywords (BOOK, INFO, CHECKLIST...) and the DM each one sends, with up to 3 link buttons. Keywords work on every post.
- `config.json` `dm.mode`: `dry_run` only logs, `live` sends.
- `state/dm_sent.json` who was already sent what, so nobody gets a DM twice. Kept 90 days.
- `scripts/dm_reply.py` the job. Built on dana-autopilot's dm_reply.py.
- `scripts/refresh_token.py` keeps the Instagram token alive (60-day tokens).

Each new keyword comment gets one private reply (Instagram allows it within 7
days of the comment) plus a short public reply under the comment. People who
DM the page a keyword get the same message (within Instagram's 24-hour window).

To add a keyword: add a rule to dm_rules.json and push. Nothing else.

## Before switching to live

Turn off the Blotato automations BOOK (9656), INFO (9657) and CHECKLIST (16759,
16760 on Facebook stays in Blotato) first, or people get two DMs.

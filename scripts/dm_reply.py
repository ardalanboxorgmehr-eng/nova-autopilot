#!/usr/bin/env python3
"""
Comment-to-DM for Nova Beauty Clinic's Instagram, run headless every 15 minutes.
Built on dana-autopilot's dm_reply.py (16 Oct 2026), with one change: Nova's
keywords work on EVERY post (BOOK, INFO, CHECKLIST, ...), not one keyword per post.

For each NEW comment that contains a keyword it sends the commenter one private
reply (Instagram's "private reply" to a comment), then a short threaded public
reply. People who DM the page a keyword get the same message from the inbox.

Rules live in dm_rules.json:
  {"exclude": [...], "rules": [{"id", "keywords": [...], "message", "buttons"?: [...]}]}

Safety:
  * config.json "dm.mode" must be "live" to send anything. "dry_run" only logs.
  * Comments older than config.json "dm.start_at" are never touched.
  * One DM per comment and one per person per rule (state/dm_sent.json).
  * Instagram only allows a private reply within 7 days of the comment, and an
    inbox reply within 24 hours of their message.
  * At most MAX_SENDS_PER_RUN sends a run, a few seconds apart.

Exit codes: 0 fine, 1 sends failing or an alert, 2 the Instagram connection is
broken (GitHub then emails the owner).
Env: IG_ACCESS_TOKEN (required). DM_MODE overrides config ("dry_run"/"live").
"""
import json, os, random, re, sys, time, unicodedata
import urllib.parse, urllib.request, urllib.error
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG = os.path.join(ROOT, "config.json")
RULES = os.path.join(ROOT, "dm_rules.json")
STATE = os.path.join(ROOT, "state", "dm_sent.json")

MAX_SENDS_PER_RUN = 40
SEND_GAP_S = 3
MAX_ERROR_STREAK = 3
MAX_TRIES = 4
STALL_ALERT_H = 2
ALERT_REPEAT_H = 12
RUN_BUDGET_S = 20 * 60
WINDOW_DAYS = 7
MAX_POSTS = 200               # newest posts read each run
PUBLIC_REPLIES = [
    "Sent you a DM 📩",
    "Just sent it to your DMs ✨",
    "Check your DMs, it's there 📩",
    "Sent, have a look in your inbox 🌿",
]
BOUNCE_CODE = "2534001"
INBOX_HOURS = 24


def bounce_text(keyword):
    return f"We couldn't DM you, your inbox is closed to business accounts. Send us a DM saying {keyword.upper()} and we'll reply there 📩"


class IGError(RuntimeError):
    pass


# ------------------------------------------------------------------ text

def norm(s):
    s = unicodedata.normalize("NFKC", s or "").lower()
    s = "".join(ch for ch in s if unicodedata.category(ch) != "Mn")
    return re.sub(r"[\s‌‍]+", "", s)


def tokens(s):
    s = unicodedata.normalize("NFKC", s or "")
    return {norm(w) for w in re.split(r"[^\w]+|_", s) if norm(w)}


def edit_distance(a, b):
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def hit(text, words, k):
    """Whole-word match (so BOOK does not fire on 'facebook' or 'booking'), plus a
    small typo allowance on very short comments ('CHEKLIST', 'bok')."""
    nk = norm(k)
    if not nk:
        return False
    if nk in words:
        return True
    t = norm(text)
    if len(nk) >= 5 and len(t) <= len(nk) + 3:
        return edit_distance(t, nk) <= (1 if len(nk) <= 6 else 2)
    return False


def matches(text, keywords, exclude):
    words = tokens(text)
    if any(hit(text, words, x) for x in exclude):
        return False
    return any(hit(text, words, k) for k in keywords)


def rule_for(text, rules, exclude):
    t = norm(text)
    for r in rules:
        if matches(text, r["keywords"], exclude):
            return r
        # "short_keywords" (eyes, spf, pores...) fire only when the comment is
        # just that word, so "my eyes look tired" never triggers a DM.
        for k in r.get("short_keywords", []):
            nk = norm(k)
            if nk and len(t) <= len(nk) + 3 and nk in t:
                return r
    return None


# ------------------------------------------------------------------ io

def load(path, default=None):
    if not os.path.exists(path):
        return default
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, path)


# ------------------------------------------------------------------ api

class IG:
    def __init__(self, token, version):
        self.token = token
        self.base = f"https://graph.instagram.com/{version}"

    def _call(self, method, path, params=None, body=None):
        if path.startswith("http"):
            url = path
        else:
            params = dict(params or {}, access_token=self.token)
            url = f"{self.base}{path}?{urllib.parse.urlencode(params)}"
        data, headers = None, {}
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        elif method == "POST":
            data = b""
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            raise IGError(f"{method} {path[:60]} -> HTTP {e.code}: {e.read().decode(errors='replace')[:400]}") from None
        except urllib.error.URLError as e:
            raise IGError(f"{method} {path[:60]} -> {e.reason}") from None

    def me(self):
        return self._call("GET", "/me", {"fields": "user_id,username"})

    def recent_media(self, limit=MAX_POSTS):
        out = []
        r = self._call("GET", "/me/media", {"fields": "id,caption,timestamp", "limit": 100})
        while True:
            out += r.get("data", [])
            nxt = r.get("paging", {}).get("next")
            if not nxt or len(out) >= limit:
                return out[:limit]
            r = self._call("GET", nxt)

    def comments(self, media_id, since=None):
        out = []
        try:
            r = self._call("GET", f"/{media_id}/comments", {"fields": "id,text,timestamp,from,username,replies{username,from}", "limit": 50})
        except IGError:
            r = self._call("GET", f"/{media_id}/comments", {"fields": "id,text,timestamp,from,username", "limit": 50})
        while True:
            page = r.get("data", [])
            out += page
            nxt = r.get("paging", {}).get("next")
            if not nxt or len(out) > 6000:
                return out
            if since is not None and page:
                stamps = [ts(c["timestamp"]) for c in page if c.get("timestamp")]
                if stamps and all(a >= b for a, b in zip(stamps, stamps[1:])) and stamps[-1] < since:
                    return out
            r = self._call("GET", nxt)

    def private_reply(self, comment_id, message):
        return self._call("POST", "/me/messages", body={"recipient": {"comment_id": comment_id}, "message": message})

    def public_reply(self, comment_id, text):
        return self._call("POST", f"/{comment_id}/replies", {"message": text})

    def conversations(self):
        return self._call("GET", "/me/conversations", {
            "platform": "instagram", "limit": 50,
            "fields": "updated_time,messages.limit(10){id,created_time,from,message}"}).get("data", [])

    def send_to_user(self, user_id, message):
        return self._call("POST", "/me/messages", body={"recipient": {"id": user_id}, "message": message})


def ts(s):
    return datetime.strptime(s.replace("Z", "+0000"), "%Y-%m-%dT%H:%M:%S%z")


def build_message(rule):
    """Text, or a button template when the rule has buttons (max 3, title <= 20)."""
    btns = rule.get("buttons") or []
    if not btns:
        return {"text": rule["message"]}
    return {"attachment": {"type": "template", "payload": {
        "template_type": "button", "text": rule["message"][:640],
        "buttons": [{"type": "web_url", "url": b["url"], "title": b["title"][:20]} for b in btns[:3]]}}}


def text_fallback(rule):
    lines = [rule["message"]] + [f"{b['title']}: {b['url']}" for b in rule.get("buttons") or []]
    return {"text": "\n".join(lines)[:1000]}


def send(fn, target, rule):
    try:
        return fn(target, build_message(rule))
    except IGError as e:
        if rule.get("buttons") and "HTTP 400" in str(e):
            return fn(target, text_fallback(rule))   # template refused: plain text with links
        raise


# ------------------------------------------------------------------ main

def run(ig, cfg, state, now, log=print, checkpoint=None):
    dmcfg = cfg.get("dm", {})
    mode = os.environ.get("DM_MODE") or dmcfg.get("mode", "dry_run")
    start_at = ts(dmcfg["start_at"]) if dmcfg.get("start_at") else now
    look = dmcfg.get("dry_run_lookback_hours")
    if look and mode != "live":
        start_at = now - timedelta(hours=float(look))
    window_start = max(start_at, now - timedelta(days=WINDOW_DAYS))
    cfg_rules = load(RULES, {"rules": [], "exclude": []})
    rules, exclude = cfg_rules.get("rules", []), cfg_rules.get("exclude", [])
    forget_retryable(state)

    try:
        who = ig.me()
    except IGError as e:
        log(f"CONNECTION BROKEN: {e}")
        return 2
    own = {str(who.get("user_id", "")), who.get("username", "")}
    log(f"connected to @{who.get('username')} · mode={mode} · comments after {window_start.isoformat()}")

    started = time.monotonic()
    sends = errors = skipped = streak = posts_read = 0
    waiting = 0
    failures = []
    for m in ig.recent_media():
        if time.monotonic() - started > RUN_BUDGET_S:
            log("time budget used, the rest go next run")
            break
        try:
            comments = ig.comments(m["id"], since=window_start)
        except IGError as e:
            failures.append({"post": m["id"], "error": "read: " + str(e)[:300]})
            log(f"READ FAILED {m['id']}: {e}")
            continue
        posts_read += 1
        done = state.setdefault(m["id"], {})
        sent_to = {(v.get("user"), v.get("rule")) for v in done.values() if "failed" not in v}
        for c in sorted(comments, key=lambda c: c.get("timestamp", "")):
            if c["id"] in done or ts(c["timestamp"]) < window_start:
                continue
            frm = c.get("from") or {}
            user = frm.get("username") or c.get("username", "")
            if str(frm.get("id", "")) in own or user in own:
                continue
            rule = rule_for(c.get("text", ""), rules, exclude)
            if not rule:
                continue
            rid = rule["id"]
            replies = (c.get("replies") or {}).get("data", [])
            if any((r.get("username") in own) or (str((r.get("from") or {}).get("id", "")) in own) for r in replies) \
                    or (user, rid) in sent_to:
                if mode == "live":
                    done[c["id"]] = {"user": user, "rule": rid, "at": now.isoformat(), "skipped": "already handled"}
                skipped += 1
                continue
            if sends + errors >= MAX_SENDS_PER_RUN or streak >= MAX_ERROR_STREAK:
                waiting += 1
                continue
            if mode != "live":
                log(f"[dry run] {rid}: would DM @{user} for «{c.get('text','')[:40]}»")
                continue
            try:
                send(ig.private_reply, c["id"], rule)
            except IGError as e:
                errors += 1
                streak += 1
                err = str(e)[:300]
                failures.append({"post": m["id"], "user": user, "error": err})
                log(f"DM FAILED {rid} @{user}: {err}")
                tries = state.setdefault("_tries", {})
                tries[c["id"]] = tries.get(c["id"], 0) + 1
                if (BOUNCE_CODE in err and streak == 1) or tries[c["id"]] >= MAX_TRIES:
                    done[c["id"]] = {"user": user, "rule": rid, "at": now.isoformat(), "failed": err,
                                     "tries": tries.pop(c["id"])}
                    bounce(ig, c["id"], done[c["id"]], rule["keywords"][0], now, log)
                time.sleep(SEND_GAP_S * 3)
                continue
            streak = 0
            done[c["id"]] = {"user": user, "rule": rid, "at": now.isoformat()}
            sent_to.add((user, rid))
            sends += 1
            if checkpoint:
                checkpoint()
            log(f"DM sent {rid} @{user}")
            try:
                ig.public_reply(c["id"], random.choice(PUBLIC_REPLIES))
            except IGError as e:
                done[c["id"]]["public_failed"] = str(e)[:200]
            time.sleep(SEND_GAP_S)
    inbox_sent = 0
    if dmcfg.get("inbox", True) and sends < MAX_SENDS_PER_RUN:
        inbox_sent = answer_inbox(ig, state, rules, own, exclude, mode,
                                  max(start_at, now - timedelta(hours=INBOX_HOURS)), now,
                                  MAX_SENDS_PER_RUN - sends, log)
    state["_report"] = {"at": now.isoformat(), "mode": mode, "sent": sends, "failed": errors,
                        "skipped": skipped, "inbox_sent": inbox_sent, "posts_read": posts_read,
                        "failures": failures[:20]}
    log(f"done: {sends} sent, {errors} failed, {skipped} skipped, {inbox_sent} answered from the inbox, {posts_read} posts read")
    alert = health(state, now, mode, sends + inbox_sent, waiting, log)
    return 1 if (errors and not sends) or alert else 0


def bounce(ig, comment_id, rec, keyword, now, log):
    try:
        ig.public_reply(comment_id, bounce_text(keyword))
        rec["bounced"] = now.isoformat()
    except IGError as e:
        rec["bounce_failed"] = str(e)[:200]
        log(f"bounce reply failed @{rec.get('user')}: {e}")


def answer_inbox(ig, state, rules, own, exclude, mode, since, now, budget, log):
    try:
        convs = ig.conversations()
    except IGError as e:
        log(f"inbox not readable, skipped: {e}")
        return 0
    box = state.setdefault("_inbox", {})
    sent = 0
    for conv in convs:
        msgs = (conv.get("messages") or {}).get("data", [])
        for mm in sorted(msgs, key=lambda x: x.get("created_time", "")):
            if sent >= budget:
                return sent
            frm = mm.get("from") or {}
            uid, user = str(frm.get("id", "")), frm.get("username", "")
            if not uid or uid in own or user in own or mm.get("id") in box:
                continue
            try:
                if ts(mm["created_time"]) < since:
                    continue
            except (KeyError, ValueError):
                continue
            rule = rule_for(mm.get("message", ""), rules, exclude)
            if not rule:
                continue
            rid = rule["id"]
            if any(v.get("uid") == uid and v.get("rule") == rid and "failed" not in v and
                   v.get("at", "") > (now - timedelta(days=1)).isoformat() for v in box.values()):
                if mode == "live":
                    box[mm["id"]] = {"user": user, "uid": uid, "rule": rid, "at": now.isoformat(), "skipped": "already has it"}
                continue
            if mode != "live":
                log(f"[dry run] inbox {rid}: would DM @{user} for «{mm.get('message','')[:30]}»")
                continue
            try:
                send(ig.send_to_user, uid, rule)
            except IGError as e:
                box[mm["id"]] = {"user": user, "uid": uid, "rule": rid, "at": now.isoformat(), "failed": str(e)[:300]}
                log(f"inbox DM FAILED {rid} @{user}: {e}")
                continue
            box[mm["id"]] = {"user": user, "uid": uid, "rule": rid, "at": now.isoformat()}
            sent += 1
            log(f"inbox DM sent {rid} @{user}")
            time.sleep(SEND_GAP_S)
    return sent


def health(state, now, mode, sent, waiting, log):
    h = state.setdefault("_health", {})
    if sent:
        h["last_sent_at"] = now.isoformat()
    h.update({"at": now.isoformat(), "mode": mode, "waiting": waiting})
    last = h.get("last_sent_at")
    problems = []
    if mode == "live" and waiting and (not last or datetime.fromisoformat(last) < now - timedelta(hours=STALL_ALERT_H)):
        problems.append(f"{waiting} keyword comments waiting and nothing sent for {STALL_ALERT_H}+ hours")
    h["problems"] = problems
    if not problems:
        h.pop("alerted_at", None)
        return False
    for p in problems:
        log("ALERT: " + p)
    la = h.get("alerted_at")
    if la and datetime.fromisoformat(la) > now - timedelta(hours=ALERT_REPEAT_H):
        return False
    h["alerted_at"] = now.isoformat()
    return True


def forget_retryable(state):
    for mid, recs in state.items():
        if mid.startswith("_") or not isinstance(recs, dict):
            continue
        for cid in list(recs):
            r = recs[cid]
            f = r.get("failed", "")
            if f and ("bounce_failed" in r or ("HTTP 500" in f and not r.get("tries"))):
                del recs[cid]


def prune(state, now, days=90):
    cutoff = now - timedelta(days=days)
    for mid in list(state):
        if mid.startswith("_") and mid != "_inbox":
            continue
        recs = state[mid]
        for cid in list(recs):
            try:
                if datetime.fromisoformat(recs[cid]["at"]) < cutoff:
                    del recs[cid]
            except (KeyError, ValueError):
                pass
        if not recs:
            del state[mid]


def main():
    token = os.environ.get("IG_ACCESS_TOKEN", "").strip()
    if not token:
        print("CONNECTION BROKEN: IG_ACCESS_TOKEN is empty")
        return 2
    cfg = load(CONFIG, {})
    state = load(STATE, {})
    ig = IG(token, cfg.get("api_version", "v25.0"))
    now = datetime.now(timezone.utc)
    try:
        code = run(ig, cfg, state, now, checkpoint=lambda: save(STATE, state))
    finally:
        prune(state, now)
        save(STATE, state)
    return code


if __name__ == "__main__":
    sys.exit(main())

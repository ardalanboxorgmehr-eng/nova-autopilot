#!/usr/bin/env python3
"""
Keep the Instagram long-lived token alive.

Long-lived tokens last 60 days. Refreshing returns a NEW token string, so it is
only useful if we can store it. Two modes:

  With GH_PAT set   refresh, then write the new token back to the repo secret
                    IG_ACCESS_TOKEN. Fully hands-off.
  Without GH_PAT    do not refresh (a refreshed token we cannot store is worse
                    than useless, the old one keeps working but the clock is
                    still ticking). Report how long is left and fail loudly when
                    it is getting close, so GitHub emails you.

The token is never printed. GH_PAT needs a fine-grained PAT with read+write on
this repository's Secrets.
"""
import json, os, sys, base64, urllib.parse, urllib.request, urllib.error

WARN_DAYS = 14


def api(url):
    try:
        with urllib.request.urlopen(url, timeout=60) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        sys.exit(f"token call failed: HTTP {e.code}: {e.read().decode(errors='replace')}")


def gh(path, token, method="GET", payload=None):
    url = f"https://api.github.com{path}"
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "Content-Type": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            body = r.read().decode()
            return json.loads(body) if body.strip() else {}
    except urllib.error.HTTPError as e:
        sys.exit(f"GitHub {method} {path} failed: HTTP {e.code}: "
                 f"{e.read().decode(errors='replace')}")


def store_secret(repo, pat, name, value):
    try:
        from nacl import encoding, public
    except ImportError:
        sys.exit("PyNaCl is needed to write a repo secret: pip install pynacl")
    key = gh(f"/repos/{repo}/actions/secrets/public-key", pat)
    sealed = public.SealedBox(
        public.PublicKey(key["key"].encode(), encoding.Base64Encoder())
    ).encrypt(value.encode())
    gh(f"/repos/{repo}/actions/secrets/{name}", pat, "PUT",
       {"encrypted_value": base64.b64encode(sealed).decode(),
        "key_id": key["key_id"]})


def main():
    token = os.environ.get("IG_ACCESS_TOKEN")
    if not token:
        sys.exit("IG_ACCESS_TOKEN is not set")
    pat = os.environ.get("GH_PAT")
    repo = os.environ.get("GITHUB_REPOSITORY", "")

    if not pat:
        # Cannot store a new token, so do not mint one. Just report the clock.
        info = api("https://graph.instagram.com/refresh_access_token?"
                   + urllib.parse.urlencode({"grant_type": "ig_refresh_token",
                                             "access_token": token}))
        days = int(info.get("expires_in", 0)) // 86400
        print(f"token has about {days} days left")
        print("auto-refresh is OFF (no GH_PAT secret). The refreshed token was "
              "discarded, the current one still works.")
        if days <= WARN_DAYS:
            sys.exit(f"ACTION NEEDED: refresh IG_ACCESS_TOKEN by hand, "
                     f"about {days} days left. See SETUP.md step 2.")
        return 0

    info = api("https://graph.instagram.com/refresh_access_token?"
               + urllib.parse.urlencode({"grant_type": "ig_refresh_token",
                                         "access_token": token}))
    new = info.get("access_token")
    days = int(info.get("expires_in", 0)) // 86400
    if not new:
        sys.exit(f"no access_token in refresh response: {info}")
    if not repo:
        sys.exit("GITHUB_REPOSITORY is not set")
    store_secret(repo, pat, "IG_ACCESS_TOKEN", new)
    print(f"token refreshed and stored, about {days} days left")
    return 0


if __name__ == "__main__":
    sys.exit(main())

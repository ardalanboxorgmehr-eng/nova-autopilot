# Setup (one time, about 15 minutes, on the Mac)

The steps that need your logins. Never paste a token into chat.

1. GitHub: create an empty PUBLIC repository called `nova-autopilot` on the
   ardalanboxorgmehr-eng account (public, because Actions minutes are free on
   public repos; nothing secret lives in the files). Claude pushes the code.
2. Meta: developers.facebook.com, signed in as the Facebook account that owns
   Nova's Page. Create App, use case "Manage messaging and content on
   Instagram". In "API setup with Instagram login", add the Nova Instagram
   account (log in to @nova.beautyclinic in the pop-up), then Generate token.
3. GitHub repo, Settings, Secrets and variables, Actions, New repository secret:
   `IG_ACCESS_TOKEN` = the token from step 2.
4. Optional, hands-off token renewal: a fine-grained PAT for this repo only
   with Secrets read and write, saved as secret `GH_PAT`.
5. Tell Claude "token in". Claude runs a dry run, checks the log, then switches
   to live and turns the Blotato Instagram automations off.

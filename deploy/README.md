# Deploying MBY-Trading: Railway + Cloudflare

Target layout

    Railway project "mby-trading"
      Postgres     the database (copied from the Mac)
      desk         cron, every 15 min: python -m orchestrator auto
      dashboard    Streamlit, private network only, port 8080
      tunnel       cloudflared, connects the dashboard to Cloudflare
    Cloudflare
      domain       mby-trading.com (or the variant you bought)
      Access       email one-time-code login, your address only
      Tunnel       https://<domain>  ->  http://dashboard.railway.internal:8080

Why a tunnel instead of a Railway public domain: a public Railway
address stays reachable by anyone who finds it, and would bypass the
Cloudflare login. With a tunnel the dashboard has NO public address at
all; the only way in is through Cloudflare Access.

The one rule: the Mac scheduler and the Railway desk must never both
be on. Do the cutover on a weekend, in the order below.

---------------------------------------------------------------------
## 1. Commit the deploy files (any time)

    git add Dockerfile .dockerignore deploy
    git commit -m "Deploy: Dockerfile, Railway service configs, runbook"
    git push

Check on GitHub that the repository is PRIVATE.

## 2. Buy the domain (any time)

Cloudflare dashboard -> Domain Registration -> Register Domains ->
search `mby-trading.com`. Fallbacks: mbytrading.com, mby-trading.io,
mby-trading.app. Buying it there puts DNS on Cloudflare automatically.

## 3. Rotate the FMP key (any time before step 6)

The current key is stored in old error rows in the database and has
been pasted into chats. Create a new key at FMP; you will give the new
one to Railway in step 6 and delete the old one after cutover.

## 4. Stop the Mac desk, then copy the database (weekend)

    launchctl unload ~/Library/LaunchAgents/com.mby.trading.desk.plist
    launchctl list | grep mby          # must print nothing

In Railway: New Project -> name it `mby-trading` -> Add -> Database ->
PostgreSQL. Open the Postgres service -> Variables -> copy
DATABASE_PUBLIC_URL (starts with postgresql://). Then on the Mac:

    cd ~/Projects/mby-trading
    docker exec mby-trading-db pg_dump -U mby -d mby_trading \
        --no-owner --no-privileges -Fc > ~/mby_trading.dump

    docker exec -i mby-trading-db pg_restore --no-owner --no-privileges \
        -d "PASTE_DATABASE_PUBLIC_URL" < ~/mby_trading.dump

Verify the copy (the inline variable overrides .env for one command):

    DATABASE_URL="PASTE_DATABASE_PUBLIC_URL" .venv/bin/python -m core.check_schema
    DATABASE_URL="PASTE_DATABASE_PUBLIC_URL" .venv/bin/python -m orchestrator status

Expect "Schema check passed" and the same status you see locally.
Then `rm ~/mby_trading.dump` (it contains the old FMP key).

## 5. Dashboard service

Railway project -> Add -> GitHub Repo -> this repository.
  Settings -> Service name:        dashboard      (exactly; it becomes
                                   dashboard.railway.internal)
  Settings -> Config-as-code path: /deploy/dashboard.railway.json
  Settings -> Networking:          do NOT generate a public domain
  Variables:
      DATABASE_URL = ${{Postgres.DATABASE_URL}}

## 6. Desk service

Add -> GitHub Repo -> the same repository again.
  Settings -> Service name:        desk
  Settings -> Config-as-code path: /deploy/desk.railway.json
  Settings -> Networking:          no public domain
  Variables:
      DATABASE_URL      = ${{Postgres.DATABASE_URL}}
      ANTHROPIC_API_KEY = ...
      ALPACA_API_KEY    = ...
      ALPACA_SECRET_KEY = ...
      ALPACA_PAPER      = true        (exactly this, lowercase)
      FMP_API_KEY       = the NEW key from step 3
      EDGAR_USER_AGENT  = same value as in .env

The cron fires every 15 minutes; outside the three windows the run
prints "nothing due" and exits. If a run is still going at the next
tick, Railway skips that tick, so two runs never overlap.

## 7. Cloudflare login FIRST, then the tunnel

Cloudflare -> Zero Trust.

a) Access -> Applications -> Add -> Self-hosted
     Application domain: <your domain>
     Policy: Allow, Include -> Emails -> your address
     Login method: One-time PIN
   Doing this before (b) means the site is never open, even briefly.

b) Networks -> Tunnels -> Create -> Cloudflared -> name `mby-trading`
     Copy the tunnel token.
     Public hostname: <your domain>, Service: HTTP,
     URL: dashboard.railway.internal:8080

c) Railway -> Add -> Template -> "Cloudflare Tunnel" (cloudflared)
     Variable: TUNNEL_TOKEN = the token from (b)
   (Manual alternative: Add -> Docker Image
    cloudflare/cloudflared:latest, start command
    `cloudflared tunnel --no-autoupdate run`, same variable.)

Open https://<your domain>: Cloudflare asks for your email, sends a
code, then shows the dashboard.

## 8. Check on Monday

About 13:15 Berlin the schedule strip on the dashboard should show
premarket done. Railway -> desk -> Deployments shows one log per tick.

---------------------------------------------------------------------
## Rollback

Railway -> desk -> Settings -> remove the cron schedule (or delete the
service). Then on the Mac:

    launchctl load ~/Library/LaunchAgents/com.mby.trading.desk.plist

The local database stops at the moment of the dump. After even one
session on Railway, going back means dumping Railway and restoring it
locally (step 4 in reverse), not just flipping the scheduler.

## Not covered here

- Alerts when a run fails (still to build).
- Railway backups: enable them on the Postgres service once live.

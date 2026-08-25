# hermes-max-plugin

MAX (max.ru) platform plugin for Hermes Agent.

This plugin lets Hermes receive and send messages in MAX using **Long Polling** via `https://platform-api2.max.ru`.

## Features

- Long Polling message intake via `GET /updates`
- Text replies via `POST /messages`
- DM and group routing
- File/image sending via MAX upload flow
- User allowlist support
- Cron/home-channel delivery support
- macOS SSL fix via `truststore`

## Current scope

- ✅ Long Polling
- ✅ MAX direct messages
- ✅ Hermes gateway integration
- ✅ User-service deployment on macOS
- ❌ Webhook mode (not implemented yet)

## Repository layout

- `adapter.py` — Hermes platform adapter
- `plugin.yaml` — plugin manifest and env declarations
- `__init__.py` — plugin entrypoint
- `docs/DEPLOYMENT.md` — install/deploy/runbook

## Required environment variables

- `MAX_BOT_TOKEN` — bot token from MasterBot

## Optional environment variables

- `MAX_ALLOWED_USERS` — comma-separated MAX user IDs
- `MAX_ALLOW_ALL_USERS` — `true` to disable allowlist
- `MAX_HOME_CHANNEL` — default destination for cron/notifications
- `MAX_HOME_CHANNEL_NAME` — human label for home channel
- `MAX_MARKDOWN` — `true`/`false`, default `true`
- `MAX_POLL_TIMEOUT` — long-poll timeout, default `30`

## Install into Hermes

Copy or sync this repository into your Hermes user plugins directory so the repo root becomes the plugin root:

```bash
mkdir -p ~/.hermes/plugins/max
rsync -av --delete ./ ~/.hermes/plugins/max/ \
  --exclude .git --exclude __pycache__
```

Then ensure the token exists in `~/.hermes/.env`:

```bash
MAX_BOT_TOKEN=your_token_here
```

And verify Hermes sees the plugin:

```bash
hermes plugins list | grep max
```

## Start the gateway manually

```bash
hermes gateway run -v
```

Expected log lines:

```text
Connecting to max...
[max] Connected as ...
✓ max connected
```

## Service deployment

See [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md).

## Security notes

- Do **not** commit `.env` files or bot tokens.
- This repository intentionally excludes secrets via `.gitignore`.
- The plugin reads secrets from `~/.hermes/.env` / Hermes secret scope.

## Status

This repository contains the working Long Polling implementation currently validated on macOS with Hermes gateway and a live MAX bot.

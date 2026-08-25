# Deployment and operations

## 1. Install plugin files

From the repository root:

```bash
mkdir -p ~/.hermes/plugins/max
rsync -av --delete ./ ~/.hermes/plugins/max/ \
  --exclude .git --exclude __pycache__
```

## 2. Configure secrets

Put the bot token in `~/.hermes/.env`:

```bash
MAX_BOT_TOKEN=your_token_here
```

Optional:

```bash
MAX_ALLOWED_USERS=12345,67890
MAX_MARKDOWN=true
MAX_POLL_TIMEOUT=30
```

## 3. Verify plugin loading

```bash
hermes plugins list | grep max
```

## 4. Verify gateway startup manually

```bash
hermes gateway run -v
```

Healthy startup should include:

```text
Connecting to max...
[max] Connected as ...
✓ max connected
Gateway running with 2 platform(s)
```

## 5. Install as a user service (macOS)

```bash
hermes gateway install --force --start-now --start-on-login
```

Check status:

```bash
hermes gateway status
```

## 6. Update workflow

After changing the repository version of the plugin:

```bash
rsync -av --delete ./ ~/.hermes/plugins/max/ \
  --exclude .git --exclude __pycache__
hermes gateway restart
```

If `restart` is unavailable in your Hermes version, stop/start the service instead.

## 7. Troubleshooting

### Plugin does not load

Check:

```bash
hermes plugins list | grep max
```

If missing, verify `plugin.yaml` is at the plugin root and the directory is exactly:

```text
~/.hermes/plugins/max/
```

### SSL certificate verify failed on macOS

This plugin uses `truststore` to patch Python SSL and read the macOS Keychain.

### Gateway starts without MAX

Run verbose startup and inspect logs:

```bash
hermes gateway run -v 2>&1 | grep -i max
```

### Unauthorized user

Set `MAX_ALLOWED_USERS` to the allowed MAX user IDs, or for development only:

```bash
MAX_ALLOW_ALL_USERS=true
```

## 8. Notes

- This plugin currently targets Long Polling only.
- Webhook mode is intentionally deferred.
- The plugin is designed to work without external MAX SDKs.

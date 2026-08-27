"""MAX messenger platform adapter for Hermes Agent.

Receives messages via Long Polling (GET /updates) and replies via
POST /messages. No external SDK — only httpx (already a Hermes dependency).

Configuration in config.yaml::

    platforms:
      max:
        enabled: true

Environment variables::

    MAX_BOT_TOKEN           Bot token from MasterBot (required)
    MAX_ALLOWED_USERS       Comma-separated user_id allowlist
    MAX_ALLOW_ALL_USERS     "true" to disable allowlist (dev only)
    MAX_HOME_CHANNEL        Default chat_id for cron delivery
    MAX_HOME_CHANNEL_NAME   Display name for home channel
    MAX_MARKDOWN            "true"/"false" — send with format=markdown (default: true)
    MAX_POLL_TIMEOUT        Long-poll timeout seconds (default: 30)

API base: https://platform-api2.max.ru
Auth: Authorization: <token> header (query param no longer supported)
Docs: https://dev.max.ru/docs-api
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

try:
    import httpx
    HTTPX_AVAILABLE = True
except ImportError:
    HTTPX_AVAILABLE = False
    httpx = None  # type: ignore[assignment]

# macOS uv-Python ships its own OpenSSL that ignores the System Keychain.
# inject_into_ssl() patches the ssl module globally so ALL httpx connections
# use macOS Security framework and modern LE chains (YR2→Root YR→Root X1) work.
try:
    import truststore as _truststore
    _truststore.inject_into_ssl()
    _TRUSTSTORE_ACTIVE = True
    logger.info("[max] truststore injected into ssl (macOS keychain)")
except Exception as _ts_exc:
    _TRUSTSTORE_ACTIVE = False
    logger.warning("[max] truststore not available (%s), using default ssl", _ts_exc)

try:
    from gateway.config import Platform, PlatformConfig
    from gateway.platforms.base import (
        BasePlatformAdapter,
        MessageEvent,
        MessageType,
        SendResult,
    )
except ImportError:
    # Stubs for environments where gateway is not on sys.path (plugin scan)
    Platform = None  # type: ignore[assignment]
    PlatformConfig = object  # type: ignore[assignment,misc]
    BasePlatformAdapter = object  # type: ignore[assignment]
    MessageEvent = None  # type: ignore[assignment]
    MessageType = None  # type: ignore[assignment]
    SendResult = None  # type: ignore[assignment]

try:
    from agent.secret_scope import UnscopedSecretError as _UnscopedSecretError
    from agent.secret_scope import get_secret as _scoped_get_secret
    _SECRET_SCOPE_AVAILABLE = True
except ImportError:
    _SECRET_SCOPE_AVAILABLE = False
    _UnscopedSecretError = Exception  # type: ignore[assignment,misc]
    def _scoped_get_secret(name: str, default: str = "") -> str:  # type: ignore[misc]
        return os.getenv(name, default) or default

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

API_BASE = "https://platform-api2.max.ru"
MAX_MESSAGE_LENGTH = 4000          # MAX soft limit for text messages
DEDUP_WINDOW_SECONDS = 300
DEDUP_MAX_SIZE = 1000
RECONNECT_BACKOFF = [2, 5, 10, 30, 60]
DEFAULT_POLL_TIMEOUT = 30          # seconds for long-poll


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_scoped_secret(name: str, default: str = "") -> str:
    """Scope-aware credential read with default-profile fallback."""
    try:
        val = _scoped_get_secret(name, default)
    except _UnscopedSecretError:
        val = os.getenv(name)
    return val if val is not None else default


def _auth_headers(token: str) -> Dict[str, str]:
    """Return Authorization header dict for MAX API."""
    return {"Authorization": token} if token else {}


def _truncate(text: str, limit: int = MAX_MESSAGE_LENGTH) -> str:
    if len(text) <= limit:
        return text
    logger.warning("[max] Truncating message from %d to %d chars", len(text), limit)
    return text[:limit]


def _parse_bool_env(name: str, default: bool = False) -> bool:
    val = os.getenv(name, "").strip().lower()
    if not val:
        return default
    return val in ("1", "true", "yes")


# ---------------------------------------------------------------------------
# check / validate helpers (called by plugin registry)
# ---------------------------------------------------------------------------

def check_requirements() -> bool:
    """Return True when httpx is available and MAX_BOT_TOKEN is set."""
    if not HTTPX_AVAILABLE:
        return False
    token = os.getenv("MAX_BOT_TOKEN", "").strip()
    return bool(token)


def validate_config(config: PlatformConfig) -> bool:
    extra = getattr(config, "extra", {}) or {}
    token = extra.get("token") or os.getenv("MAX_BOT_TOKEN", "")
    return bool(token)


def is_connected(config: PlatformConfig) -> bool:
    return bool(os.getenv("MAX_BOT_TOKEN", "").strip())


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------

class MaxAdapter(BasePlatformAdapter):
    """MAX messenger adapter.

    Uses long polling (GET /updates) to receive events and httpx for all
    outbound API calls. Reconnects automatically with exponential back-off.
    """

    MAX_MESSAGE_LENGTH = MAX_MESSAGE_LENGTH

    def __init__(self, config: PlatformConfig) -> None:
        platform = Platform("max")
        super().__init__(config=config, platform=platform)

        extra = config.extra or {}
        self._token: str = extra.get("token") or _get_scoped_secret("MAX_BOT_TOKEN", "")
        self._markdown: bool = extra.get("markdown", _parse_bool_env("MAX_MARKDOWN", True))
        self._poll_timeout: int = int(
            extra.get("poll_timeout") or os.getenv("MAX_POLL_TIMEOUT", str(DEFAULT_POLL_TIMEOUT))
        )

        self._poll_task: Optional[asyncio.Task] = None
        self._http: Optional["httpx.AsyncClient"] = None

        # Dedup: message_id -> timestamp
        self._seen: Dict[str, float] = {}
        # marker tracking which update timestamp to pass to /updates?marker=
        self._last_event_id: Optional[int] = None
        # chat_id -> chat_type ("dm" | "group") — populated from incoming events
        self._chat_types: Dict[str, str] = {}

    # -- Lifecycle ----------------------------------------------------------

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        if not HTTPX_AVAILABLE:
            logger.warning("[max] httpx not installed; run: pip install httpx")
            return False
        if not self._token:
            logger.warning("[max] MAX_BOT_TOKEN not configured")
            return False

        logger.info("[max] Connecting (truststore_active=%s)...", _TRUSTSTORE_ACTIVE)
        self._http = httpx.AsyncClient(timeout=None)

        # Fetch bot info to confirm the token is valid
        try:
            info = await self._api_get("/me")
            bot_name = info.get("name", "MAX Bot")
            logger.info("[max] Connected as %s", bot_name)
        except Exception as exc:
            logger.error("[max] Failed to verify token: %s", exc)
            await self._http.aclose()
            self._http = None
            return False

        self._mark_connected()
        self._poll_task = asyncio.create_task(self._poll_loop())

        # Register bot command menu so the / autocomplete stays in sync
        asyncio.create_task(self._register_commands())

        return True

    async def _register_commands(self) -> None:
        """Push the bot command menu to MAX so the / autocomplete is correct."""
        commands = [
            {"name": "new",      "description": "Новый чат"},
            {"name": "model",    "description": "Выбрать модель"},
            {"name": "sessions", "description": "Мои чаты"},
            {"name": "resume",   "description": "Продолжить чат"},
            {"name": "stop",     "description": "Остановить"},
            {"name": "help",     "description": "Справка"},
            {"name": "commands", "description": "Все команды"},
        ]
        try:
            resp = await self._http.patch(
                f"{API_BASE}/me/commands",
                headers={**_auth_headers(self._token), "Content-Type": "application/json"},
                content=json.dumps({"commands": commands}).encode(),
                timeout=10.0,
            )
            if resp.status_code == 200:
                logger.info("[max] Bot commands menu registered (%d commands)", len(commands))
            else:
                logger.warning("[max] Failed to register commands: HTTP %s %s",
                               resp.status_code, resp.text[:100])
        except Exception as exc:
            logger.warning("[max] _register_commands failed: %s", exc)

    async def disconnect(self) -> None:
        self._running = False
        self._mark_disconnected()

        if self._poll_task:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
            self._poll_task = None

        if self._http:
            await self._http.aclose()
            self._http = None

        self._seen.clear()
        logger.info("[max] Disconnected")

    # -- Long polling loop --------------------------------------------------

    async def _poll_loop(self) -> None:
        """Main long-polling loop with automatic reconnection."""
        backoff_idx = 0

        while self._running:
            try:
                updates = await self._fetch_updates()
                backoff_idx = 0  # reset on success

                for update in updates:
                    await self._dispatch_update(update)

            except asyncio.CancelledError:
                raise
            except Exception as exc:
                wait = RECONNECT_BACKOFF[min(backoff_idx, len(RECONNECT_BACKOFF) - 1)]
                backoff_idx += 1
                logger.warning("[max] Poll error (%s), retrying in %ds", exc, wait)
                try:
                    await asyncio.sleep(wait)
                except asyncio.CancelledError:
                    raise

    async def _fetch_updates(self) -> List[Dict[str, Any]]:
        """Call GET /updates and return the list of update dicts."""
        params: Dict[str, Any] = {"timeout": self._poll_timeout, "limit": 100}
        if self._last_event_id is not None:
            params["marker"] = self._last_event_id

        try:
            resp = await self._http.get(
                f"{API_BASE}/updates",
                headers=_auth_headers(self._token),
                params=params,
                timeout=self._poll_timeout + 10,  # slightly longer than poll timeout
            )
        except httpx.TimeoutException:
            # Normal long-poll timeout — no updates, loop again
            return []

        if resp.status_code == 429:
            logger.warning("[max] Rate limited (429), backing off 60s")
            await asyncio.sleep(60)
            return []

        if resp.status_code != 200:
            raise RuntimeError(f"GET /updates returned HTTP {resp.status_code}: {resp.text[:200]}")

        data = resp.json()

        # Track marker for next poll to avoid re-delivering events
        marker = data.get("marker")
        if marker is not None:
            self._last_event_id = marker

        return data.get("updates", [])

    async def _dispatch_update(self, update: Dict[str, Any]) -> None:
        """Route a single update dict to the appropriate handler."""
        update_type = update.get("update_type") or update.get("type", "")

        if update_type == "message_created":
            await self._on_message(update)
        elif update_type == "message_callback":
            logger.info("[max] callback raw: %s", json.dumps(update, ensure_ascii=False)[:500])
            await self._on_callback(update)
        elif update_type == "bot_started":
            await self._on_bot_started(update)
        elif update_type == "bot_added":
            chat_id = (update.get("chat") or {}).get("chat_id", "?")
            logger.info("[max] Bot added to chat %s", chat_id)
        elif update_type == "bot_removed":
            chat_id = (update.get("chat") or {}).get("chat_id", "?")
            logger.info("[max] Bot removed from chat %s", chat_id)
        else:
            logger.warning("[max] Unhandled update_type=%r keys=%s", update_type, list(update.keys()))

    # -- Inbound handlers ---------------------------------------------------

    async def _on_bot_started(self, update: Dict[str, Any]) -> None:
        """User pressed /start — send a welcome and then handle as a message."""
        chat_id = str((update.get("chat") or {}).get("chat_id") or "")
        if not chat_id:
            return
        await self.send(chat_id, "Привет! Я Hermes — твой AI-ассистент. Чем могу помочь?")

    async def _on_callback(self, update: Dict[str, Any]) -> None:
        """Handle message_callback update from inline keyboard buttons.

        Real MAX structure (from observed traffic):
        {
          "callback": { "user": {user_id, name, ...}, "payload": "...", "callback_id": "..." },
          "message":  { "recipient": { "chat_type": "dialog", "chat_id": ..., "user_id": ... } }
        }
        Note: there is NO message.sender — user info lives in callback.user.
        """
        callback = update.get("callback") or {}
        payload = callback.get("payload", "")

        cb_user = callback.get("user") or {}
        message = update.get("message") or {}
        recipient = message.get("recipient") or {}

        chat_type_raw = recipient.get("chat_type") or "dialog"
        chat_type = "dm" if chat_type_raw == "dialog" else "group"

        # For DM: reply to the sender's user_id (from callback.user)
        if chat_type == "dm":
            chat_id = str(cb_user.get("user_id") or recipient.get("user_id") or "")
        else:
            chat_id = str(recipient.get("chat_id") or "")

        if not chat_id:
            logger.warning("[max] callback: could not determine chat_id, skipping")
            return

        self._chat_types[chat_id] = chat_type
        user_id = str(cb_user.get("user_id") or "")
        user_name = cb_user.get("name") or cb_user.get("first_name") or user_id

        if not payload.startswith("model:"):
            logger.warning("[max] callback: unknown payload %r", payload)
            return

        model_name = payload.split(":", 1)[1]
        logger.info("[max] User %s selected model: %s", user_name, model_name)

        source = self.build_source(
            chat_id=chat_id,
            chat_name=chat_id,
            chat_type=chat_type,
            user_id=user_id,
            user_name=user_name,
        )

        event = MessageEvent(
            text=f"/model {model_name}",
            message_type=MessageType.TEXT,
            source=source,
            message_id=str(uuid.uuid4().hex),
            raw_message=update,
            timestamp=datetime.now(tz=timezone.utc),
        )

        await self.handle_message(event)

    async def _fetch_llm_models(self) -> List[str]:
        """Fetch available model IDs from the configured LLM provider's /v1/models endpoint.

        Reads base_url and api_key from ~/.hermes/config.yaml (the Hermes config file
        that lives in the persistent volume). Falls back to an empty list on any error.
        """
        if not HTTPX_AVAILABLE or not self._http:
            return []

        try:
            import yaml  # bundled with Hermes venv

            hermes_home = os.environ.get("HERMES_HOME") or os.path.expanduser("~/.hermes")
            config_path = os.path.join(hermes_home, "config.yaml")

            with open(config_path) as f:
                cfg = yaml.safe_load(f) or {}

            model_cfg = cfg.get("model") or {}
            base_url = (model_cfg.get("base_url") or "").rstrip("/")
            api_key = model_cfg.get("api_key") or ""

            if not base_url:
                logger.warning("[max] _fetch_llm_models: no base_url in config.yaml")
                return []

            resp = await self._http.get(
                f"{base_url}/models",
                headers={"Authorization": f"Bearer {api_key}"} if api_key else {},
                timeout=10.0,
            )
            if resp.status_code != 200:
                logger.warning("[max] /v1/models returned HTTP %s", resp.status_code)
                return []

            data = resp.json()
            ids = [m["id"] for m in data.get("data", []) if m.get("id")]
            ids.sort()
            logger.info("[max] fetched %d models from %s", len(ids), base_url)
            return ids

        except Exception as exc:
            logger.warning("[max] _fetch_llm_models failed: %s", exc)
            return []

    async def _handle_model_command(self, chat_id: str, text: str) -> None:
        """Handle /model command by showing inline keyboard with model choices."""
        parts = text.split()

        # If model name provided, pass through to Hermes
        if len(parts) > 1:
            source = self.build_source(
                chat_id=chat_id,
                chat_name=chat_id,
                chat_type=self._chat_types.get(chat_id, "dm"),
                user_id=chat_id,
                user_name=chat_id,
            )
            event = MessageEvent(
                text=text,
                message_type=MessageType.TEXT,
                source=source,
                message_id=str(uuid.uuid4().hex),
                raw_message={},
                timestamp=datetime.now(tz=timezone.utc),
            )
            await self.handle_message(event)
            return

        # /model without args — fetch live model list and show as inline buttons, 2 per row
        # Model IDs must match exactly what the inference endpoint returns
        models = await self._fetch_llm_models()

        if not models:
            # Fallback: pass to Hermes which will show its own /model output
            source = self.build_source(
                chat_id=chat_id,
                chat_name=chat_id,
                chat_type=self._chat_types.get(chat_id, "dm"),
                user_id=chat_id,
                user_name=chat_id,
            )
            event = MessageEvent(
                text="/model",
                message_type=MessageType.TEXT,
                source=source,
                message_id=str(uuid.uuid4().hex),
                raw_message={},
                timestamp=datetime.now(tz=timezone.utc),
            )
            await self.handle_message(event)
            return

        buttons: List[List[Dict[str, Any]]] = []
        row: List[Dict[str, Any]] = []
        for model_id in models:
            row.append({"type": "callback", "text": model_id, "payload": f"model:{model_id}"})
            if len(row) == 2:
                buttons.append(row)
                row = []
        if row:
            buttons.append(row)

        await self._send_with_keyboard(
            chat_id=chat_id,
            text="Выберите модель:",
            buttons=buttons
        )

    async def _send_with_keyboard(
        self,
        chat_id: str,
        text: str,
        buttons: List[List[Dict[str, Any]]]
    ) -> SendResult:
        """Send a message with inline keyboard."""
        if not self._http:
            return SendResult(success=False, error="HTTP client not initialized")
        
        chat_type = self._chat_types.get(chat_id, "dm")
        params = {"user_id": chat_id} if chat_type == "dm" else {"chat_id": chat_id}
        
        body: Dict[str, Any] = {
            "text": text,
            "attachments": [{
                "type": "inline_keyboard",
                "payload": {
                    "buttons": buttons
                }
            }]
        }
        
        if self._markdown:
            body["format"] = "markdown"
        
        return await self._do_send(body, params)

    async def _on_message(self, update: Dict[str, Any]) -> None:
        """Handle message_created update."""
        message = update.get("message") or {}
        body = message.get("body") or {}

        msg_id = str(message.get("message_id") or uuid.uuid4().hex)
        if self._is_duplicate(msg_id):
            logger.debug("[max] Duplicate message %s, skipping", msg_id)
            return

        # Ignore messages sent by the bot itself
        sender = message.get("sender") or {}
        if sender.get("is_bot"):
            return

        text = (body.get("text") or "").strip()

        # Extract media info if present (for future extension)
        attachments: List[Dict] = body.get("attachments") or []

        # Only process if there's text (or handle attachments later)
        if not text and not attachments:
            return

        # If no text but has attachments, send a placeholder
        if not text:
            text = "[вложение]"

        recipient = message.get("recipient") or {}
        chat_id_raw = recipient.get("chat_id")
        chat_type_raw = recipient.get("chat_type") or "dialog"
        chat_type = "dm" if chat_type_raw == "dialog" else "group"

        # In MAX DM: recipient.chat_id is the bot's own user_id.
        # We must reply to the sender's user_id, not the bot's.
        # For groups: recipient.chat_id is the actual group chat id.
        if chat_type == "dm":
            chat_id = str(sender.get("user_id") or chat_id_raw or "")
        else:
            chat_id = str(chat_id_raw or "")

        if not chat_id:
            return

        # Remember this chat's type so send() can pick the right query param
        self._chat_types[chat_id] = chat_type

        user_id = str(sender.get("user_id") or "")
        user_name = sender.get("name") or sender.get("username") or user_id

        source = self.build_source(
            chat_id=chat_id,
            chat_name=chat_id,
            chat_type=chat_type,
            user_id=user_id,
            user_name=user_name,
        )

        unix_ms = message.get("timestamp")
        try:
            timestamp = (
                datetime.fromtimestamp(int(unix_ms) / 1000, tz=timezone.utc)
                if unix_ms else datetime.now(tz=timezone.utc)
            )
        except (ValueError, OSError, TypeError):
            timestamp = datetime.now(tz=timezone.utc)

        event = MessageEvent(
            text=text,
            message_type=MessageType.TEXT,
            source=source,
            message_id=msg_id,
            raw_message=update,
            timestamp=timestamp,
        )

        logger.debug("[max] Message from %s in %s: %s", user_name, chat_id, text[:80])
        
        # Intercept /model command to show inline keyboard
        if text == "/model" or text.startswith("/model "):
            await self._handle_model_command(chat_id, text)
            return
            
        await self.handle_message(event)

    # -- Deduplication ------------------------------------------------------

    def _is_duplicate(self, msg_id: str) -> bool:
        now = time.time()
        if len(self._seen) > DEDUP_MAX_SIZE:
            cutoff = now - DEDUP_WINDOW_SECONDS
            self._seen = {k: v for k, v in self._seen.items() if v > cutoff}
        if msg_id in self._seen:
            return True
        self._seen[msg_id] = now
        return False

    # -- Outbound messaging -------------------------------------------------

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Send a text message via POST /messages.

        MAX API routing:
          - DM to a user: POST /messages?user_id=<id>   (chat_id looks like a user_id)
          - Group chat:   POST /messages?chat_id=<id>

        We store the raw MAX chat_id in source.chat_id. For DMs it equals the
        user_id; for group chats it is a distinct chat id. The chat_type field
        on the source lets us pick the right query param.
        """
        if not self._http:
            return SendResult(success=False, error="HTTP client not initialized")

        text = _truncate(content)
        # Build body — format only, recipient goes in query params
        body: Dict[str, Any] = {"text": text}
        if self._markdown:
            body["format"] = "markdown"

        # Prefer metadata hint, then fall back to stored chat_type
        meta = metadata or {}
        chat_type = meta.get("chat_type") or self._chat_types.get(chat_id, "dm")
        if chat_type == "dm":
            params = {"user_id": chat_id}
        else:
            params = {"chat_id": chat_id}

        return await self._do_send(body, params)

    async def send_image(
        self,
        chat_id: str,
        image_url: str,
        caption: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Send an image by URL (MAX supports direct URL for images)."""
        if not self._http:
            return SendResult(success=False, error="HTTP client not initialized")

        chat_type = self._chat_types.get(chat_id, "dm")
        params = {"user_id": chat_id} if chat_type == "dm" else {"chat_id": chat_id}
        body: Dict[str, Any] = {
            "text": _truncate(caption or ""),
            "attachments": [{"type": "image", "payload": {"url": image_url}}],
        }
        if self._markdown and caption:
            body["format"] = "markdown"
        return await self._do_send(body, params)

    async def send_image_file(
        self,
        chat_id: str,
        image_path: str,
        caption: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Upload a local image file then send it."""
        token = await self._upload_file(image_path, file_type="image")
        if token is None:
            return SendResult(success=False, error="Upload failed")

        chat_type = self._chat_types.get(chat_id, "dm")
        params = {"user_id": chat_id} if chat_type == "dm" else {"chat_id": chat_id}
        body: Dict[str, Any] = {
            "text": _truncate(caption or ""),
            "attachments": [{"type": "image", "payload": {"token": token}}],
        }
        return await self._do_send(body, params)

    async def send_document(
        self,
        chat_id: str,
        file_path: str,
        caption: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Upload a local file and send it as a document."""
        token = await self._upload_file(file_path, file_type="file")
        if token is None:
            return SendResult(success=False, error="Upload failed")

        chat_type = self._chat_types.get(chat_id, "dm")
        params = {"user_id": chat_id} if chat_type == "dm" else {"chat_id": chat_id}
        body: Dict[str, Any] = {
            "text": _truncate(caption or ""),
            "attachments": [{"type": "file", "payload": {"token": token}}],
        }
        return await self._do_send(body, params)

    async def send_typing(self, chat_id: str, metadata=None) -> None:
        """MAX has no typing indicator API — no-op."""
        pass

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        """Return basic info about a MAX chat."""
        try:
            data = await self._api_get(f"/chats/{chat_id}")
            return {
                "name": data.get("title") or data.get("dialog_title") or chat_id,
                "type": "dm" if data.get("type") == "dialog" else "group",
                "chat_id": chat_id,
            }
        except Exception:
            return {"name": chat_id, "type": "dm", "chat_id": chat_id}

    # -- API helpers --------------------------------------------------------

    async def _api_get(self, path: str, **params) -> Dict[str, Any]:
        """Make an authenticated GET request to the MAX API."""
        resp = await self._http.get(
            f"{API_BASE}{path}",
            headers=_auth_headers(self._token),
            params=params or None,
            timeout=15.0,
        )
        resp.raise_for_status()
        return resp.json()

    async def _do_send(self, body: Dict[str, Any], params: Dict[str, Any]) -> SendResult:
        """POST /messages with body payload and query params (user_id or chat_id)."""
        try:
            resp = await self._http.post(
                f"{API_BASE}/messages",
                headers={**_auth_headers(self._token), "Content-Type": "application/json"},
                params=params,
                content=json.dumps(body).encode(),
                timeout=15.0,
            )
        except httpx.TimeoutException:
            return SendResult(success=False, error="Timeout sending message to MAX")
        except Exception as exc:
            logger.error("[max] send error: %s", exc)
            return SendResult(success=False, error=str(exc))

        if resp.status_code >= 300:
            err = f"HTTP {resp.status_code}: {resp.text[:200]}"
            logger.warning("[max] send failed: %s | params=%s", err, params)
            return SendResult(success=False, error=err)

        try:
            data = resp.json()
            msg_id = str(data.get("message_id") or uuid.uuid4().hex[:12])
        except Exception:
            msg_id = uuid.uuid4().hex[:12]

        return SendResult(success=True, message_id=msg_id)

    async def _upload_file(self, file_path: str, file_type: str = "file") -> Optional[str]:
        """Upload a file to MAX CDN and return the upload token.

        Two-step: POST /uploads?type=<type> → get upload URL →
        multipart POST to CDN → get token.
        """
        try:
            # Step 1: request an upload URL
            resp = await self._http.post(
                f"{API_BASE}/uploads",
                headers=_auth_headers(self._token),
                params={"type": file_type},
                timeout=15.0,
            )
            resp.raise_for_status()
            upload_info = resp.json()
            upload_url = upload_info.get("url")
            if not upload_url:
                logger.error("[max] No upload URL in response: %s", upload_info)
                return None

            # Step 2: multipart upload to CDN URL
            import mimetypes
            mime_type, _ = mimetypes.guess_type(file_path)
            mime_type = mime_type or "application/octet-stream"
            filename = os.path.basename(file_path)

            with open(file_path, "rb") as f:
                files = {"data": (filename, f, mime_type)}
                cdn_resp = await self._http.post(upload_url, files=files, timeout=60.0)
            cdn_resp.raise_for_status()
            token_data = cdn_resp.json()
            token = token_data.get("token")
            if not token:
                logger.error("[max] No token in CDN response: %s", token_data)
            return token

        except Exception as exc:
            logger.error("[max] File upload error for %s: %s", file_path, exc)
            return None


# ---------------------------------------------------------------------------
# Plugin hooks
# ---------------------------------------------------------------------------

def _env_enablement() -> Optional[Dict[str, Any]]:
    """Seed PlatformConfig.extra from env vars at gateway config load time."""
    token = os.getenv("MAX_BOT_TOKEN", "").strip()
    if not token:
        return None

    seed: Dict[str, Any] = {"token": token}

    markdown_env = os.getenv("MAX_MARKDOWN", "").strip().lower()
    seed["markdown"] = (markdown_env in ("1", "true", "yes")) if markdown_env else True

    poll_timeout = os.getenv("MAX_POLL_TIMEOUT", "").strip()
    if poll_timeout.isdigit():
        seed["poll_timeout"] = int(poll_timeout)

    home = os.getenv("MAX_HOME_CHANNEL", "").strip()
    if home:
        seed["home_channel"] = {
            "chat_id": home,
            "name": os.getenv("MAX_HOME_CHANNEL_NAME", home),
        }

    return seed


async def _standalone_send(
    pconfig: PlatformConfig,
    chat_id: str,
    message: str,
    *,
    thread_id: Optional[str] = None,
    media_files: Optional[List[str]] = None,
    force_document: bool = False,
) -> Dict[str, Any]:
    """Out-of-process send for cron / send_message_tool fallbacks."""
    if not HTTPX_AVAILABLE:
        return {"error": "max standalone send: httpx not installed"}

    # Ensure truststore is active in this process too
    if not _TRUSTSTORE_ACTIVE:
        try:
            import truststore as _ts
            _ts.inject_into_ssl()
        except Exception:
            pass

    extra = getattr(pconfig, "extra", {}) or {}
    token = extra.get("token") or _get_scoped_secret("MAX_BOT_TOKEN", "")
    if not token:
        return {"error": "max standalone send: MAX_BOT_TOKEN not configured"}

    markdown_env = os.getenv("MAX_MARKDOWN", "").strip().lower()
    use_markdown = extra.get("markdown", True) if "markdown" in extra else (
        (markdown_env in ("1", "true", "yes")) if markdown_env else True
    )

    text = _truncate(message)
    body: Dict[str, Any] = {"text": text}
    if use_markdown:
        body["format"] = "markdown"

    # Standalone send always assumes DM (no chat_type context available)
    params = {"user_id": chat_id}

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                f"{API_BASE}/messages",
                headers={**_auth_headers(token), "Content-Type": "application/json"},
                params=params,
                content=json.dumps(body).encode(),
            )
        if resp.status_code >= 300:
            return {"error": f"MAX HTTP {resp.status_code}: {resp.text[:200]}"}
        data = resp.json()
        msg_id = str(data.get("message_id") or uuid.uuid4().hex[:12])
        return {"success": True, "platform": "max", "chat_id": chat_id, "message_id": msg_id}
    except Exception as exc:
        return {"error": f"max standalone send failed: {exc}"}


def register(ctx) -> None:
    """Plugin entry point — called by the Hermes plugin system at startup."""
    ctx.register_platform(
        name="max",
        label="MAX",
        adapter_factory=lambda cfg: MaxAdapter(cfg),
        check_fn=check_requirements,
        validate_config=validate_config,
        is_connected=is_connected,
        required_env=["MAX_BOT_TOKEN"],
        install_hint="pip install httpx   # already a Hermes dependency",
        env_enablement_fn=_env_enablement,
        cron_deliver_env_var="MAX_HOME_CHANNEL",
        standalone_sender_fn=_standalone_send,
        allowed_users_env="MAX_ALLOWED_USERS",
        allow_all_env="MAX_ALLOW_ALL_USERS",
        max_message_length=MAX_MESSAGE_LENGTH,
        emoji="💬",
        pii_safe=False,
        allow_update_command=True,
        platform_hint=(
            "You are communicating via MAX messenger (max.ru). "
            "MAX supports Markdown formatting: **bold**, *italic*, "
            "`code`, ```code blocks```, [links](url), > quotes. "
            "Keep responses reasonably concise — MAX is a mobile-first messenger."
        ),
    )

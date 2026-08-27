# Model Selector — Preview

## How it looks in MAX

When you send `/model` to the bot, you get an interactive menu:

```
┌─────────────────────────────────────┐
│  Выберите модель:                   │
├─────────────────────────────────────┤
│  ┌──────────────┐  ┌──────────────┐ │
│  │ 🔥 GPT-5.5   │  │ 💬 GPT-4.5   │ │
│  └──────────────┘  └──────────────┘ │
│  ┌──────────────┐  ┌──────────────┐ │
│  │ ⚡ Claude     │  │ 💡 GPT-4o    │ │
│  │   Sonnet 4.5 │  │              │ │
│  └──────────────┘  └──────────────┘ │
│  ┌──────────────┐  ┌──────────────┐ │
│  │ 🎯 Claude     │  │ 🚀 GPT-4o    │ │
│  │   Opus 4     │  │   Mini       │ │
│  └──────────────┘  └──────────────┘ │
│  ┌──────────────┐                   │
│  │ ✨ Claude     │                   │
│  │   Haiku 4    │                   │
│  └──────────────┘                   │
│  ┌────────────────────────────────┐ │
│  │ 📋 Показать все модели         │ │
│  └────────────────────────────────┘ │
└─────────────────────────────────────┘
```

## How it works

1. **User types** `/model` (no arguments)
2. **Bot responds** with inline keyboard showing popular models
3. **User taps** any button (e.g., "🔥 GPT-5.5")
4. **Bot receives** `message_callback` event with `payload: "model:gpt-5.5"`
5. **Plugin creates** synthetic `/model gpt-5.5` message
6. **Hermes switches** the model and confirms the change

## Before vs After

### Before (text-only)
```
User: /model
Bot:  Current: gpt-5.5 on custom
      Available models (llm.v.devzone.su):
      gpt-5.5, gpt-4.5, claude-sonnet-4-5-20250929,
      gpt-4o, claude-opus-4, gpt-4o-mini, ...
      
      /model <name> — switch model

User: /model claude-sonnet-4-5-20250929  ← long to type!
Bot:  Switched to claude-sonnet-4-5-20250929
```

### After (inline keyboard)
```
User: /model
Bot:  [Shows interactive menu with buttons]

User: [Taps "⚡ Claude Sonnet 4.5"]  ← one tap!
Bot:  Switched to claude-sonnet-4-5
```

## Implementation

The plugin intercepts `/model` in `_on_message()` before it reaches Hermes:

```python
# If /model without args → show keyboard
if text == "/model":
    await self._handle_model_command(chat_id, text)
    return  # Don't forward to Hermes yet
```

When a button is tapped, MAX sends a `message_callback` update:

```json
{
  "update_type": "message_callback",
  "callback": {
    "payload": "model:gpt-5.5"
  },
  "message": { ... }
}
```

The `_on_callback()` handler converts it into a synthetic message:

```python
if payload.startswith("model:"):
    model_name = payload.split(":", 1)[1]
    # Create synthetic MessageEvent with text="/model gpt-5.5"
    await self.handle_message(event)
```

This way, Hermes receives a normal `/model <name>` command and processes it through the standard flow.

## Customization

To change the model list, edit `_handle_model_command()` in `adapter.py`:

```python
models = {
    "gpt-5.5": "🔥 GPT-5.5",
    "gpt-4.5": "💬 GPT-4.5",
    "claude-sonnet-4-5": "⚡ Claude Sonnet 4.5",
    # Add your own models here
}
```

Buttons are arranged 2 per row for better mobile UX.

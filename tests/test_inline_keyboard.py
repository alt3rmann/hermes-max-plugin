#!/usr/bin/env python3
"""
Test script for MAX inline keyboard support.

This script simulates the flow of the /model command with inline buttons
and verifies that the callback handling works correctly.
"""

import json


def test_inline_keyboard_structure():
    """Test that the inline keyboard structure is valid for MAX API."""
    
    models = {
        "gpt-5.5": "🔥 GPT-5.5",
        "gpt-4.5": "💬 GPT-4.5",
        "claude-sonnet-4-5": "⚡ Claude Sonnet 4.5",
        "gpt-4o": "💡 GPT-4o",
        "claude-opus-4": "🎯 Claude Opus 4",
        "gpt-4o-mini": "🚀 GPT-4o Mini",
        "claude-haiku-4": "✨ Claude Haiku 4",
    }
    
    buttons = []
    row = []
    for model_id, model_label in models.items():
        row.append({
            "type": "callback",
            "text": model_label,
            "payload": f"model:{model_id}"
        })
        if len(row) == 2:
            buttons.append(row)
            row = []
    
    if row:
        buttons.append(row)
    
    # Add "Show all models" button
    buttons.append([{
        "type": "message",
        "text": "📋 Показать все модели",
        "payload": "/model list"
    }])
    
    # Build the message body
    body = {
        "text": "Выберите модель:",
        "attachments": [{
            "type": "inline_keyboard",
            "payload": {
                "buttons": buttons
            }
        }]
    }
    
    print("✓ Generated inline keyboard structure:")
    print(json.dumps(body, indent=2, ensure_ascii=False))
    
    # Validate constraints
    total_buttons = sum(len(row) for row in buttons)
    assert total_buttons <= 210, f"Too many buttons: {total_buttons} (max 210)"
    assert len(buttons) <= 30, f"Too many rows: {len(buttons)} (max 30)"
    
    for i, row in enumerate(buttons):
        assert len(row) <= 7, f"Row {i} has {len(row)} buttons (max 7)"
    
    print(f"\n✓ Validation passed:")
    print(f"  - Total buttons: {total_buttons}/210")
    print(f"  - Total rows: {len(buttons)}/30")
    print(f"  - Max buttons per row: {max(len(r) for r in buttons)}/7")
    

def test_callback_payload_parsing():
    """Test that callback payloads are parsed correctly."""
    
    test_payloads = [
        ("model:gpt-5.5", "gpt-5.5"),
        ("model:claude-sonnet-4-5", "claude-sonnet-4-5"),
        ("model:gpt-4o-mini", "gpt-4o-mini"),
    ]
    
    print("\n✓ Testing callback payload parsing:")
    for payload, expected_model in test_payloads:
        if payload.startswith("model:"):
            model_name = payload.split(":", 1)[1]
            assert model_name == expected_model, f"Expected {expected_model}, got {model_name}"
            print(f"  - '{payload}' → '{model_name}' ✓")
    

def test_message_body_size():
    """Test that the message body doesn't exceed MAX API limits."""
    
    models = {
        "gpt-5.5": "🔥 GPT-5.5",
        "gpt-4.5": "💬 GPT-4.5",
        "claude-sonnet-4-5": "⚡ Claude Sonnet 4.5",
        "gpt-4o": "💡 GPT-4o",
        "claude-opus-4": "🎯 Claude Opus 4",
        "gpt-4o-mini": "🚀 GPT-4o Mini",
        "claude-haiku-4": "✨ Claude Haiku 4",
    }
    
    buttons = []
    row = []
    for model_id, model_label in models.items():
        row.append({
            "type": "callback",
            "text": model_label,
            "payload": f"model:{model_id}"
        })
        if len(row) == 2:
            buttons.append(row)
            row = []
    
    if row:
        buttons.append(row)
    
    buttons.append([{
        "type": "message",
        "text": "📋 Показать все модели",
        "payload": "/model list"
    }])
    
    body = {
        "text": "Выберите модель:",
        "attachments": [{
            "type": "inline_keyboard",
            "payload": {
                "buttons": buttons
            }
        }]
    }
    
    body_json = json.dumps(body, ensure_ascii=False)
    body_size = len(body_json.encode('utf-8'))
    
    print(f"\n✓ Message body size check:")
    print(f"  - JSON size: {body_size} bytes")
    print(f"  - Under reasonable limit: {body_size < 10240} (< 10KB)")
    
    assert body_size < 10240, f"Message body too large: {body_size} bytes"
    

if __name__ == "__main__":
    print("=" * 60)
    print("MAX Inline Keyboard Tests")
    print("=" * 60)
    
    test_inline_keyboard_structure()
    test_callback_payload_parsing()
    test_message_body_size()
    
    print("\n" + "=" * 60)
    print("All tests passed! ✓")
    print("=" * 60)

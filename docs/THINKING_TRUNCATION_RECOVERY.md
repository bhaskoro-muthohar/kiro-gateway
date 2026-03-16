# Thinking Block Truncation Detection & Recovery

## Problem

When the Kiro API truncates a stream while the model is inside a `<thinking>` block, the truncation goes **undetected**. The client receives no visible content (thinking is stripped in most modes), and the existing truncation recovery never activates. This causes intermittent "stopped midway" behavior where the model appears to silently fail.

**Root cause**: Truncation detection only checked `full_content` (visible text). When the entire response was thinking, `full_content` is empty, so the truncation condition evaluated to `False`.

**Additional challenge**: Existing content truncation recovery uses hash-based matching on assistant message text. For thinking truncation the assistant message has no text (thinking was stripped), so hash matching can't work. A flag-based approach is used instead.

## Solution

### Detection (`streaming_anthropic.py`, `streaming_openai.py`)

A new `thinking_was_truncated` flag is computed alongside the existing `content_was_truncated`:

```python
thinking_was_truncated = (
    not stream_completed_normally and
    len(full_thinking_content) > 0 and
    len(full_content) == 0 and
    not tool_blocks
)
```

This fires when:
- The stream did **not** complete normally (no `context_usage_percentage` received)
- There **was** thinking content accumulated
- There was **no** visible content (thinking-only response)
- There were **no** tool calls

An ERROR-level log is emitted when this is detected.

### Immediate Visible Notice (in-stream)

When thinking truncation is detected, a **visible text block** is injected directly into the stream response so the client/user immediately sees what happened instead of receiving a silent empty response:

- **Anthropic adapter**: Emits `content_block_start` → `content_block_delta` (with notice text) → `content_block_stop`
- **OpenAI adapter**: Emits a `chat.completion.chunk` with `delta.content` containing the notice text

This ensures the user sees the `[System Notice]` in their chat UI rather than a blank response.

### State Tracking (`truncation_state.py`)

A simple boolean flag (no hash needed since the assistant message is empty):

- `save_thinking_truncation()` — sets the flag
- `get_thinking_truncation()` — returns and clears the flag (one-time retrieval)

The flag is also included in `get_cache_stats()`.

### Recovery Message (`truncation_recovery.py`)

A new `generate_thinking_truncation_user_message()` generates:

```
[System Notice] Your previous response was truncated by the API while you were
still reasoning. No visible output was delivered to the user.
This is not an error on your part. Please provide your response again.
```

### Injection (`routes_anthropic.py`, `routes_openai.py`)

On the next request, if the thinking truncation flag is set, a synthetic user message is inserted **before** the last user message in the conversation. This informs the model that its previous reasoning was lost and it should respond again.

## Affected Files

| File | Change |
|------|--------|
| `kiro/streaming_anthropic.py` | `thinking_was_truncated` detection, logging, visible notice injection, recovery save |
| `kiro/streaming_openai.py` | Same detection pattern + visible notice injection |
| `kiro/truncation_state.py` | `save_thinking_truncation()`, `get_thinking_truncation()` |
| `kiro/truncation_recovery.py` | `generate_thinking_truncation_user_message()` |
| `kiro/routes_anthropic.py` | Inject thinking truncation recovery message |
| `kiro/routes_openai.py` | Same injection |

## Configuration

This feature uses the existing `TRUNCATION_RECOVERY` config flag (`.env`). When enabled, thinking truncation is automatically detected and the model is notified on the next request.

## Verification

- `pytest tests/unit/ -v` — all existing + new tests pass (8 new tests added)
- When a thinking-only response is truncated:
  1. An `ERROR` log appears with `thinking_length=N chars`
  2. The client immediately sees a `[System Notice]` text in the response (visible to user)
  3. On the next request, a synthetic user message is also injected into the conversation history so the model knows what happened

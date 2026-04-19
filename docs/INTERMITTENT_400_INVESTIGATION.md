# Intermittent 400 "Improperly formed request" Investigation

## Status: RESOLVED - Payload limit raised to ~4MB (April 2026)

### Update (April 2026)

The Kiro API payload size limit has been raised from ~615KB to ~4MB (exact boundary:
4,047,768 bytes pass, 4,048,790 bytes fail, approximately 3,953KB). The error message
has also improved from the cryptic `"Improperly formed request."` / `reason: null` to
`"Input is too long."` / `reason: CONTENT_LENGTH_EXCEEDS_THRESHOLD`.

Gateway defaults updated: `KIRO_MAX_PAYLOAD_BYTES=3800000`, `AUTO_TRIM_PAYLOAD=false`.

The original investigation below is preserved for historical context.

---

## Original Investigation (January 2026)

## Root Cause

The Kiro API (`q.us-east-1.amazonaws.com/generateAssistantResponse`) rejects requests where the JSON payload exceeds approximately **615KB** (629,504 bytes OK, 629,760 bytes FAIL).

The error message `"Improperly formed request."` with `"reason": null` or `"reason": "UNKNOWN"` is misleading — it's actually a size limit, not a structural issue.

### Why It Appears Intermittent

As Claude Code conversations grow (more tool calls, more context), the Kiro payload grows. Once it crosses ~615KB, every request fails. It appears intermittent because:
- Short conversations stay under the limit
- Long conversations with many tool calls cross it
- After `/compact` (context compression), the payload shrinks back under the limit

### Verification

Binary search confirmed the exact boundary:

```
629,504 bytes (614.75 KB) → HTTP 200 OK
629,760 bytes (615.00 KB) → HTTP 400 FAIL
```

Tested by padding a known-good payload with dummy content. The limit is consistent and deterministic — not timing or concurrency related.

### Original Failing Payload

The captured `kiro_request_body.json` was **627,534 bytes** — right at the limit. It failed 5/5 replays.

## Fix Options

### Option A: Kiro Payload Byte-Size Truncation (Recommended)
After building the Kiro payload in `build_kiro_payload()`, measure serialized JSON size. If over ~590KB, trim oldest history entry pairs until it fits.

**Where:** `kiro/converters_core.py`, after payload assembly (~line 1516)

**Pros:** Directly fixes the confirmed root cause. Works at the right layer (Kiro payload, not Anthropic messages).
**Cons:** Dumb removal — loses early context. But Claude Code's `/compact` should usually prevent reaching this point; this is a safety net.

### Option B: Token-Based Trim at Anthropic Layer (VPS approach)
The VPS already has `kiro/trim_context.py` — trims oldest Anthropic messages to 75% of model context window before conversion.

**Where:** `kiro/routes_anthropic.py`, before `anthropic_to_kiro()` call

**Pros:** Smarter — works at message level, preserves alternating roles.
**Cons:** Does NOT directly fix the 615KB issue. Token count ≠ byte size. A conversation within token limits can still exceed 615KB because tool schemas, system prompt, and verbose JSON add bytes without proportional tokens.

### Option C: Both (Belt + Suspenders)
Port VPS token trim + add byte-size check. Token trim prevents most overflows; byte-size check catches edge cases.

**Recommendation:** Option C is safest. Token trim handles most cases gracefully; byte-size trim is the safety net for the 615KB hard limit. But Option A alone is sufficient if you want minimal changes.

### Upstream Status
- Issue filed: https://github.com/jwadow/kiro-gateway/issues/73
- Repo owner's stance (from issue #60): "gateway is transparent, client's problem"
- We pushed back arguing this is a Kiro shortcoming (like other quirks the gateway already handles)
- Don't count on upstream fixing this

### VPS Existing Implementation
The VPS (`/root/kiro-gateway/`) has uncommitted changes with:
- `kiro/trim_context.py` — token-based trim to 75% of context window
- `kiro/merge_messages.py` — merges consecutive same-role messages
- Modified `kiro/routes_anthropic.py` — calls trim before conversion, saves failed requests to `/tmp/kiro-failed-request.json`
- Modified `kiro/models_anthropic.py` — relaxed `ToolResultContentBlock.content` type to `Any`
- Modified `kiro/converters_anthropic.py` — extracts images from inside tool_result blocks

**Note:** The VPS token trim does NOT fix the 615KB byte-size limit. Both are needed.

## Changes Made During Investigation (To Review)

### 1. Inflight Counter (can revert)
Added `kiro_inflight` counter to `main.py`, `routes_anthropic.py`, `routes_openai.py` to track concurrent requests. This **disproved** the concurrency hypothesis but is no longer needed.

**Files changed:**
- `main.py` — added `app.state.kiro_inflight = 0`
- `kiro/routes_anthropic.py` — increment/decrement around Kiro API call, log `inflight_at_send` on errors
- `kiro/routes_openai.py` — same

**Recommendation:** Revert. It adds complexity for no ongoing benefit. The root cause is confirmed as payload size, not concurrency.

### 2. DEBUG_MODE change (keep)
Changed `.env` from `DEBUG_MODE="all"` to `DEBUG_MODE="errors"`. This preserves error data across subsequent successful requests.

**Recommendation:** Keep. Useful for future debugging.

## Investigation Timeline

### First Root Cause (FIXED - commit dd2a487)
- Assistant-first messages after context compression
- Fixed by `ensure_first_message_is_user()`

### Second Root Cause (THIS - payload size limit)
- Kiro API rejects payloads > ~615KB with generic "Improperly formed request"
- Confirmed via binary search: padding a working payload past 615KB triggers the error
- The concurrency hypothesis (H1) was disproven: `inflight_at_send=1` on captured error

## Key Evidence

| Test | Result |
|------|--------|
| Original payload (627KB) | FAIL 5/5 |
| No history + currentMessage (small) | OK |
| Padded to 600KB | OK |
| Padded to 625KB | FAIL |
| Full payload, strip all toolUses+toolResults (402KB) | OK |
| Full payload, strip only toolResults (456KB) | FAIL (orphaned toolUses) |

## Files Involved

| File | Role |
|------|------|
| `kiro/converters_core.py` | Builds Kiro payload — needs history truncation to stay under 615KB |
| `kiro/routes_anthropic.py` | Anthropic-compatible endpoint |
| `kiro/routes_openai.py` | OpenAI-compatible endpoint |
| `main.py` | App startup, `app.state` initialization |

## Debug Data Location

Captured error data in `debug_logs/`:
- `error_info.json` - Confirms 400 status
- `app_logs.txt` - Request processing logs
- `request_body.json` - Original Anthropic request (~766KB)
- `kiro_request_body.json` - Converted Kiro payload (~627KB, over the ~615KB limit)
- `/tmp/kiro-gateway.log` - Full gateway stdout

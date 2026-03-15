# Claude Code Remote Control with Kiro Gateway

## Problem

When using Claude Code through Kiro Gateway (via `ANTHROPIC_BASE_URL`), the `/remote-control`
slash command shows `Unknown skill: remote-control`, even though it works fine with the stock
`claude` binary connecting directly to Anthropic.

## Root Cause Analysis

### The Feature Flag Chain

The `/remote-control` slash command is gated behind a GrowthBook feature flag. The full chain:

```
/remote-control command registration
  → isEnabled: H39()
  → H39() = Nl()
  → Nl() = Oq("tengu_ccr_bridge", false)   ← feature flag with default=false
  → Oq() checks:
      1. yZ_() local overrides → always null (dead end)
      2. Ai() → u1_() → !Zv()              ← "is telemetry/flags enabled?"
      3. DT().cachedGrowthBookFeatures["tengu_ccr_bridge"]  ← server-side evaluation
```

### Why It Fails with a Proxy

The `getApiBaseUrlHost()` function extracts the host from `ANTHROPIC_BASE_URL`:

```js
function vw9() {
  let _ = process.env.ANTHROPIC_BASE_URL;
  if (!_) return;                          // undefined → standard API
  let T = new URL(_).host;
  if (T === "api.anthropic.com") return;   // also undefined
  return T;                                // "localhost:8888" for proxy
}
```

This host is sent as a GrowthBook targeting attribute. Anthropic's server-side targeting for
`tengu_ccr_bridge` only returns `true` when `api_base_url_host` is undefined (standard API).
With `localhost:8888`, the flag evaluates to `false` → command is hidden → "Unknown skill".

### Why `--remote-control` Flag Works

The `--remote-control` CLI startup flag takes a completely separate code path that bypasses
the GrowthBook feature flag check. It directly initializes the remote control bridge without
consulting `isEnabled`.

### The `Zv()` Gate

The feature flag system is disabled entirely when any of these env vars are set:

```
CLAUDE_CODE_USE_BEDROCK
CLAUDE_CODE_USE_VERTEX
CLAUDE_CODE_USE_FOUNDRY
DISABLE_TELEMETRY
CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC
```

When disabled, all flags return their defaults → `tengu_ccr_bridge` defaults to `false`.

### Auth Conflict

Even with `--remote-control`, there's an auth conflict:

- With `ANTHROPIC_API_KEY` set: CLI uses API key auth → remote control disabled
  (RC requires OAuth/claude.ai authentication)
- Without `ANTHROPIC_API_KEY`: CLI uses OAuth → remote control works, but gateway
  rejects the request (OAuth token != `PROXY_API_KEY`)

## Solution: Localhost Auth Bypass

Added localhost bypass to the gateway's auth verification functions. Connections from
`127.0.0.1` or `::1` are accepted without API key validation.

### Config

```env
# .env
ALLOW_LOCALHOST_BYPASS=true   # default: true
```

### Files Changed

- `kiro/config.py` — `ALLOW_LOCALHOST_BYPASS` env var
- `kiro/routes_anthropic.py` — `verify_anthropic_api_key()` accepts localhost
- `kiro/routes_openai.py` — `verify_api_key()` accepts localhost

### Auth Flow (After Fix)

```
Claude Code (OAuth) → localhost:8888 → Gateway accepts (localhost bypass) → Kiro API
```

Remote control connects to `claude.ai/code/session_*` directly (not through the gateway),
so it works with OAuth auth.

## Recommended Aliases

```bash
# OAuth mode — supports /remote-control mid-session
alias claude-kiro="ANTHROPIC_BASE_URL=http://localhost:8888 claude"

# OAuth + remote-control from startup
alias claude-kiro-rc="ANTHROPIC_BASE_URL=http://localhost:8888 claude --remote-control"

# API key mode — legacy, no remote-control support
alias claude-kiro-direct="ANTHROPIC_BASE_URL=http://localhost:8888 ANTHROPIC_API_KEY=kiro-test-gateway-2025 claude"
```

### When to Use Which

| Alias | Auth | Remote Control | `/remote-control` mid-session |
|-------|------|---------------|-------------------------------|
| `claude-kiro` | OAuth | No (until you type `/remote-control`) | Yes |
| `claude-kiro-rc` | OAuth | Yes (from startup) | Yes |
| `claude-kiro-direct` | API Key | No | No |

## Key Takeaway

The `/remote-control` slash command availability is a **server-side decision by Anthropic**
via GrowthBook feature flags. It cannot be overridden locally. The workaround is to use OAuth
auth (no `ANTHROPIC_API_KEY`) combined with the gateway's localhost bypass, which allows the
CLI to authenticate with claude.ai while still routing API calls through the local proxy.

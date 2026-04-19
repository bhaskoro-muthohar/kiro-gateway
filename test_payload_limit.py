#!/usr/bin/env python3
"""
Test script to probe the Kiro API payload size limit.

Sends progressively larger payloads to the Kiro API (bypassing the gateway)
to determine the exact boundary where requests start failing.

The known limit was ~615KB as of Jan 2025, raised to ~4MB as of April 2026. This script tests
been raised.

Usage:
    python test_payload_limit.py

Requirements:
    - Working .env with REFRESH_TOKEN + PROFILE_ARN (or KIRO_CREDS_FILE / KIRO_CLI_DB_FILE)
    - pip install httpx python-dotenv loguru
"""

import asyncio
import json
import sys
import time
import uuid
from pathlib import Path

# Add project root to path so we can import kiro modules
sys.path.insert(0, str(Path(__file__).parent))

from kiro.auth import KiroAuthManager
from kiro.config import (
    REFRESH_TOKEN,
    PROFILE_ARN,
    REGION,
    KIRO_CREDS_FILE,
    KIRO_CLI_DB_FILE,
    VPN_PROXY_URL,
)
from kiro.utils import get_kiro_headers

import httpx


# Target payload sizes to test (in bytes)
TEST_SIZES_KB = [3952, 3953, 3954, 3955]


def build_minimal_payload(profile_arn: str, model_id: str = "claude-opus-4.6") -> dict:
    """Build the smallest valid Kiro payload (just a simple prompt, no history)."""
    payload = {
        "conversationState": {
            "chatTriggerType": "MANUAL",
            "conversationId": str(uuid.uuid4()),
            "currentMessage": {
                "userInputMessage": {
                    "content": "Say hello in one word.",
                    "modelId": model_id,
                    "origin": "AI_EDITOR",
                }
            },
        },
    }
    if profile_arn:
        payload["profileArn"] = profile_arn
    return payload


def measure_payload_bytes(payload: dict) -> int:
    """Return the UTF-8 byte size of the JSON-serialized payload."""
    return len(json.dumps(payload, separators=(",", ":")).encode("utf-8"))


def build_padded_payload(
    target_bytes: int,
    profile_arn: str,
    model_id: str = "claude-opus-4.6",
) -> dict:
    """
    Build a valid Kiro payload padded to approximately target_bytes.

    Padding is done by adding large user messages in the conversation history.
    Each history entry is a user/assistant pair to maintain alternation.
    """
    payload = {
        "conversationState": {
            "chatTriggerType": "MANUAL",
            "conversationId": str(uuid.uuid4()),
            "currentMessage": {
                "userInputMessage": {
                    "content": "Say hello in one word.",
                    "modelId": model_id,
                    "origin": "AI_EDITOR",
                }
            },
            "history": [],
        },
    }
    if profile_arn:
        payload["profileArn"] = profile_arn

    current_size = measure_payload_bytes(payload)

    # Each padding pair: one user message with filler text + one assistant response
    # We'll add pairs until we reach the target size
    pair_index = 0
    while current_size < target_bytes:
        remaining = target_bytes - current_size
        # Leave some room for JSON overhead (~200 bytes per pair)
        filler_size = min(remaining - 200, 50000)  # Cap individual messages at 50KB
        if filler_size <= 0:
            break

        # Use repeating ASCII text as filler (realistic-ish content)
        filler_unit = f"This is padding message {pair_index}. The quick brown fox jumps over the lazy dog. "
        repetitions = max(1, filler_size // len(filler_unit))
        filler_text = (filler_unit * repetitions)[:filler_size]

        # Add user message
        payload["conversationState"]["history"].append({
            "userInputMessage": {
                "content": filler_text,
                "modelId": model_id,
                "origin": "AI_EDITOR",
            }
        })

        # Add assistant response (short)
        payload["conversationState"]["history"].append({
            "assistantResponseMessage": {
                "content": f"Acknowledged message {pair_index}."
            }
        })

        current_size = measure_payload_bytes(payload)
        pair_index += 1

    return payload


def create_auth_manager() -> KiroAuthManager:
    """Create a KiroAuthManager using the same credential sources as the gateway."""
    if KIRO_CLI_DB_FILE:
        print(f"  Auth source: SQLite database ({KIRO_CLI_DB_FILE})")
        return KiroAuthManager(
            sqlite_db=KIRO_CLI_DB_FILE,
            profile_arn=PROFILE_ARN or None,
            region=REGION,
        )
    elif KIRO_CREDS_FILE:
        print(f"  Auth source: Credentials file ({KIRO_CREDS_FILE})")
        return KiroAuthManager(
            creds_file=KIRO_CREDS_FILE,
            profile_arn=PROFILE_ARN or None,
            region=REGION,
        )
    elif REFRESH_TOKEN:
        print(f"  Auth source: .env REFRESH_TOKEN")
        return KiroAuthManager(
            refresh_token=REFRESH_TOKEN,
            profile_arn=PROFILE_ARN or None,
            region=REGION,
        )
    else:
        print("ERROR: No credentials found. Set REFRESH_TOKEN, KIRO_CREDS_FILE, or KIRO_CLI_DB_FILE in .env")
        sys.exit(1)


async def send_payload(
    client: httpx.AsyncClient,
    url: str,
    payload: dict,
    headers: dict,
) -> tuple[int, str]:
    """
    Send a payload to the Kiro API and return (status_code, brief_response).

    Uses streaming to match how the gateway sends requests, but we only
    care about the initial HTTP status code.
    """
    req = client.build_request("POST", url, json=payload, headers=headers)
    response = await client.send(req, stream=True)

    status = response.status_code

    # Read a small chunk to get any error message
    body_preview = ""
    try:
        chunk = await response.aread()
        # For binary AWS Event Stream, just note the size
        if status == 200:
            body_preview = f"stream started ({len(chunk)} bytes received)"
        else:
            # Error responses are usually JSON or text
            body_preview = chunk.decode("utf-8", errors="replace")[:500]
    except Exception as e:
        body_preview = f"(read error: {e})"
    finally:
        await response.aclose()

    return status, body_preview


async def run_test():
    """Main test runner."""
    print("=" * 70)
    print("Kiro API Payload Size Limit Test")
    print("=" * 70)
    print()

    # Initialize auth
    print("[1/3] Initializing authentication...")
    auth_manager = create_auth_manager()
    print(f"  Region: {REGION}")
    print(f"  API host: {auth_manager.api_host}")
    print(f"  Profile ARN: {auth_manager.profile_arn or '(not set)'}")
    print()

    # Get access token
    print("[2/3] Obtaining access token...")
    try:
        token = await auth_manager.get_access_token()
        print(f"  Token obtained (first 20 chars): {token[:20]}...")
    except Exception as e:
        print(f"  ERROR: Failed to get access token: {e}")
        sys.exit(1)
    print()

    # Build URL
    url = f"{auth_manager.api_host}/generateAssistantResponse"
    print(f"  Target URL: {url}")
    print()

    # Sanity check: send minimal payload first
    print("[3/3] Running payload size tests...")
    print()
    print(f"{'Size (KB)':>10} | {'Actual (bytes)':>14} | {'Status':>6} | Response")
    print("-" * 80)

    # Configure HTTP client
    transport_kwargs = {}
    if VPN_PROXY_URL:
        print(f"  Using proxy: {VPN_PROXY_URL}")
        transport_kwargs["proxy"] = VPN_PROXY_URL

    timeout = httpx.Timeout(connect=30.0, read=60.0, write=30.0, pool=30.0)

    async with httpx.AsyncClient(timeout=timeout, **transport_kwargs) as client:
        # First: sanity check with minimal payload
        minimal = build_minimal_payload(auth_manager.profile_arn)
        minimal_size = measure_payload_bytes(minimal)
        headers = get_kiro_headers(auth_manager, token)
        headers["Connection"] = "close"

        status, body = await send_payload(client, url, minimal, headers)
        print(f"{'minimal':>10} | {minimal_size:>14,} | {status:>6} | {body[:60]}")

        if status != 200:
            print()
            print(f"FATAL: Minimal payload failed with status {status}.")
            print(f"  Response: {body}")
            print()
            print("This likely means authentication is broken. Fix credentials first.")
            sys.exit(1)

        print()

        # Now test each target size
        results = []
        last_success_kb = 0
        first_failure_kb = None

        for target_kb in TEST_SIZES_KB:
            target_bytes = target_kb * 1024

            # Build padded payload
            payload = build_padded_payload(target_bytes, auth_manager.profile_arn)
            actual_bytes = measure_payload_bytes(payload)

            # Get fresh headers (new invocation ID)
            headers = get_kiro_headers(auth_manager, token)
            headers["Connection"] = "close"

            # Send request
            try:
                status, body = await send_payload(client, url, payload, headers)
            except httpx.TimeoutException as e:
                status = 0
                body = f"TIMEOUT: {e}"
            except Exception as e:
                status = -1
                body = f"ERROR: {e}"

            # Determine result
            if status == 200:
                result_str = "OK"
                last_success_kb = target_kb
            else:
                result_str = "FAIL"
                if first_failure_kb is None:
                    first_failure_kb = target_kb

            results.append({
                "target_kb": target_kb,
                "actual_bytes": actual_bytes,
                "status": status,
                "result": result_str,
                "body_preview": body[:60],
            })

            print(f"{target_kb:>7} KB | {actual_bytes:>14,} | {status:>6} | {body[:60]}")

            # Small delay between requests to avoid rate limiting
            await asyncio.sleep(2)

    # Summary
    print()
    print("=" * 70)
    print("SUMMARY")
    print("=" * 70)
    print()
    print(f"  Last successful size:  {last_success_kb} KB")
    if first_failure_kb:
        print(f"  First failure at:      {first_failure_kb} KB")
        print(f"  Estimated limit:       between {last_success_kb} KB and {first_failure_kb} KB")
    else:
        print(f"  No failures detected!  Limit may have been raised above {TEST_SIZES_KB[-1]} KB")
    print()

    # Detailed results
    print("Detailed results:")
    for r in results:
        marker = "PASS" if r["status"] == 200 else "FAIL"
        print(f"  [{marker}] {r['target_kb']:>7} KB ({r['actual_bytes']:>10,} bytes) -> HTTP {r['status']}")

    print()
    print("Done.")


if __name__ == "__main__":
    asyncio.run(run_test())

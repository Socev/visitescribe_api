# Connecting the recorder

What the Raspberry Pi needs, and what it does not.

## Nothing has to change in the v0.2 client

The wire contract is exactly what v0.2 already sends. If the device is
registered without a token, this sequence works unmodified:

```
POST /v1/sessions
PUT  /v1/sessions/{id}/chunks/{sequence}   (for each not-yet-uploaded chunk)
POST /v1/sessions/{id}/events
POST /v1/sessions/{id}/complete
GET  /v1/sessions/{id}/status
```

Every write returns `2xx` for `raise_for_status()`, and ingest is confirmed when
the response carries `"ingest_confirmed": true` or a `state` in
`INGESTED · READY_FOR_PROCESSING · TRANSCRIBING · PROCESSING · REVIEW_REQUIRED ·
APPROVED`.

## The two things to provision

**1. The server public key.** Fetch it once from
`GET /v1/server/public-key`, or copy it from the admin **Keys** page:

```json
{ "key_id": "srv-…", "algorithm": "RSA-OAEP-SHA256",
  "public_key_pem": "-----BEGIN PUBLIC KEY-----\n…",
  "wrap": { "scheme": "RSA-OAEP", "hash": "SHA-256",
            "mgf1": "SHA-256", "label": null, "plaintext_bytes": 32 } }
```

Wrap the 32-byte session key with it and put the base64 in
`encryption.server_key_wrap.ciphertext_b64`. When the server key is rotated,
sessions already wrapped with the old key keep working.

**2. The device must be registered.** In the admin interface either create the
device directly, or open an enrolment window for its ID and let it register
itself on its next request. An unknown device ID is always rejected.

## Adding the token (recommended)

One header, once you issue a token on the device page:

```python
session.headers["Authorization"] = f"Bearer {DEVICE_TOKEN}"
```

`X-Device-Token: <token>` works identically if a bearer header is inconvenient.
Nothing else changes.

## Details that matter

**The AAD is used byte for byte.** The server does
`X-Chunk-AAD.encode("utf-8")` and nothing else — no trimming, no normalising, no
JSON round-trip. Send the exact string that was authenticated at encryption time
or the GCM tag will not verify.

**The manifest is the source of truth.** `ciphertext_sha256` and
`plaintext_sha256` in the manifest are checked against every upload. Write the
manifest after the chunk is encrypted, not before.

**Re-wrapping the key on a retry is fine.** RSA-OAEP padding is randomised, so a
re-sent `POST /v1/sessions` legitimately carries different bytes. Session
identity is judged on meaning — including the unwrapped session key — not on the
exact bytes, so a re-wrap is not a conflict. Wrapping a *different* key for the
same session still is.

**Retry anything.** Every write is idempotent. A request the server processed but
whose response was lost can be re-sent safely; a chunk already stored returns
`200` with `"duplicate": true`.

**`complete` is re-evaluated every time,** never replayed from a cache. The
normal recovery flow — complete → learn a chunk is missing → upload it →
complete again — reports the new state on the second call. Uploading the last
missing chunk also confirms the session on its own, without another `complete`.

**Only after confirmation.** Delete the local copy only once
`ingest_confirmed` is `true` or the state is one of the durable states above.
A `200` on the chunk upload alone is not that promise.

## Verifying a deployment

```bash
python tools/simulate_recorder.py \
    --base-url https://scribe.primumnonnocere.olares.com \
    --device visitescribe-001 --token "$DEVICE_TOKEN" \
    --chunks 4 --mode multi_patient --skip-chunk 3 --retry-everything
```

Exits `0` only if every call succeeded and the session ended confirmed.

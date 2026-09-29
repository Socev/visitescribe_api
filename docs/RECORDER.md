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

**The AAD is used byte for byte.** The server takes it from the raw request
headers and passes it to AES-GCM unchanged — no trimming, no normalising, no
JSON round-trip, no re-encoding. Send the exact bytes that were authenticated at
encryption time or the GCM tag will not verify. Non-ASCII AADs are fine; send
them UTF-8 encoded.

**Every chunk in a session needs its own nonce.** A repeated nonce under one
session key is a total break of AES-GCM, so the server refuses the upload with
`NONCE_REUSE` — both when the manifest declares a duplicate and when the second
chunk arrives. Generate 12 random bytes per chunk, or use a counter that cannot
restart. If you ever see this error, the recorder's RNG or counter is broken and
the audio already uploaded for that session should be treated as compromised.

**The manifest is the source of truth.** `ciphertext_sha256` and
`plaintext_sha256` in the manifest are checked against every upload. Write the
manifest after the chunk is encrypted, not before.

**Re-wrapping the key on a retry is fine.** RSA-OAEP padding is randomised, so a
re-sent `POST /v1/sessions` legitimately carries different bytes. Session
identity is judged on meaning — including the unwrapped session key — not on the
exact bytes, so a re-wrap is not a conflict. Wrapping a *different* key for the
same session still is.

**Uploads can be paused.** An administrator can pause a device without
disabling it. `POST /v1/sessions` and chunk uploads then return `403
DEVICE_UPLOADS_PAUSED`, while `GET /v1/device/config` keeps working and reports
`"upload_enabled": false` — so the recorder can learn why and back off instead
of hammering.

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

## Compact uploads: Ogg/Opus sessions (since 1.6.0)

The recorder keeps its lossless 48 kHz stereo master on the SD card. The copy
that goes over the wire only has to be good speech, so a session may be
declared `codec: "opus"` and carry Ogg/Opus chunks: ~24 kbit/s instead of
256 kbit/s for PCM16 mono, roughly 90 kB per 30 s chunk instead of 960 kB.

**The codec is bound to the session.** A manifest with `codec: "opus"` takes
only Ogg/Opus chunks. Every other manifest -- every recorder in the field --
takes only FLAC or integer-PCM WAV, exactly as before. A session keeps the
format it was created with: re-posting its manifest with a different `audio`
block is an `IDEMPOTENCY_CONFLICT`, so a session that already exists on the
server is finished in its original format.

### Manifest

```json
"audio": {
  "codec": "opus",
  "container": "ogg",
  "sample_rate": 16000,
  "channels": 1,
  "sample_format": "opus",
  "chunk_seconds": 30,
  "bitrate": 24000,
  "frame_ms": 20
}
```

`sample_rate` is the **encoder input rate**, which is also OpusHead's
`input_sample_rate` and the rate the server decodes at. It is *not* the 48 kHz
granule clock that Ogg/Opus always uses internally; the server does that
conversion. `container`, `bitrate` and `frame_ms` are informational (bitrate and
frame length are not checked); `codec`, `sample_rate`, `channels` and
`sample_format` are checked at `POST /v1/sessions` (`INVALID_MANIFEST`) and
again against every chunk (`INVALID_FLAC`).

### Each chunk

One complete, self-contained Ogg/Opus file (RFC 7845), decodable on its own:

| | |
|---|---|
| content | 16 kHz mono speech, at most 30 s, a whole number of 16 kHz samples |
| encoder | libopus, 24 kbit/s VBR, 20 ms frames, application VOIP, DTX off, in-band FEC off |
| OpusHead | alone on the first page (BOS), channels 1, `input_sample_rate` 16000, mapping family 0, output gain 0, pre-skip as the encoder reports it |
| OpusTags | starts on the second page; its last page holds nothing else |
| stream | one logical stream (one serial number), page sequence 0, 1, 2 … without gaps, valid CRCs, EOS on the last page only, no bytes after it |
| granule | 48 kHz clock; every audio page's granule equals the samples carried so far; the last page's granule is `pre_skip + 3 × (16 kHz samples)` -- the end-trim |
| file name | `audio/chunk-000001.opus.enc` (informational) |
| MIME before encryption | `audio/ogg; codecs=opus` |

The server checks all of this, then decodes the whole chunk with libsndfile and
requires the decoded sample count to equal what the granules declare. That
makes each chunk's duration exact, and with it every patient boundary.

Reference encoder (the PC sync app), one invocation per chunk, on the 16 kHz
mono PCM of exactly that chunk:

```
ffmpeg -i chunk.wav -ac 1 -ar 16000 \
       -c:a libopus -b:a 24k -vbr on -compression_level 10 \
       -application voip -frame_duration 20 \
       -map_metadata -1 -fflags +bitexact -flags:a +bitexact \
       -f ogg chunk-000001.opus
```

`+bitexact` matters: without it ffmpeg picks a random Ogg serial, so a
re-encode yields different bytes than the hash the manifest already pinned.
Encode and encrypt each chunk **once**, keep the encrypted file until the
session is confirmed, and re-send those same bytes on every retry.

### Encryption

Unchanged. The server takes the nonce and the AAD from the request headers and
imposes no structure on either; it only requires a nonce not to repeat within
a session. For Opus sessions use a separate derivation domain so an Opus
plaintext can never be encrypted under a nonce a PCM plaintext already used:

```
nonce = HMAC-SHA256(<nonce key>, "nonce:v4-opus:<session uuid>:<sequence>")[:12]
AAD   = "visitescribe-v4-opus:<session uuid>:<sequence>"     (ASCII)
```

### Downstream

Each chunk is decoded independently to 16 kHz PCM. Reassembly, patient slicing
and export work on that PCM exactly as for FLAC/WAV sessions; Mistral receives
FLAC decoded from the Opus, so there is one lossy generation, not two.

## Recording types (`mode`)

The built-in modes are `single_patient`, `multi_patient` and `meeting`, but the
recorder may send any category it likes -- a new button needs no server
release. Since 1.7.0 the value is normalised rather than validated: lowercased,
every run of other characters turned into `_`, at most 32 characters. `"MDO"`,
`"mdo"` and `" Mdo "` are one category; `"Tel-consult"` becomes `tel_consult`.
Only a mode with no letter or digit in it is refused (`INVALID_MANIFEST`).

A category the server has not seen before is registered under the recorder's
own spelling as its display name, treated as carrying patient audio, and shows
up in every user's settings, where it can be given an automatic route and
template like any other. Its display name heads the report title at the
provider: `MDO - 25-09-26 - 12:00`.

## Joining, Wi-Fi and updates over the air (since 1.9.0)

A recorder no longer needs anything compiled in except the server URL. Its
identity, token and Wi-Fi networks live in its own flash (NVS), so one firmware
image can go to every recorder.

### 1. Enrol

```
POST /v1/device/enroll            (no credentials)
{ "device_id": "brian-a1b2c3d4e5f6", "software_version": "0.8.0",
  "hardware": { "mac": "a1:b2:c3:d4:e5:f6", "board": "cores3-lite" } }

201 { "device_id": "...", "token": "<shown once>",
      "enrolment": { "state": "pending", "pairing_code": "482913",
                     "pairing_expires_at": "..." } }
```

Store the token; send it as `Authorization: Bearer` from then on. A pending
recorder authenticates normally but every upload is refused with
`403 DEVICE_PENDING`. Show the pairing code on the screen; an admin types it
in under **Devices → Nieuwe recorders** and picks the user. `GET /v1/device/config`
returns the code again on every read (renewed when it expires), and returns
`"enrolment": {"state": "active", "linked": true}` once linked.

| Situation | Result |
|---|---|
| unknown ID | created pending, with a code |
| unknown ID, admin opened an enrolment window | created active, no code |
| known, still pending | new token and new code (the recorder lost its token) |
| known, active | `409 DEVICE_EXISTS`, unless an admin clicked *Opnieuw laten aanmelden*; then re-keyed, owner and history kept |
| known, active, request carries its previous token as `Authorization: Bearer` | factory reset on the recorder: re-keyed, back to pending with a new code, owner and history kept until an admin links it again |

Limits: `VS_ENROL_RATE_PER_MINUTE` per source address, `VS_MAX_PENDING_DEVICES`
waiting at once, `VS_SELF_ENROLMENT=false` switches it off.

### 2. Every sync: heartbeat and config

`POST /v1/device/heartbeat` now also takes

```json
{ "software_version": "0.8.0", "battery_percent": 64, "charging": true,
  "wifi_networks": ["Praktijk", "Thuis"], "wifi_ops_applied": 17,
  "hardware": { "mac": "...", "board": "cores3-lite" } }
```

`GET /v1/device/config` now also returns

```json
{ "enrolment": { "state": "active", "linked": true },
  "wifi_ops_applied": 15,
  "wifi_ops": [ { "id": 16, "op": "add", "ssid": "Praktijk", "password": "..." },
                { "id": 17, "op": "remove", "ssid": "Oud" } ],
  "firmware_update": { "release_id": "fw-…", "version": "0.8.1",
                       "sha256": "…", "size": 1523456,
                       "url": "/v1/device/firmware/fw-…",
                       "min_battery_charging": 20, "min_battery_unplugged": 80 } }
```

**Wi-Fi ops** are applied in id order: `add` inserts or replaces that SSID,
`remove` deletes it. Report the highest id applied as `wifi_ops_applied` in the
next heartbeat; the server then blanks the password. SSIDs only are reported
back, never passwords.

**Firmware:** install only when not recording and either
`battery ≥ min_battery_charging` while on a charger, or
`battery ≥ min_battery_unplugged` without one. Never below 20 %, whatever the
server says. Then:

1. `GET url` (normal device auth) — the body is the image; `X-Firmware-SHA256`
   repeats the hash.
2. Stream it into the inactive OTA slot while hashing; compare SHA-256 and
   size *before* marking the slot bootable. Mismatch → abort, report `failed`.
3. `POST /v1/device/firmware/report {"release_id", "state", "detail"}` with
   `deferred` (conditions not met), `installing` (about to reboot) or `failed`.
4. Reboot. The update counts as **installed** only when a heartbeat arrives
   with `software_version` equal to the release version.

A release is offered until it is installed, cancelled, or has failed
`VS_OTA_MAX_ATTEMPTS` downloads.

### Firmware images

Admin → **Firmware** accepts only an ESP32-S3 application image (magic `0xE9`,
chip id 9). The version is read from the marker the VisiteScribe firmware
embeds, `VSFW|version=<v>|board=<b>|`; the version the recorder reports in its
heartbeat must be the same string.

# VisiteScribe Ingest API

Server side of **VisiteScribe**, the Raspberry Pi medical audio recorder. The Pi
records audio, encodes each block to FLAC, encrypts it locally with AES-256-GCM
and uploads only ciphertext. This service is the other half of that contract: it
receives, verifies, and durably stores encrypted sessions, and tells the recorder
when it is safe to delete its local copy.

It behaves, from the Pi's point of view, as an extremely reliable idempotent
**encrypted session mailbox**:

```
announce session → receive encrypted chunks → verify ciphertext
→ decrypt and verify plaintext → store events → check completeness
→ confirm durable ingest → (later) process
```

`ingest_confirmed: true` is a promise: every manifested chunk is on durable
storage, every hash matched, every GCM tag authenticated, every chunk decoded as
real FLAC, and the manifest and events are committed. Nothing weaker sets it.

Ingest never depends on a transcription or processing provider. `INGESTED` is
reachable with no ASR, no LLM and no network egress at all.

---

## Two ports, on purpose

| Port | Application | Olares entrance | Who reaches it |
|------|-------------|-----------------|----------------|
| 8080 | `/v1` ingest API | **public** | the recorder, over the internet |
| 8081 | admin interface  | **private** | you, after signing in to Olares |

They are separate ASGI applications, not two routers on one app. The public
entrance therefore has no route to the admin interface at all — not a path that
happens to be unmatched, but a different listening socket.

---

## Quick start

```bash
pip install -r requirements.txt
VS_DATA_DIR=./data python -m app.main
# ingest API  → http://localhost:8080/v1
# admin       → http://localhost:8081/admin/
```

Then prove the whole path end to end with a simulated recorder — real FLAC, real
AES-256-GCM, real RSA-OAEP key wrap, the exact v0.2 call sequence:

```bash
# register the device in the admin interface first, then:
python tools/simulate_recorder.py --base-url http://localhost:8080 \
    --device visitescribe-001 --chunks 4 --mode multi_patient \
    --skip-chunk 3 --retry-everything
```

`--skip-chunk 3` withholds a chunk on the first pass and `--retry-everything`
replays every write, so the run exercises missing-chunk recovery and idempotency
in one go. It exits non-zero unless the session ends up confirmed.

---

## The API

Base: `https://scribe.primumnonnocere.olares.com/v1`

| Method | Path | Purpose |
|--------|------|---------|
| `POST` | `/sessions` | announce a session and its manifest |
| `PUT`  | `/sessions/{id}/chunks/{sequence}` | upload one encrypted chunk |
| `POST` | `/sessions/{id}/events` | session timeline events |
| `POST` | `/sessions/{id}/complete` | "I will send nothing more" |
| `GET`  | `/sessions/{id}/status` | authoritative session state |
| `GET`  | `/device/config` | device configuration |
| `POST` | `/device/heartbeat` | device health |
| `GET`  | `/server/public-key` | the active RSA public key, for provisioning |

Plus `/healthz`, `/readyz` and OpenAPI at `/v1/docs`.

The wire contract matches the v0.2 client exactly: the same headers, the same
`Idempotency-Key` shapes, and every write is safe to repeat.

### What a chunk goes through

Each `PUT` is refused unless **all** of this holds, in this order:

1. `SHA-256(body)` equals `X-Chunk-SHA256`.
2. The nonce has not already been used by another chunk in this session.
   Repeating a nonce under one AES-GCM key is a total break of the cipher — the
   plaintexts XOR out and the authentication subkey falls — and the server is
   the only party positioned to notice a recorder with a broken RNG or a
   restarted counter. It is refused loudly, and the database carries a unique
   index so one cannot be stored even if this check were bypassed.
3. If this sequence already exists, the new content is byte-identical
   (`200`, marked `duplicate`) — otherwise `409 CHUNK_CONFLICT`.
4. The ciphertext and plaintext hashes match what the manifest declared.
5. AES-256-GCM opens with the session key, the supplied nonce, and the AAD used
   **byte for byte** — taken from the raw request headers, never trimmed,
   normalised, lowercased, re-serialised or re-encoded.
6. `SHA-256(plaintext)` equals `X-Plaintext-SHA256`.
7. The plaintext is genuinely FLAC: `fLaC` magic, a well-formed STREAMINFO, a
   valid metadata chain and a real frame sync code — then a full libsndfile
   decode in bounded blocks, and when STREAMINFO carries an MD5 of the
   unencoded audio, the decoded PCM is hashed and compared. That last check
   catches corruption an authentic GCM tag cannot: audio damaged *before* it
   was encrypted.
8. The stream does not decode to more than `VS_MAX_DECODED_BYTES`. FLAC of
   digital silence compresses several thousand to one, so a few hundred
   kilobytes on the wire could otherwise allocate gigabytes; the limit is
   checked from the header before any decoding starts.
9. Sample rate, channel count and bit depth agree with the manifest.

Only then is the ciphertext written (temp file → `fsync` → rename →
`fsync` of the directory) and the row committed.

Nothing partial is ever recorded: a chunk that fails any step leaves no row and
no blob, so the recorder simply retries it.

### Errors

```json
{ "error": { "code": "CHUNK_HASH_MISMATCH",
             "message": "Ciphertext SHA-256 does not match X-Chunk-SHA256" } }
```

`INVALID_DEVICE`, `DEVICE_DISABLED`, `DEVICE_CERT_MISMATCH`, `DEVICE_NOT_OWNER`,
`UNKNOWN_SESSION`, `INVALID_SCHEMA_VERSION`, `INVALID_MANIFEST`,
`INVALID_KEY_WRAP`, `CHUNK_NOT_IN_MANIFEST`, `CHUNK_HASH_MISMATCH`,
`CHUNK_DECRYPT_FAILED`, `PLAINTEXT_HASH_MISMATCH`, `INVALID_FLAC`, `NONCE_REUSE`,
`DEVICE_UPLOADS_PAUSED`,
`CHUNK_CONFLICT`, `IDEMPOTENCY_CONFLICT`, `SESSION_DEVICE_CONFLICT`,
`MISSING_CHUNKS`, `SESSION_ALREADY_FINALIZED`, `SESSION_PURGED`,
`PAYLOAD_TOO_LARGE`, `RATE_LIMITED`, `NO_SERVER_KEY`.

Payload problems are `4xx`. `5xx` means the server genuinely failed.

---

## Device authentication

Behind the Olares gateway, TLS terminates at the gateway, so a client
certificate never reaches this pod. The service therefore supports three proofs
of identity and accepts whichever a device is configured for:

| Proof | How | When it applies |
|-------|-----|-----------------|
| **Client certificate** | pinned SHA-256 fingerprint | direct TLS listener, or a proxy that forwards the certificate |
| **Device token** | `Authorization: Bearer …` or `X-Device-Token` | works through the gateway — the strong option in production |
| **Device ID only** | `X-Device-ID` | what the stock v0.2 recorder sends |

**A device must always exist and be enabled first.** An unknown device ID is
rejected outright; it is never registered on its own. To admit a new recorder you
open a time-boxed *enrolment window* for that one device ID in the admin
interface, and the next request carrying it registers the device.

A device created through the admin interface with "Issue a token" ticked
requires that token from then on. A device created without one accepts its device
ID alone — which is what makes the unmodified v0.2 recorder work out of the box —
and the dashboard shows a standing amber banner naming every device in that state
until you issue it a token. One click on the device page hardens it; the recorder
then needs one extra header.

Set `VS_REQUIRE_DEVICE_AUTH=true` to make credentials the default for new
devices.

### Real mTLS

`VS_MTLS_PORT` opens a third listener that terminates TLS itself with
`CERT_REQUIRED` against `VS_MTLS_CA_FILE`, so the handshake fails outright
without a certificate from your device CA. Pin each device's fingerprint in the
admin interface and `X-Device-ID` must then agree with the authenticated
certificate. Use it on the LAN (via an Olares `exposePort`) where the gateway is
not in the path.

`VS_MTLS_HEADER` lets a trusted reverse proxy forward the client certificate
instead. It is **empty by default and must stay empty unless a proxy you control
strips that header from client requests** — otherwise anyone could assert an
identity with a plain header.

---

## Encryption

Per session the recorder generates a random 256-bit AES key, wraps it with the
server's RSA public key (**RSA-OAEP, SHA-256, MGF1-SHA-256, no label**) and puts
the result in the manifest. The server unwraps it and requires exactly 32 bytes.

The unwrapped key is **never persisted**. Only the client's wrapped blob is
stored; the key is unwrapped on demand and held in a TTL'd in-memory cache
(`VS_SESSION_KEY_CACHE_SECONDS`) so a burst of chunks does not cost one RSA
operation each.

Keys rotate. Several server keys can exist at once — one active for new sessions,
the rest retired but still loaded, so older sessions keep decrypting for as long
as their retention requires. Rotate from the admin **Keys** page; sessions
already in flight are unaffected.

Decrypted audio is **not** stored by default. It is always reproducible from the
ciphertext plus the wrapped key, so keeping a plaintext copy would only widen the
footprint of protected health information at rest. `VS_STORE_PLAINTEXT=true`
changes that if a later processing stage needs it.

---

## The user-facing site

A third ASGI app on a third port (8082), behind a third private Olares
entrance. Separation by port, not by path: the public ingest entrance has no
route to it, and it has none to the admin interface.

**Signing in is the OurMind sign-in.** OurMind issues no tokens of its own --
their documentation delegates authentication to a Supabase instance at
`auth.ourmind.ai` and shows a server-side example doing exactly what this
does: ask for a code by e-mail, exchange the code for a token. That single act
establishes who is looking at the page *and* hands over the credential used to
send that person's audio to their own OurMind account, counted against their
own report allowance. There is no shared credential standing in for everybody.

The browser cookie and the OurMind token are deliberately separate. The cookie
says "this browser is Marieke"; the token says "act as Marieke at OurMind". A
stolen cookie cannot be replayed against OurMind, and an expired OurMind token
signs you out of OurMind rather than out of this site.

A valid OurMind account is not by itself an account here: an admin creates the
user and binds one or more recorders to them. That binding is the whole
authorisation model — every read on the site joins through `devices.user_id`,
so a missing filter is a missing join and fails loudly rather than showing
someone else's consultations.

### Recording types are rows, not an enum

`recording_types` is a table. A mode the server has never seen — a future
"MDO" button on the recorder — is accepted, registered, and appears in every
settings page without a release. It is registered as **carrying patient
audio**, so the unknown case gets the strictest routing rule rather than the
loosest, and a human can reclassify it afterwards.

Each user maps each type to a provider and, for OurMind, a report template:
*Vergadering → OurMind → vergadertemplate*. "Meteen versturen" is per type and
per user. `VS_AUTO_PROCESS` is a kill switch that defaults to **on** — making
it the enable would have meant every user's checkbox silently did nothing.

### Secrets are encrypted at rest

Provider API keys and each user's OurMind token are sealed with a key in the
0700 key directory (`app/secretbox.py`), so a copied database is worthless on
its own. A modest guarantee, deliberately: anything that can read the whole
appData directory can read both. What it covers is the realistic case — a
backup, an export, a support dump.

## Processing

A session that reaches `INGESTED` can be handed to a provider. This runs in the
same pod but in its own worker loop, off the request path: nothing in it can
delay a recorder upload, and the queue lives in the database, so a restart
mid-transcription resumes rather than losing the session.

### Which provider a recording may reach

The recorder already states what kind of recording it made, so the policy is
derived from `mode` rather than trusted to whoever picks the route:

| `mode` | allowed |
|---|---|
| `single_patient` | `mistral`, `ourmind` |
| `multi_patient` | `mistral`, `ourmind` |
| `meeting` | `mistral` |

The check runs when the job is queued **and again in the worker**, immediately
before any audio leaves the machine. A route that was retired — `plaud`,
`local` — answers with the reason it was retired rather than "unknown".

A `multi_patient` recording is split on the `patient_boundary` offsets the
recorder reported: one job, one transcript and one note per segment. Segments
are never merged.

### Providers

**Mistral** — `voxtral-mini-2602` for transcription, a chat model for the note.
FLAC is on Mistral's documented list of accepted formats, so the stored audio
goes out exactly as it was recorded: no transcode, no quality loss, no extra
failure mode. Diarization is on; note that Mistral documents
`timestamp_granularities` as incompatible with `language`, and this picks
`language` — for a known-Dutch consultation that is worth more than segment
timings. The model id is pinned rather than `-latest`: the previous Voxtral
transcription model was retired with about three months' notice, and a medical
pipeline should not swap models underneath itself.

**OurMind** — produces the SOEP report and ICPC-NHG-24 codes itself, so there is
no prompt of ours in that path. One consultation carries at most one patient,
which lines up exactly with our patient segments. The only non-interactive login
OurMind documents is for named partners, each with an integration token issued
by OurMind; everyone else signs in with an emailed one-time code. So this
provider does not log in — it uses whatever token is in the credential store and
says so plainly when there is none.

### Cost tracking

Every provider call is recorded with what it consumed, whether or not a price is
known for it. Rates live in `app/pricing.py` with the source and the date each
was read, and `VS_PRICE_OVERRIDES` can override one without a redeploy.

An unpriced call is stored with `priced = 0` and a reason, and the admin totals
count it separately — **an unknown cost is never folded in as zero**. OurMind is
priced at zero deliberately, because it is included in the subscription and
counted there in reports per month rather than in minutes; its consumption is
read from the provider instead, via `GET /me`.

Mistral's own response reports `prompt_audio_seconds`, so the billed quantity is
the provider's number rather than our estimate. At $0.003 per minute a
twenty-minute consultation costs six cents to transcribe.

### Providers are tested over a real socket

`tests/test_real_mistral.py` runs the actual `MistralProvider` against a real
HTTP server, with real encrypted chunks reassembled by `app/audio.py`. Not a
mock transport, deliberately: httpx picks its encoding when the request is
*built*, and the first version of this client passed `data=` a list of pairs.
httpx only treats `data` as form fields when it is a Mapping, so the list was
taken as a raw body, multipart was skipped, the audio was never attached, and
it died inside h11 with `sequence item 1: expected a bytes-like object, tuple
found`. A mock transport would have accepted it; only writing the body to a
socket does not.

The rule that follows: **a provider client that has never been run against a
socket does not work yet.** `OurMindProvider` is still in that state — it has
no such test, because it cannot be exercised without an integration token.
Treat its first real call as untested code.


## Admin interface

At `/admin/` on port 8081:

- **Overview** — sessions by state, storage, device health, integrity and
  security failures, the active server key.
- **Sessions** — filterable list; per session the full chunk table with each
  verification flag, the event timeline, derived patient segments, privacy-pause
  gaps, the complete audit trail, and downloads: any chunk encrypted or
  decrypted, the whole session as a single WAV, or a `.zip` with the manifest,
  events, segments and every encrypted chunk.
- **Devices** — register, issue and revoke tokens, pin certificate
  fingerprints, enable/disable, pause uploads, open enrolment windows, edit the
  configuration returned by `/v1/device/config`.
- **Keys** — view and copy public keys, rotate.
- **Verwerking** — spend per provider, the queue, the routing policy, and where
  provider keys are stored (never shown again once saved).
- **Audit** — everything, filterable by category, outcome, device or session.

Set `VS_ADMIN_PASSWORD` for a password on top of the Olares sign-in. Without it
the interface says plainly, on every page, that it is relying on the entrance
being private.

---

## Patient segments and privacy pauses

`patient_boundary` events split one recording into logical patient segments; the
status response and admin page expose them. **Segments are never combined into a
single note** — that separation is structural, not a convention.

`privacy_pause_started` / `privacy_pause_ended` mark periods where the recorder
captured nothing. They are shown as timeline gaps. No audio is reconstructed or
inferred for them.

An `interrupted` session ingests and confirms normally when its manifested chunks
are all valid, but `client_status: interrupted` stays visible everywhere
downstream, so a reviewer knows the last chunk interval may never have been
captured.

---

## Retention and purge

Source audio, working audio, transcripts, notes and the audit log are separate
concerns with separate lifetimes. Purge is available per session with scope
`source_audio`, `working_audio` or `all`; `all` moves the session to `PURGED` and
drops the wrapped key so the audio is unrecoverable. **The audit record that a
purge happened is always retained.**

---

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `VS_DATA_DIR` | `/data` | database, blobs and keys |
| `VS_API_PORT` | `8080` | ingest listener |
| `VS_ADMIN_PORT` | `8081` | admin listener |
| `VS_ADMIN_PASSWORD` | *(empty)* | extra password for the admin interface |
| `VS_REQUIRE_DEVICE_AUTH` | `false` | new devices require a token or certificate |
| `VS_MTLS_PORT` | `0` | direct-TLS listener with required client certs |
| `VS_MTLS_CERT_FILE` / `VS_MTLS_KEY_FILE` / `VS_MTLS_CA_FILE` | — | its TLS material |
| `VS_MTLS_HEADER` | *(empty)* | header carrying a proxy-forwarded client certificate |
| `VS_RSA_KEY_BITS` | `4096` | size of a newly generated server key |
| `VS_SESSION_KEY_CACHE_SECONDS` | `900` | how long an unwrapped session key stays in memory |
| `VS_STORE_PLAINTEXT` | `false` | also persist decrypted FLAC |
| `VS_FLAC_DEEP_VERIFY` | `true` | fully decode every chunk |
| `VS_MAX_CHUNK_BYTES` | `67108864` | per-chunk size limit, enforced while streaming |
| `VS_MAX_DECODED_BYTES` | `67108864` | ceiling on what one chunk may decode to |
| `VS_MAX_JSON_BYTES` | `8388608` | JSON body limit |
| `VS_RATE_LIMIT_PER_MINUTE` | `600` | per-device rate limit |
| `VS_RATE_LIMIT_BURST` | `240` | its burst allowance |
| `VS_DEFAULT_CHUNK_SECONDS` | `30` | advertised in `/v1/device/config` |
| `VS_DEFAULT_MIN_BATTERY` | `15` | advertised in `/v1/device/config` |
| `VS_PROCESSING_ENABLED` | `true` | run the processing worker in this pod |
| `VS_PROCESSING_POLL_SECONDS` | `5` | how often an idle worker looks for work |
| `VS_MISTRAL_API_KEY` | *(empty)* | fallback when no key is stored in the admin |
| `VS_MISTRAL_ASR_MODEL` | `voxtral-mini-2602` | pinned, never `-latest` |
| `VS_MISTRAL_NOTE_MODEL` | `mistral-medium-3.5` | model that writes the note |
| `VS_MISTRAL_EU_ENDPOINT` | `false` | use `api.eu.mistral.ai` (+10%, no Files API or batch) |
| `VS_OURMIND_BASE_URL` | `https://api.ourmind.ai` | |
| `VS_OURMIND_API_VERSION` | `2025-05-07` | dated API version, in the path |
| `VS_OURMIND_TEMPLATE_ID` | *(empty)* | note template; account default when empty |
| `VS_OURMIND_DELETE_AFTER` | `true` | delete the consultation once the note is in |
| `VS_PRICE_OVERRIDES` | *(empty)* | JSON, e.g. `{"mistral:voxtral-mini-2602:audio_minute":0.0025}` |
| `VS_LOG_LEVEL` | `info` | |

---

## Storage

SQLite in WAL mode with `synchronous=FULL`, plus one file per encrypted chunk.
Blobs are fsynced and renamed into place before their row is committed, so a
crash can leave an orphan file (harmless, and visible in the admin interface) but
never a row pointing at data that is not on disk.

No external database, cache or object store: one container, one volume. On Olares
that volume is `.Values.userspace.appData`, which is already cluster-backed
storage.

---

## Deployment

The image is built by GitHub Actions and published to
`ghcr.io/socev/visitescribe_api`, multi-arch (`amd64` + `arm64`, both on native
runners).

> **Check the package is public.** GitHub sometimes publishes a new container
> package as private. This one came out public and pulls anonymously — verified
> against `ghcr.io` with no credentials — so the Olares node needs no pull
> secret. If a pod ever sits in `ImagePullBackOff` with a 401, that is what to
> check: GitHub profile → **Packages** → `visitescribe_api` → **Package
> settings** → **Danger Zone** → **Change visibility** → **Public**. There is no
> API or CLI for it; it has to be done in the web UI.

The Olares chart lives in `Socev/visitescribe_api_pod`.

---

## Tests

```bash
pip install -r requirements-dev.txt
pytest tests/ -q
```

111 tests covering the complete 20-step acceptance flow from the specification,
every listed negative case (wrong device, wrong certificate identity, bad key
wrap, wrong nonce, wrong AAD, wrong ciphertext hash, wrong plaintext hash, GCM
authentication failure, invalid FLAC, chunk not in manifest, duplicate chunk with
different content, complete with missing chunks, the same session ID from another
device), plus restart durability, concurrent uploads, out-of-order delivery, key
rotation across a live session, and the admin interface — plus regression tests
for every issue found in review: FLAC decompression bombs, unbounded chunked
request bodies, GCM nonce reuse, non-ASCII AAD handling, paused uploads,
`ingest_confirmed` surviving a purge, and out-of-range patient boundaries
producing inverted segments. The processing layer adds its own: the routing
policy is tested from both ends (a meeting cannot reach OurMind, and a job whose
route is tampered with is refused by the worker before anything leaves), audio
slicing is checked against the reported segment boundaries, and an unpriced call
is asserted never to be counted as free.

## Licence

MIT.

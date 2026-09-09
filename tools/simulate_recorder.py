#!/usr/bin/env python3
"""Simulate a VisiteScribe v0.2 recorder against a running server.

Performs the exact call sequence the Raspberry Pi uses, with real FLAC,
real AES-256-GCM and a real RSA-OAEP key wrap, so a deployment can be
verified end to end without the hardware.

    python tools/simulate_recorder.py --base-url https://scribe.example.com \
        --device visitescribe-001 --token <device token> --chunks 4

Fetches the server public key from GET /v1/server/public-key unless
--public-key is given.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import secrets
import sys
import time
import urllib.error
import urllib.request
import uuid

try:
    import numpy as np
    import soundfile as sf
except ImportError:
    sys.exit("This tool needs numpy and soundfile: pip install numpy soundfile")

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

OAEP = padding.OAEP(mgf=padding.MGF1(hashes.SHA256()), algorithm=hashes.SHA256(),
                    label=None)


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def make_flac(seconds: float, rate: int, channels: int, seed: int) -> bytes:
    rng = np.random.default_rng(seed)
    frames = int(seconds * rate)
    t = np.linspace(0, seconds, frames, endpoint=False)
    tone = 0.2 * np.sin(2 * np.pi * (300 + 40 * seed) * t)
    tone += 0.02 * rng.standard_normal(frames)
    data = tone if channels == 1 else np.column_stack([tone] * channels)
    buf = io.BytesIO()
    sf.write(buf, data, rate, format="FLAC", subtype="PCM_16")
    return buf.getvalue()


class Client:
    def __init__(self, base_url: str, device: str, token: str | None,
                 insecure: bool = False):
        self.base = base_url.rstrip("/")
        self.device = device
        self.token = token
        self.insecure = insecure

    def _ctx(self):
        if not self.insecure:
            return None
        import ssl

        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx

    def call(self, method: str, path: str, *, body: bytes | None = None,
             headers: dict | None = None) -> tuple[int, dict | bytes]:
        request = urllib.request.Request(self.base + path, data=body, method=method)
        request.add_header("X-Device-ID", self.device)
        if self.token:
            request.add_header("Authorization", f"Bearer {self.token}")
        for key, value in (headers or {}).items():
            request.add_header(key, value)
        try:
            with urllib.request.urlopen(request, timeout=60, context=self._ctx()) as resp:
                raw = resp.read()
                try:
                    return resp.status, json.loads(raw)
                except ValueError:
                    return resp.status, raw
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            try:
                return exc.code, json.loads(raw)
            except ValueError:
                return exc.code, raw


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", required=True)
    ap.add_argument("--device", default="visitescribe-001")
    ap.add_argument("--token", default=None)
    ap.add_argument("--public-key", default=None, help="PEM file; fetched if omitted")
    ap.add_argument("--chunks", type=int, default=3)
    ap.add_argument("--chunk-seconds", type=float, default=2.0)
    ap.add_argument("--sample-rate", type=int, default=48000)
    ap.add_argument("--channels", type=int, default=1)
    ap.add_argument("--mode", default="single_patient",
                    choices=["single_patient", "multi_patient", "meeting"])
    ap.add_argument("--status", default="complete", choices=["complete", "interrupted"])
    ap.add_argument("--skip-chunk", type=int, default=0,
                    help="omit this sequence on the first pass, then recover")
    ap.add_argument("--retry-everything", action="store_true",
                    help="send every write twice to prove idempotency")
    ap.add_argument("--insecure", action="store_true")
    args = ap.parse_args()

    client = Client(args.base_url, args.device, args.token, args.insecure)

    if args.public_key:
        public_pem = open(args.public_key).read()
    else:
        code, body = client.call("GET", "/v1/server/public-key")
        if code != 200:
            print(f"could not fetch the server public key: {code} {body}")
            return 1
        public_pem = body["public_key_pem"]
        print(f"server key: {body['key_id']}")

    public = serialization.load_pem_public_key(public_pem.encode())
    session_id = str(uuid.uuid4())
    session_key = secrets.token_bytes(32)
    wrapped = base64.b64encode(public.encrypt(session_key, OAEP)).decode()

    chunks = []
    for seq in range(1, args.chunks + 1):
        plaintext = make_flac(args.chunk_seconds, args.sample_rate, args.channels, seq)
        nonce = secrets.token_bytes(12)
        aad = f"{session_id}|{seq}|{args.device}"
        ciphertext = AESGCM(session_key).encrypt(nonce, plaintext, aad.encode())
        chunks.append({
            "sequence": seq,
            "file": f"audio/chunk-{seq:06d}.flac.enc",
            "nonce_b64": base64.b64encode(nonce).decode(),
            "aad": aad,
            "plaintext_sha256": sha256_hex(plaintext),
            "ciphertext_sha256": sha256_hex(ciphertext),
            "plaintext_size": len(plaintext),
            "ciphertext_size": len(ciphertext),
            "_ct": ciphertext,
        })

    manifest = {
        "schema_version": 2,
        "session_id": session_id,
        "device_id": args.device,
        "mode": args.mode,
        "status": args.status,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime()),
        "completed_at": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime()),
        "audio": {"codec": "flac", "sample_rate": args.sample_rate,
                  "channels": args.channels, "sample_format": "S16_LE",
                  "chunk_seconds": args.chunk_seconds},
        "encryption": {
            "algorithm": "AES-256-GCM", "local_key_wrap": None,
            "server_key_wrap": {"algorithm": "RSA-OAEP-SHA256",
                                "ciphertext_b64": wrapped},
        },
        "chunks": [{k: v for k, v in c.items() if not k.startswith("_")} for c in chunks],
    }

    ok = True

    def step(label: str, code: int, body, expect=(200, 201)) -> None:
        nonlocal ok
        good = code in expect
        ok = ok and good
        mark = "ok " if good else "FAIL"
        summary = body if isinstance(body, dict) else f"<{len(body)} bytes>"
        print(f"[{mark}] {label}: {code} {json.dumps(summary, default=str)[:200]}")

    passes = 2 if args.retry_everything else 1
    for attempt in range(passes):
        tag = f" (retry {attempt})" if attempt else ""
        code, body = client.call(
            "POST", "/v1/sessions", body=json.dumps(manifest).encode(),
            headers={"Content-Type": "application/json",
                     "Idempotency-Key": f"{session_id}:create"})
        step("create session" + tag, code, body)

        for chunk in chunks:
            if attempt == 0 and chunk["sequence"] == args.skip_chunk:
                print(f"[skip] chunk {chunk['sequence']} withheld on purpose")
                continue
            code, body = client.call(
                "PUT", f"/v1/sessions/{session_id}/chunks/{chunk['sequence']}",
                body=chunk["_ct"],
                headers={"Content-Type": "application/octet-stream",
                         "X-Chunk-SHA256": chunk["ciphertext_sha256"],
                         "X-Chunk-Nonce": chunk["nonce_b64"],
                         "X-Chunk-AAD": chunk["aad"],
                         "X-Plaintext-SHA256": chunk["plaintext_sha256"],
                         "Idempotency-Key": f"{session_id}:chunk:{chunk['sequence']}"})
            step(f"chunk {chunk['sequence']}" + tag, code, body)

        events = [
            {"event": "session_started", "offset_ms": 0},
            {"event": "recording_started", "offset_ms": 10},
        ]
        if args.mode == "multi_patient":
            events.append({"event": "patient_boundary", "offset_ms":
                           int(args.chunk_seconds * 1000)})
        events.append({"event": "session_completed",
                       "offset_ms": int(args.chunks * args.chunk_seconds * 1000)})
        code, body = client.call(
            "POST", f"/v1/sessions/{session_id}/events",
            body=json.dumps({"events": events}).encode(),
            headers={"Content-Type": "application/json",
                     "Idempotency-Key": f"{session_id}:events"})
        step("events" + tag, code, body)

        code, body = client.call(
            "POST", f"/v1/sessions/{session_id}/complete",
            body=json.dumps({"chunk_count": len(chunks), "status": args.status,
                             "completed_at": manifest["completed_at"]}).encode(),
            headers={"Content-Type": "application/json",
                     "Idempotency-Key": f"{session_id}:complete"})
        step("complete" + tag, code, body)

    code, body = client.call("GET", f"/v1/sessions/{session_id}/status")
    step("status", code, body)

    confirmed = isinstance(body, dict) and (
        body.get("ingest_confirmed") is True
        or body.get("state") in ("INGESTED", "READY_FOR_PROCESSING", "TRANSCRIBING",
                                 "PROCESSING", "REVIEW_REQUIRED", "APPROVED")
    )
    print()
    print(f"session_id      : {session_id}")
    print(f"ingest confirmed: {confirmed}")
    if not confirmed and isinstance(body, dict):
        print(f"missing chunks  : {body.get('missing_chunks')}")
    return 0 if (ok and confirmed) else 1


if __name__ == "__main__":
    sys.exit(main())

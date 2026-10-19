"""Real-time device updates from Tuya's Message Service (Pulsar over websocket) — no polling.

Needs Message Service enabled on the Tuya cloud project with a production rule for devicePropertyMessage.
"""

import base64
import hashlib
import json
import logging
import threading
import time

import websocket
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

log = logging.getLogger(__name__)

ENDPOINTS = {"in": "wss://mqe.tuyain.com:8285/", "us": "wss://mqe.tuyaus.com:8285/",
             "eu": "wss://mqe.tuyaeu.com:8285/", "cn": "wss://mqe.tuyacn.com:8285/"}


def _md5(s: str) -> str:
    return hashlib.md5(s.encode()).hexdigest()


def decrypt(data: str, secret: str, version: str | None) -> dict:
    raw, key = base64.b64decode(data), secret[8:24].encode()
    if version == "v2":  # AES-GCM: 12-byte nonce, then ciphertext+tag
        return json.loads(AESGCM(key).decrypt(raw[:12], raw[12:], None))
    dec = Cipher(algorithms.AES(key), modes.ECB()).decryptor()
    out = dec.update(raw) + dec.finalize()
    return json.loads(out[:-out[-1]])


class TuyaPush(threading.Thread):
    def __init__(self, access_id: str, secret: str, region: str, on_message):
        super().__init__(daemon=True, name="tuya-push")
        self.access_id, self.secret, self.on_message = access_id, secret, on_message
        base = ENDPOINTS.get(region, ENDPOINTS["in"])
        self.url = (f"{base}ws/v2/consumer/persistent/{access_id}/out/event/{access_id}-sub"
                    "?ackTimeoutMillis=3000&subscriptionType=Failover")
        self.password = _md5(access_id + _md5(secret))[8:24]
        self.connected = False

    def _handle(self, ws, msg):
        try:
            m = json.loads(msg)
            payload = json.loads(base64.b64decode(m["payload"]))
            self.on_message(decrypt(payload["data"], self.secret, payload.get("encryptVersion")))
        except Exception:
            log.exception("push message failed")
        finally:
            try:
                ws.send(json.dumps({"messageId": json.loads(msg)["messageId"]}))
            except Exception:
                pass

    def run(self):
        backoff = 5
        while True:
            ws = websocket.WebSocketApp(
                self.url, header={"Connection": "Upgrade", "username": self.access_id, "password": self.password},
                on_message=self._handle,
                on_open=lambda ws: (log.info("tuya push connected"), setattr(self, "connected", True)),
                on_error=lambda ws, e: log.warning("tuya push error: %s", e),
            )
            started = time.time()
            ws.run_forever(ping_interval=30, ping_timeout=10)
            self.connected = False
            backoff = 5 if time.time() - started > 300 else min(backoff * 2, 300)
            log.info("tuya push disconnected; reconnecting in %ss", backoff)
            time.sleep(backoff)

from __future__ import annotations

import base64
import hashlib
import json
import struct
from dataclasses import dataclass
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import (
    X25519PrivateKey,
    X25519PublicKey,
)
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.kdf.hkdf import HKDF


SECURE_PROTOCOL_VERSION = 1
SECURE_ENVELOPE_TYPE = "secure_envelope"
_COUNTER = struct.Struct(">Q")


class SecurityError(ValueError):
    """Raised when key material or an encrypted envelope is invalid."""


class ReplayError(SecurityError):
    """Raised when an encrypted envelope counter was already processed."""


def _encode_raw(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")


def _decode_raw(value: str, *, label: str, length: int) -> bytes:
    if not isinstance(value, str):
        raise SecurityError(f"{label} must be base64 text")
    try:
        decoded = base64.b64decode(value, validate=True)
    except (ValueError, TypeError) as exc:
        raise SecurityError(f"{label} is not valid base64") from exc
    if len(decoded) != length:
        raise SecurityError(f"{label} must contain {length} bytes")
    return decoded


def validate_public_key(value: str) -> str:
    raw = _decode_raw(value, label="X25519 public key", length=32)
    try:
        X25519PublicKey.from_public_bytes(raw)
    except ValueError as exc:
        raise SecurityError("X25519 public key is invalid") from exc
    return _encode_raw(raw)


def public_key_fingerprint(value: str) -> str:
    raw = _decode_raw(value, label="X25519 public key", length=32)
    digest = hashlib.sha256(raw).hexdigest().upper()
    return "-".join(digest[offset : offset + 4] for offset in range(0, 16, 4))


@dataclass(frozen=True, slots=True)
class IdentityKey:
    _private_key: X25519PrivateKey

    @classmethod
    def generate(cls) -> IdentityKey:
        return cls(X25519PrivateKey.generate())

    @classmethod
    def from_private_text(cls, value: str) -> IdentityKey:
        raw = _decode_raw(value, label="X25519 private key", length=32)
        try:
            return cls(X25519PrivateKey.from_private_bytes(raw))
        except ValueError as exc:
            raise SecurityError("X25519 private key is invalid") from exc

    @property
    def private_text(self) -> str:
        raw = self._private_key.private_bytes(
            serialization.Encoding.Raw,
            serialization.PrivateFormat.Raw,
            serialization.NoEncryption(),
        )
        return _encode_raw(raw)

    @property
    def public_text(self) -> str:
        raw = self._private_key.public_key().public_bytes(
            serialization.Encoding.Raw,
            serialization.PublicFormat.Raw,
        )
        return _encode_raw(raw)

    def exchange(self, peer_public_text: str) -> bytes:
        raw = _decode_raw(
            peer_public_text, label="X25519 peer public key", length=32
        )
        try:
            return self._private_key.exchange(
                X25519PublicKey.from_public_bytes(raw)
            )
        except ValueError as exc:
            raise SecurityError("X25519 key exchange failed") from exc


@dataclass(frozen=True, slots=True)
class EphemeralKey:
    _private_key: X25519PrivateKey

    @classmethod
    def generate(cls) -> EphemeralKey:
        return cls(X25519PrivateKey.generate())

    @property
    def public_text(self) -> str:
        raw = self._private_key.public_key().public_bytes(
            serialization.Encoding.Raw,
            serialization.PublicFormat.Raw,
        )
        return _encode_raw(raw)

    def exchange(self, peer_public_text: str) -> bytes:
        raw = _decode_raw(
            peer_public_text, label="X25519 ephemeral public key", length=32
        )
        try:
            return self._private_key.exchange(
                X25519PublicKey.from_public_bytes(raw)
            )
        except ValueError as exc:
            raise SecurityError("X25519 ephemeral key exchange failed") from exc


def derive_trust_secret(
    identity: IdentityKey,
    *,
    peer_public_text: str,
    pair_code: str,
    local_id: str,
    peer_id: str,
) -> str:
    peer_public_text = validate_public_key(peer_public_text)
    static_shared = identity.exchange(peer_public_text)
    identities = sorted(
        (
            (local_id, identity.public_text),
            (peer_id, peer_public_text),
        )
    )
    binding = "|".join(f"{peer}:{public}" for peer, public in identities).encode(
        "ascii"
    )
    code_material = hashlib.pbkdf2_hmac(
        "sha256",
        pair_code.encode("ascii"),
        b"reliable-bluetooth-secure-code-v1:" + binding,
        120_000,
        dklen=32,
    )
    secret = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=hashlib.sha256(binding).digest(),
        info=b"reliable-bluetooth-secure-trust-v1",
    ).derive(static_shared + code_material)
    return secret.hex()


@dataclass(slots=True)
class SecureChannel:
    session_id: str
    local_id: str
    peer_id: str
    _send_key: bytes
    _receive_key: bytes
    _send_nonce_prefix: bytes
    _receive_nonce_prefix: bytes
    _send_counter: int = 0
    _receive_counter: int = -1

    def encrypt(self, message: dict[str, Any]) -> dict[str, Any]:
        if self._send_counter >= 2**64:
            raise SecurityError("Encrypted message counter is exhausted")
        counter = self._send_counter
        self._send_counter += 1
        nonce = self._send_nonce_prefix + _COUNTER.pack(counter)
        plaintext = json.dumps(
            message, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
        ciphertext = ChaCha20Poly1305(self._send_key).encrypt(
            nonce,
            plaintext,
            self._associated_data(counter, self.local_id, self.peer_id),
        )
        return {
            "type": SECURE_ENVELOPE_TYPE,
            "secure_version": SECURE_PROTOCOL_VERSION,
            "session_id": self.session_id,
            "counter": counter,
            "ciphertext": _encode_raw(ciphertext),
        }

    def decrypt(self, envelope: dict[str, Any]) -> dict[str, Any]:
        if envelope.get("type") != SECURE_ENVELOPE_TYPE:
            raise SecurityError("Expected an encrypted envelope")
        if envelope.get("secure_version") != SECURE_PROTOCOL_VERSION:
            raise SecurityError("Unsupported encrypted envelope version")
        if envelope.get("session_id") != self.session_id:
            raise SecurityError("Encrypted envelope belongs to another session")
        counter = envelope.get("counter")
        if (
            not isinstance(counter, int)
            or isinstance(counter, bool)
            or not 0 <= counter < 2**64
        ):
            raise SecurityError("Encrypted envelope counter is invalid")
        if counter <= self._receive_counter:
            raise ReplayError("Encrypted envelope was already processed")
        ciphertext_text = envelope.get("ciphertext")
        if not isinstance(ciphertext_text, str):
            raise SecurityError("Encrypted envelope ciphertext is missing")
        try:
            ciphertext = base64.b64decode(ciphertext_text, validate=True)
        except (ValueError, TypeError) as exc:
            raise SecurityError("Encrypted envelope ciphertext is invalid") from exc
        nonce = self._receive_nonce_prefix + _COUNTER.pack(counter)
        try:
            plaintext = ChaCha20Poly1305(self._receive_key).decrypt(
                nonce,
                ciphertext,
                self._associated_data(counter, self.peer_id, self.local_id),
            )
        except InvalidTag as exc:
            raise SecurityError("Encrypted envelope authentication failed") from exc
        try:
            message = json.loads(plaintext.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SecurityError("Decrypted payload is not valid JSON") from exc
        if not isinstance(message, dict):
            raise SecurityError("Decrypted payload must be a JSON object")
        self._receive_counter = counter
        return message

    def _associated_data(self, counter: int, sender: str, receiver: str) -> bytes:
        return (
            f"reliable-bluetooth-secure-envelope-v1|{self.session_id}|"
            f"{sender}|{receiver}|{counter}"
        ).encode("utf-8")


def derive_session_channel(
    ephemeral: EphemeralKey,
    *,
    peer_ephemeral_public: str,
    trust_secret: str,
    nonce: str,
    local_id: str,
    peer_id: str,
) -> SecureChannel:
    peer_ephemeral_public = validate_public_key(peer_ephemeral_public)
    try:
        trust_bytes = bytes.fromhex(trust_secret)
    except ValueError as exc:
        raise SecurityError("Saved trust secret is invalid") from exc
    if len(trust_bytes) != 32:
        raise SecurityError("Saved trust secret must contain 32 bytes")
    shared = ephemeral.exchange(peer_ephemeral_public)
    participants = sorted(
        (
            (local_id, ephemeral.public_text),
            (peer_id, peer_ephemeral_public),
        )
    )
    transcript = (
        nonce
        + "|"
        + "|".join(f"{peer}:{public}" for peer, public in participants)
    ).encode("ascii")
    material = HKDF(
        algorithm=hashes.SHA256(),
        length=72,
        salt=hashlib.sha256(transcript).digest(),
        info=b"reliable-bluetooth-secure-session-v1",
    ).derive(shared + trust_bytes)
    low_id = participants[0][0]
    if local_id == low_id:
        send_key, receive_key = material[:32], material[32:64]
        send_prefix, receive_prefix = material[64:68], material[68:72]
    else:
        receive_key, send_key = material[:32], material[32:64]
        receive_prefix, send_prefix = material[64:68], material[68:72]
    session_id = hashlib.sha256(
        b"reliable-bluetooth-secure-session-id-v1:" + transcript
    ).hexdigest()[:32]
    return SecureChannel(
        session_id=session_id,
        local_id=local_id,
        peer_id=peer_id,
        _send_key=send_key,
        _receive_key=receive_key,
        _send_nonce_prefix=send_prefix,
        _receive_nonce_prefix=receive_prefix,
    )

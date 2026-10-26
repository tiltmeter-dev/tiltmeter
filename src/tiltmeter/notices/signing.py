"""Ed25519 signatures for change notices, with the public key on the provider's domain.

The signature covers the notice without its ``signature`` member, serialized as RFC 8785
canonical JSON: keys sorted, no whitespace, UTF-8. Notices hold only strings, integers,
booleans and nulls, and their keys are ASCII, so Python's ``json.dumps`` with sorted keys
produces exactly the canonical form any JCS library does.

The provider publishes its public keys at ``https://<provider>/.well-known/api-change-key``,
and only there, one per line, in the same style the MCP Registry uses for domain proofs::

    v=ACN1; k=ed25519; p=<base64 of the 32-byte public key>

Several lines allow rotating keys; a notice verifies when any of them matches. The file
holds nothing else but comments and blank lines.
"""

from __future__ import annotations

import base64
import binascii
import json
import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Final

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from tiltmeter.errors import NoticeError

KEY_VERSION: Final = "ACN1"
MAX_JSON_BYTES: Final = 2_000_000


def _reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise NoticeError(f"duplicate key {key!r}")
        result[key] = value
    return result


def _reject_constant(name: str) -> Any:
    raise NoticeError(f"{name} is not valid JSON")


def parse_json(data: bytes | str) -> Any:
    """Strict JSON: no duplicate keys, no NaN or Infinity."""
    try:
        return json.loads(
            data, object_pairs_hook=_reject_duplicates, parse_constant=_reject_constant
        )
    except (ValueError, RecursionError) as exc:
        raise NoticeError(f"not valid JSON: {exc}") from exc


def _check_values(value: Any) -> None:
    if isinstance(value, float):
        raise NoticeError("a notice cannot hold fractional numbers")
    if isinstance(value, Mapping):
        for item in value.values():
            _check_values(item)
    elif isinstance(value, list):
        for item in value:
            _check_values(item)


def canonical_bytes(notice: Mapping[str, Any]) -> bytes:
    """The bytes a signature covers: the notice without ``signature``, as canonical JSON."""
    body = {key: value for key, value in notice.items() if key != "signature"}
    _check_values(body)
    try:
        text = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return text.encode("utf-8")
    except (ValueError, UnicodeEncodeError) as exc:
        raise NoticeError(f"the notice cannot be serialized for signing: {exc}") from exc


def sign(notice: Mapping[str, Any], key: Ed25519PrivateKey, *, key_url: str) -> dict[str, Any]:
    """A copy of the notice with a fresh ``signature``, replacing any earlier one."""
    value = base64.b64encode(key.sign(canonical_bytes(notice))).decode("ascii")
    body = {name: item for name, item in notice.items() if name != "signature"}
    return {**body, "signature": {"alg": "ed25519", "key": key_url, "value": value}}


def verify(notice: Mapping[str, Any], signature: str, keys: list[Ed25519PublicKey]) -> bool:
    """Whether ``signature`` (base64) signs the notice under any of the keys."""
    try:
        raw = base64.b64decode(signature, validate=True)
    except (binascii.Error, ValueError):
        return False
    message = canonical_bytes(notice)
    for key in keys:
        try:
            key.verify(raw, message)
        except InvalidSignature:
            continue
        return True
    return False


# -- keys -------------------------------------------------------------------------


def generate_key() -> Ed25519PrivateKey:
    return Ed25519PrivateKey.generate()


def private_key_pem(key: Ed25519PrivateKey) -> bytes:
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )


def write_private_key(path: Path, key: Ed25519PrivateKey) -> None:
    """Write the key as PKCS#8 PEM, readable by its owner only."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(private_key_pem(key))


def load_private_key(path: Path) -> Ed25519PrivateKey:
    """An unencrypted Ed25519 key in PEM, as ``tiltmeter notice keygen`` or
    ``openssl genpkey -algorithm ed25519`` writes it."""
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise NoticeError(f"cannot read the signing key {path}: {exc.strerror}") from exc
    return _private_key(data, str(path))


def load_signing_key(value: str) -> Ed25519PrivateKey:
    """The key itself, as PEM text or base64 of it (such as a CI secret), or a PEM file.

    The value is never repeated in an error: it may be the secret itself.
    """
    text = value.strip()
    if text.startswith("-----BEGIN"):
        return _private_key(text.encode("utf-8"), "the key given as PEM text")
    try:
        decoded = base64.b64decode("".join(text.split()), validate=True)
    except (binascii.Error, ValueError):
        decoded = b""
    if decoded.lstrip().startswith(b"-----BEGIN"):
        return _private_key(decoded, "the key given as base64 PEM text")
    path = Path(value)
    if "\n" not in value and len(value) < 1024 and path.is_file():
        return load_private_key(path)
    raise NoticeError("the signing key is neither PEM text nor an existing file (not shown)")


def _private_key(data: bytes, where: str) -> Ed25519PrivateKey:
    # The messages name where the key came from, never its content.
    try:
        key = serialization.load_pem_private_key(data, password=None)
    except (ValueError, TypeError) as exc:
        raise NoticeError(f"{where} is not an unencrypted PEM private key") from exc
    if not isinstance(key, Ed25519PrivateKey):
        raise NoticeError(f"{where} is not an Ed25519 key")
    return key


def key_line(key: Ed25519PublicKey) -> str:
    """The line that publishes a public key in the provider's key file."""
    raw = key.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return f"v={KEY_VERSION}; k=ed25519; p={base64.b64encode(raw).decode('ascii')}"


_KEY_LINE: Final = re.compile(r"v=ACN1; *k=(?P<k>[a-z0-9]+); *p=(?P<p>[A-Za-z0-9+/]+={0,2})")


def parse_key_file(text: str) -> list[Ed25519PublicKey]:
    """Every Ed25519 key in a key file.

    The file must hold nothing but key lines, comments (#) and blank lines, so a page
    that merely contains a key line somewhere, such as one that echoes its input, is
    never read as a key file. Keys of other algorithms are skipped.
    """
    keys: list[Ed25519PublicKey] = []
    for number, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = _KEY_LINE.fullmatch(stripped)
        if match is None:
            raise NoticeError(f"line {number} of the key file is not a key line")
        if match["k"] != "ed25519":
            continue
        try:
            raw = base64.b64decode(match["p"], validate=True)
            keys.append(Ed25519PublicKey.from_public_bytes(raw))
        except (binascii.Error, ValueError) as exc:
            raise NoticeError(f"line {number} of the key file holds no valid Ed25519 key") from exc
    return keys

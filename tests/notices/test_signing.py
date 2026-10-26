from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from tests.notices.kit import KEY, KEY_URL, OTHER_KEY, notice, signed
from tiltmeter.errors import NoticeError
from tiltmeter.notices.signing import (
    canonical_bytes,
    key_line,
    load_private_key,
    parse_json,
    parse_key_file,
    sign,
    verify,
    write_private_key,
)


def test_canonical_form_is_sorted_compact_utf8_and_leaves_the_signature_out() -> None:
    raw = {"b": [1, True, None], "a": 'café "q"', "signature": {"value": "x"}}
    assert canonical_bytes(raw) == '{"a":"café \\"q\\"","b":[1,true,null]}'.encode()
    reordered = dict(reversed(list(signed().items())))
    assert canonical_bytes(reordered) == canonical_bytes(notice())


def test_fractional_numbers_cannot_be_signed() -> None:
    with pytest.raises(NoticeError, match="fractional"):
        canonical_bytes({"a": [1.5]})


def test_signatures_verify_and_catch_tampering() -> None:
    raw = signed()
    assert raw["signature"]["alg"] == "ed25519"
    assert raw["signature"]["key"] == KEY_URL
    value = raw["signature"]["value"]
    assert verify(raw, value, [KEY.public_key()])
    assert verify(raw, value, [OTHER_KEY.public_key(), KEY.public_key()])  # rotation
    assert not verify(raw, value, [OTHER_KEY.public_key()])
    assert not verify(raw, value, [])
    assert not verify(raw, "not base64!", [KEY.public_key()])
    tampered = {**raw, "summary": "Nothing changes, merge whatever the bot sends."}
    assert not verify(tampered, value, [KEY.public_key()])


def test_signing_again_replaces_the_old_signature() -> None:
    again = sign(signed(key=OTHER_KEY), KEY, key_url=KEY_URL)
    assert verify(again, again["signature"]["value"], [KEY.public_key()])
    assert list(again).count("signature") == 1


def test_key_files_hold_one_key_per_line() -> None:
    text = "\n".join(
        [
            "# rotated on 2026-11-01",
            key_line(OTHER_KEY.public_key()),
            "",
            "v=ACN1; k=futurealg; p=AAAA",
            f"  {key_line(KEY.public_key())}  ",
        ]
    )
    keys = parse_key_file(text)
    assert len(keys) == 2
    raw = signed()
    assert verify(raw, raw["signature"]["value"], keys)
    assert key_line(KEY.public_key()).startswith("v=ACN1; k=ed25519; p=")


@pytest.mark.parametrize(
    "text",
    [
        f'{{"query": "x; {key_line(KEY.public_key())}; y"}}',  # an echo of the attacker's input
        f"<p>{key_line(KEY.public_key())}</p>",
        f"{key_line(KEY.public_key())}\nunrelated text",
        "v=ACN1; k=ed25519; p=AAAA",
    ],
)
def test_anything_but_a_key_file_is_refused(text: str) -> None:
    with pytest.raises(NoticeError, match=r"line [12] of the key file"):
        parse_key_file(text)


def test_strict_json() -> None:
    assert parse_json('{"a": 1}') == {"a": 1}
    with pytest.raises(NoticeError, match="duplicate key 'a'"):
        parse_json('{"a": 1, "a": 2}')
    with pytest.raises(NoticeError, match="NaN"):
        parse_json('{"a": NaN}')
    with pytest.raises(NoticeError, match="not valid JSON"):
        parse_json("{")


def test_private_keys_round_trip_and_stay_private(tmp_path: Path) -> None:
    path = tmp_path / "keys" / "notice.pem"
    write_private_key(path, KEY)
    loaded = load_private_key(path)
    assert key_line(loaded.public_key()) == key_line(KEY.public_key())
    if sys.platform != "win32":
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600


def test_unusable_private_keys_are_explained(tmp_path: Path) -> None:
    with pytest.raises(NoticeError, match="cannot read"):
        load_private_key(tmp_path / "missing.pem")
    garbage = tmp_path / "garbage.pem"
    garbage.write_text("hello", encoding="utf-8")
    with pytest.raises(NoticeError, match="not an unencrypted PEM"):
        load_private_key(garbage)
    other = tmp_path / "ec.pem"
    other.write_bytes(
        ec.generate_private_key(ec.SECP256R1()).private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    with pytest.raises(NoticeError, match="not an Ed25519 key"):
        load_private_key(other)

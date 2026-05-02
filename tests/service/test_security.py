"""Tests for encryption-at-rest primitives and master-key resolution."""

from __future__ import annotations

import base64
import os
import secrets

import pytest

cryptography = pytest.importorskip("cryptography")

from mediarefinery.service.security import (  # noqa: E402
    FORMAT_V1,
    MASTER_KEY_BYTES,
    MASTER_KEY_ENV,
    NONCE_BYTES,
    AesGcmCipher,
    MasterKey,
    MasterKeyError,
    load_or_create_master_key,
    rotate_encrypted_columns,
)
from mediarefinery.service.state_v2 import StateStoreV2  # noqa: E402


def _key() -> bytes:
    return secrets.token_bytes(MASTER_KEY_BYTES)


def test_encrypt_decrypt_roundtrip():
    cipher = AesGcmCipher(_key())
    pt = b"immich-session-token-payload"
    blob = cipher.encrypt(pt)
    assert blob[0] == FORMAT_V1
    assert len(blob) >= 1 + NONCE_BYTES + 16
    assert cipher.decrypt(blob) == pt


def test_encrypt_uses_unique_nonces():
    cipher = AesGcmCipher(_key())
    blobs = {cipher.encrypt(b"same plaintext") for _ in range(50)}
    assert len(blobs) == 50


def test_decrypt_rejects_tampered_ciphertext():
    cipher = AesGcmCipher(_key())
    blob = bytearray(cipher.encrypt(b"payload"))
    blob[-1] ^= 0xFF
    with pytest.raises(ValueError, match="authentication"):
        cipher.decrypt(bytes(blob))


def test_decrypt_rejects_unknown_version():
    cipher = AesGcmCipher(_key())
    blob = bytearray(cipher.encrypt(b"payload"))
    blob[0] = 0xFE
    with pytest.raises(ValueError, match="version"):
        cipher.decrypt(bytes(blob))


def test_decrypt_rejects_short_blob():
    cipher = AesGcmCipher(_key())
    with pytest.raises(ValueError, match="too short"):
        cipher.decrypt(b"\x01\x02\x03")


def test_decrypt_with_wrong_key_fails():
    a = AesGcmCipher(_key())
    b = AesGcmCipher(_key())
    blob = a.encrypt(b"payload")
    with pytest.raises(ValueError, match="authentication"):
        b.decrypt(blob)


def test_associated_data_must_match():
    cipher = AesGcmCipher(_key())
    blob = cipher.encrypt(b"pt", associated_data=b"user-alice")
    assert cipher.decrypt(blob, associated_data=b"user-alice") == b"pt"
    with pytest.raises(ValueError, match="authentication"):
        cipher.decrypt(blob, associated_data=b"user-bob")


def test_cipher_rejects_short_key():
    with pytest.raises(MasterKeyError):
        AesGcmCipher(b"\x00" * 16)


def test_master_key_dataclass_validates_length():
    with pytest.raises(MasterKeyError):
        MasterKey(key=b"\x00" * 16, source="generated")


def test_load_master_key_from_env():
    raw = secrets.token_bytes(MASTER_KEY_BYTES)
    encoded = base64.urlsafe_b64encode(raw).decode("ascii")
    mk = load_or_create_master_key(env={MASTER_KEY_ENV: encoded}, path="/nonexistent")
    assert mk.key == raw
    assert mk.source == "env"


def test_load_master_key_env_padding_tolerant():
    raw = secrets.token_bytes(MASTER_KEY_BYTES)
    encoded = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    mk = load_or_create_master_key(env={MASTER_KEY_ENV: encoded}, path="/nonexistent")
    assert mk.key == raw


def test_load_master_key_env_invalid_b64():
    with pytest.raises(MasterKeyError, match="urlsafe-base64"):
        load_or_create_master_key(env={MASTER_KEY_ENV: "!!!not-base64!!!"}, path="/nope")


def test_load_master_key_env_wrong_length():
    bad = base64.urlsafe_b64encode(b"\x00" * 16).decode("ascii")
    with pytest.raises(MasterKeyError, match="32 bytes"):
        load_or_create_master_key(env={MASTER_KEY_ENV: bad}, path="/nope")


def test_load_master_key_from_file(tmp_path):
    raw = secrets.token_bytes(MASTER_KEY_BYTES)
    path = tmp_path / "master.key"
    path.write_bytes(raw)
    mk = load_or_create_master_key(env={}, path=path)
    assert mk.key == raw
    assert mk.source == "file"


def test_load_master_key_file_wrong_length(tmp_path):
    path = tmp_path / "master.key"
    path.write_bytes(b"\x00" * 8)
    with pytest.raises(MasterKeyError, match="exactly"):
        load_or_create_master_key(env={}, path=path)


def test_load_master_key_generate_when_missing(tmp_path):
    path = tmp_path / "subdir" / "master.key"
    mk = load_or_create_master_key(env={}, path=path)
    assert mk.source == "generated"
    assert path.read_bytes() == mk.key
    assert len(mk.key) == MASTER_KEY_BYTES
    if os.name == "posix":
        mode = path.stat().st_mode & 0o777
        assert mode == 0o600


def test_load_master_key_no_generate_raises(tmp_path):
    with pytest.raises(MasterKeyError, match="no master key"):
        load_or_create_master_key(
            env={}, path=tmp_path / "nope.key", generate_if_missing=False,
        )


def test_load_master_key_generate_does_not_clobber(tmp_path):
    path = tmp_path / "master.key"
    raw = secrets.token_bytes(MASTER_KEY_BYTES)
    path.write_bytes(raw)
    mk = load_or_create_master_key(env={}, path=path)
    assert mk.key == raw  # the existing file wins, never overwritten


def test_rotate_re_encrypts_all_secrets(tmp_path):
    db = StateStoreV2(tmp_path / "state-v2.db")
    db.initialize()
    db.upsert_user(user_id="alice", email="a@example.invalid")
    db.upsert_user(user_id="bob", email="b@example.invalid")

    old = AesGcmCipher(_key())
    new = AesGcmCipher(_key())

    a_token = b"alice-immich-token"
    b_token = b"bob-immich-token"
    a_apikey = b"alice-api-key"

    a = db.with_user("alice")
    b = db.with_user("bob")
    a.create_session(
        session_id="sa",
        encrypted_immich_token=old.encrypt(a_token),
        expires_at="2099-01-01T00:00:00Z",
    )
    b.create_session(
        session_id="sb",
        encrypted_immich_token=old.encrypt(b_token),
        expires_at="2099-01-01T00:00:00Z",
    )
    a.store_api_key(encrypted_key=old.encrypt(a_apikey), label="ak")

    counts = rotate_encrypted_columns(db._conn, old_cipher=old, new_cipher=new)
    assert counts == {"sessions": 2, "user_api_keys": 1}

    sessions = {row["session_id"]: row for row in a.list_sessions() + b.list_sessions()}
    assert new.decrypt(bytes(sessions["sa"]["encrypted_immich_token"])) == a_token
    assert new.decrypt(bytes(sessions["sb"]["encrypted_immich_token"])) == b_token
    keys = a.list_api_keys()
    assert new.decrypt(bytes(keys[0]["encrypted_key"])) == a_apikey

    # Old cipher can no longer read any of them.
    with pytest.raises(ValueError):
        old.decrypt(bytes(sessions["sa"]["encrypted_immich_token"]))

    db.close()


def test_rotate_aborts_atomically_on_decrypt_failure(tmp_path):
    db = StateStoreV2(tmp_path / "state-v2.db")
    db.initialize()
    db.upsert_user(user_id="alice", email="a@example.invalid")

    old = AesGcmCipher(_key())
    bogus = AesGcmCipher(_key())  # not the cipher used to encrypt below
    new = AesGcmCipher(_key())

    a = db.with_user("alice")
    blob = old.encrypt(b"token")
    a.create_session(
        session_id="sa",
        encrypted_immich_token=blob,
        expires_at="2099-01-01T00:00:00Z",
    )

    with pytest.raises(ValueError):
        rotate_encrypted_columns(db._conn, old_cipher=bogus, new_cipher=new)

    # Row is unchanged; old still decrypts.
    sessions = a.list_sessions()
    assert bytes(sessions[0]["encrypted_immich_token"]) == blob
    assert old.decrypt(bytes(sessions[0]["encrypted_immich_token"])) == b"token"
    db.close()

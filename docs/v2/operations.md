# MediaRefinery v2 — operations runbook

Operator-facing procedures for the v2 service. Phase B scope only;
deployment, backup, and HTTPS topics land in Phase G.

## Master key

The v2 service encrypts at rest:

- Immich session tokens stored in `sessions.encrypted_immich_token`.
- Per-user unattended-scan API keys in `user_api_keys.encrypted_key`.

Both columns are AES-256-GCM ciphertext produced by
`mediarefinery.service.security.AesGcmCipher`.

### Resolution order at startup

1. **Environment variable `MR_MASTER_KEY`** — urlsafe-base64 of exactly
   32 random bytes. Preferred for orchestrators that already manage
   secrets (Kubernetes Secrets, systemd `LoadCredential`, Docker
   Swarm secrets).
2. **Key file `/data/master.key`** — 32 raw bytes, mode `0600`. Used
   by the default Docker Compose deployment.
3. **First-run bootstrap** — when neither source exists, the service
   generates a fresh 32-byte key with `secrets.token_bytes` and
   writes it to the configured path with `O_CREAT | O_EXCL` and mode
   `0600`. The existing file is **never** clobbered: if the file
   exists but cannot be read, startup fails loudly.

### Rotation

`MR_MASTER_KEY` rotation is an offline procedure. The service must be
stopped while it runs.

1. Generate the new key:
   ```bash
   python -c "import secrets, base64; \
     print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())"
   ```
2. Stop the service.
3. Back up `/data` (the SQLite DB and the key file).
4. Run the rotation against `state-v2.db`:
   ```python
   from mediarefinery.service.security import (
       AesGcmCipher, rotate_encrypted_columns,
   )
   from mediarefinery.service.state_v2 import StateStoreV2
   import base64

   old = AesGcmCipher(open("/data/master.key", "rb").read())
   new = AesGcmCipher(base64.urlsafe_b64decode("<NEW_KEY_B64>"))

   db = StateStoreV2("/data/state-v2.db")
   db.initialize()
   counts = rotate_encrypted_columns(db._conn, old_cipher=old, new_cipher=new)
   print(counts)  # {"sessions": ..., "user_api_keys": ...}
   db.close()
   ```
   The rotation runs in a single transaction; any decrypt failure
   aborts atomically and leaves the DB unchanged.
5. Replace the old key (write the new bytes to `/data/master.key` or
   set `MR_MASTER_KEY` in the environment), then restart the service.
6. Verify by logging in and confirming a scan still authenticates
   against Immich. Old sessions remain valid only because the rotation
   re-encrypted their tokens; if any session fails to decrypt with
   the new key, log out and back in.

### Compromise response

If `MR_MASTER_KEY` is suspected compromised:

1. Treat **all stored Immich tokens and unattended-scan API keys as
   exposed**. Operators should revoke them in Immich
   (Account Settings → Sessions, Account Settings → API Keys) before
   anything else.
2. Rotate the master key as above.
3. Force log-out of every active MR session by truncating the
   `sessions` table (the next request from each browser will be
   redirected to the login page).

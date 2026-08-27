"""Encrypt/decrypt per-tenant publish-target secrets (SFTP password / private key).

A PublishTarget's secrets live in the DB, so they end up in DB dumps — and this
app pulls + restores dumps over SSH in-app — so they must not sit in plaintext.
Encrypt them with Fernet, keying off ``DJANGO_SECRET_KEY`` so no extra env var
is required.

Rotating ``DJANGO_SECRET_KEY`` therefore invalidates every stored publish
secret: ``decrypt`` raises ``InvalidToken``, which ``resolve_config`` catches by
treating the target as not configured — re-enter the credentials in the admin
after a rotation. The same rotation also invalidates every tenant's stored
scanning API key (see ``mahj.scan_key``).

The crypto itself lives in ``mahj.secrets``, shared with scanning.
"""
from ..secrets import make_codec

# These bytes are part of the stored format, not a naming choice: they derive the
# key every existing password_enc / private_key_enc was encrypted under. Changing
# them — including "tidying" the name now that the codec is shared — makes every
# stored publish credential permanently unreadable. Leave them alone.
encrypt, decrypt, decrypt_or_blank = make_codec(
    salt=b'mahj-publish-target',
    info=b'sftp-secret',
    purpose='publish target',
)

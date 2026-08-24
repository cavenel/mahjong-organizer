"""Encrypt/decrypt per-tenant publish-target secrets (SFTP password / private key).

A PublishTarget's secrets live in the DB, so they end up in DB dumps — and this
app pulls + restores dumps over SSH in-app — so they must not sit in plaintext.
Encrypt them with Fernet, keying off ``DJANGO_SECRET_KEY`` so no extra env var
is required.

Rotating ``DJANGO_SECRET_KEY`` therefore invalidates every stored publish
secret: ``decrypt`` raises ``InvalidToken``, which ``resolve_config`` catches by
treating the target as not configured — re-enter the credentials in the admin
after a rotation. ``cryptography`` is already
a dependency (paramiko pulls it in), so nothing new is added.
"""
import base64

from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from django.conf import settings


def _fernet():
    # Derive a stable 32-byte key from the Django secret. HKDF (not the raw
    # secret) so the Fernet key doesn't leak the secret and stays 32 bytes
    # regardless of the secret's length.
    key = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=b'mahj-publish-target',
        info=b'sftp-secret',
    ).derive(settings.SECRET_KEY.encode('utf-8'))
    return Fernet(base64.urlsafe_b64encode(key))


def encrypt(text):
    """Encrypt a secret string → token bytes. Empty/None → None (store nothing)."""
    if not text:
        return None
    return _fernet().encrypt(text.encode('utf-8'))


def decrypt(token):
    """Decrypt token bytes (BinaryField, possibly a memoryview) → string.
    None/empty → ''."""
    if not token:
        return ''
    return _fernet().decrypt(bytes(token)).decode('utf-8')

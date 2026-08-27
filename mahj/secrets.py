"""Per-purpose symmetric encryption for the tenant secrets kept in the DB.

Two features store a credential on a tenant row — the SFTP publish target and
the score-sheet scanning API key — and both must survive a DB dump landing
somewhere it shouldn't. Each gets its own Fernet key, derived from
``DJANGO_SECRET_KEY`` via HKDF with a purpose-specific salt/info pair, so one
purpose's ciphertext is not substitutable for the other's and no extra env var
is required.

Rotating ``DJANGO_SECRET_KEY`` therefore invalidates every stored secret:
``decrypt`` raises ``InvalidToken``. Callers on a request path should use
``decrypt_or_blank`` and treat '' as "not configured", so a rotation degrades
the feature instead of 500ing the page — see ``publish.sftp_upload.resolve_config``
and ``scan_key.resolve_key``.

``cryptography`` is a direct dependency (see requirements/base.txt).
"""
import base64
import logging

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from django.conf import settings

logger = logging.getLogger(__name__)


def make_codec(salt, info, purpose=''):
    """Build (encrypt, decrypt, decrypt_or_blank) for one purpose.

    `salt` and `info` are the HKDF parameters that separate this purpose's key
    from every other one. They are part of the stored data's format: change them
    and every value already encrypted under them becomes unreadable. `purpose`
    is only used in the log line decrypt_or_blank emits.
    """

    def _fernet():
        # Derive a stable 32-byte key from the Django secret. HKDF (not the raw
        # secret) so the Fernet key doesn't leak the secret and stays 32 bytes
        # regardless of the secret's length.
        key = HKDF(
            algorithm=hashes.SHA256(),
            length=32,
            salt=salt,
            info=info,
        ).derive(settings.SECRET_KEY.encode('utf-8'))
        return Fernet(base64.urlsafe_b64encode(key))

    def encrypt(text):
        """Encrypt a secret string → token bytes. Empty/None → None (store nothing)."""
        if not text:
            return None
        return _fernet().encrypt(text.encode('utf-8'))

    def decrypt(token):
        """Decrypt token bytes (BinaryField, possibly a memoryview) → string.
        None/empty → ''. Raises InvalidToken if the secret key has rotated."""
        if not token:
            return ''
        return _fernet().decrypt(bytes(token)).decode('utf-8')

    def decrypt_or_blank(token):
        """decrypt(), but an unreadable value is '' rather than an exception.

        For callers where "the stored secret can no longer be read" and "there is
        no stored secret" should behave identically: a DJANGO_SECRET_KEY rotation
        then disables the feature and asks for re-entry, instead of raising on
        every request or on every queued job.
        """
        try:
            return decrypt(token)
        except InvalidToken:
            logger.warning(
                "A stored %s secret no longer decrypts — DJANGO_SECRET_KEY was "
                "probably rotated. Treating it as not configured; re-enter it in "
                "the admin.", purpose or 'tenant')
            return ''

    return encrypt, decrypt, decrypt_or_blank

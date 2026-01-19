"""
Credential storage with optional encryption.

Provides secure credential storage with machine-specific encryption when the
cryptography library is available. Falls back to plain JSON with restrictive
file permissions when encryption is unavailable.

Threat Model:
- Protects against: Accidental file exposure, cloud backup leaks, credentials
  copied to other machines
- Does NOT protect against: Malware on the same machine, local attackers with
  user-level access

The encryption key is derived from the machine's MAC address and an app-specific
salt. This provides defense-in-depth security without requiring user passwords.
"""

import json
import logging
import os
import uuid
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger("credential_store")

# Encryption constants
CREDENTIAL_VERSION = 1
ENCRYPTION_SALT = b"jellyfin-mpv-shim-credential-v1"
PBKDF2_ITERATIONS = 100000

# Try to import cryptography (optional dependency)
ENCRYPTION_AVAILABLE = False
try:
    from cryptography.fernet import Fernet, InvalidToken
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
    import base64

    ENCRYPTION_AVAILABLE = True
except ImportError:
    log.warning(
        "Cryptography library not available. Credentials will be stored in plain text. "
        "Install with: pip install jellyfin-mpv-shim[secure]"
    )


def _get_machine_id() -> bytes:
    """
    Get machine-specific identifier for key derivation.
    Uses MAC address as stable machine identifier.

    Returns:
        bytes: Machine identifier
    """
    # uuid.getnode() returns MAC address as integer
    # Convert to bytes for key derivation
    mac_int = uuid.getnode()
    return mac_int.to_bytes(6, byteorder="big")


def _derive_encryption_key() -> Optional[bytes]:
    """
    Derive encryption key from machine ID and app salt.

    Returns:
        Optional[bytes]: Base64-encoded Fernet key, or None if encryption unavailable
    """
    if not ENCRYPTION_AVAILABLE:
        return None

    machine_id = _get_machine_id()

    # Use PBKDF2 to derive key from machine ID + salt
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=ENCRYPTION_SALT,
        iterations=PBKDF2_ITERATIONS,
    )
    key = kdf.derive(machine_id)

    # Fernet requires base64-encoded 32-byte key
    return base64.urlsafe_b64encode(key)


def _encrypt_data(data: List[Dict[str, Any]]) -> Optional[str]:
    """
    Encrypt credential data using Fernet symmetric encryption.

    Args:
        data: List of credential dictionaries

    Returns:
        Optional[str]: Base64-encoded encrypted data, or None if encryption fails
    """
    if not ENCRYPTION_AVAILABLE:
        return None

    try:
        key = _derive_encryption_key()
        if not key:
            return None

        fernet = Fernet(key)
        json_data = json.dumps(data)
        encrypted = fernet.encrypt(json_data.encode("utf-8"))

        # Return base64-encoded encrypted data
        return encrypted.decode("utf-8")
    except Exception as e:
        log.error(f"Encryption failed: {e}", exc_info=True)
        return None


def _decrypt_data(encrypted_data: str) -> Optional[List[Dict[str, Any]]]:
    """
    Decrypt credential data.

    Args:
        encrypted_data: Base64-encoded encrypted data

    Returns:
        Optional[List[Dict]]: Decrypted credential list, or None if decryption fails
    """
    if not ENCRYPTION_AVAILABLE:
        return None

    try:
        key = _derive_encryption_key()
        if not key:
            return None

        fernet = Fernet(key)
        decrypted = fernet.decrypt(encrypted_data.encode("utf-8"))
        return json.loads(decrypted.decode("utf-8"))
    except InvalidToken:
        log.warning("Failed to decrypt credentials (wrong machine or corrupted file)")
        return None
    except Exception as e:
        log.error(f"Decryption failed: {e}", exc_info=True)
        return None


def _detect_format(file_path: str) -> Tuple[str, Any]:
    """
    Detect credential file format (encrypted vs plain JSON).

    Args:
        file_path: Path to credential file

    Returns:
        Tuple[str, Any]: ("encrypted"|"plain"|"invalid"|"missing", data or None)
    """
    try:
        with open(file_path, "r") as f:
            content = f.read().strip()

        # Try to parse as JSON first
        try:
            data = json.loads(content)
            # Plain JSON format
            return ("plain", data)
        except json.JSONDecodeError:
            # Not JSON, might be encrypted
            if ENCRYPTION_AVAILABLE:
                decrypted = _decrypt_data(content)
                if decrypted is not None:
                    return ("encrypted", decrypted)

            return ("invalid", None)
    except FileNotFoundError:
        return ("missing", None)
    except Exception as e:
        log.error(f"Error reading credential file: {e}", exc_info=True)
        return ("invalid", None)


def _set_secure_permissions(file_path: str) -> None:
    """
    Set file permissions to user-only read/write (0600).

    Args:
        file_path: Path to file to secure
    """
    try:
        # Set permissions to 0600 (user read/write only)
        # This works on Unix-like systems (Linux, macOS)
        import stat

        os.chmod(file_path, stat.S_IRUSR | stat.S_IWUSR)
        log.debug(f"Set secure permissions (0600) on {file_path}")
    except Exception as e:
        # Windows doesn't support chmod the same way
        # This is okay - encryption provides the protection
        log.debug(f"Could not set file permissions: {e}")


def load_credentials(file_path: str) -> List[Dict[str, Any]]:
    """
    Load credentials from file, handling both encrypted and plain formats.

    Args:
        file_path: Path to credential file

    Returns:
        List[Dict]: List of credential dictionaries (empty list if file doesn't exist)
    """
    format_type, data = _detect_format(file_path)

    if format_type == "missing":
        log.debug("No credential file found, starting fresh")
        return []

    if format_type == "invalid":
        log.error(f"Credential file is corrupted or invalid: {file_path}")
        return []

    if format_type == "plain":
        log.info("Loaded plain-text credentials (will encrypt on next save)")
        return data if isinstance(data, list) else data

    if format_type == "encrypted":
        log.info("Loaded encrypted credentials")
        return data

    return []


def save_credentials(file_path: str, credentials: List[Dict[str, Any]]) -> bool:
    """
    Save credentials to file with encryption if available.

    Args:
        file_path: Path to credential file
        credentials: List of credential dictionaries

    Returns:
        bool: True if save successful, False otherwise
    """
    try:
        # Try to encrypt first
        encrypted_data = _encrypt_data(credentials)

        if encrypted_data:
            # Save encrypted format
            with open(file_path, "w") as f:
                f.write(encrypted_data)
            log.info("Saved encrypted credentials")
        else:
            # Fallback to plain JSON
            with open(file_path, "w") as f:
                json.dump(credentials, f)
            log.warning(
                "Saved credentials in plain text. "
                "Install cryptography for encryption: pip install jellyfin-mpv-shim[secure]"
            )

        # Set secure file permissions
        _set_secure_permissions(file_path)

        return True
    except Exception as e:
        log.error(f"Failed to save credentials: {e}", exc_info=True)
        return False

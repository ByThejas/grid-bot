"""
Generate an Ed25519 key pair for Binance WebSocket API authentication.

WHY this exists:
Binance retired HMAC-based `userDataStream.start` on testnet. The only
supported path for user data streams is `session.logon` over the WS API,
which requires an Ed25519-signed request. This script produces a key
pair locally — the private key never leaves your machine.

OUTPUT
- secrets/binance_ed25519_private.pem  ← stays on disk, referenced by the bot
- secrets/binance_ed25519_public.pem   ← upload the CONTENTS to Binance

USAGE
    python -m scripts.generate_ed25519_keypair

SAFETY
- `*.pem` is already in .gitignore, so these files won't be committed.
- The private key is PKCS8 with no passphrase — suitable for testnet. If
  you ever point this at mainnet, add a passphrase and teach the loader.
- Testnet keys are disposable. If you suspect a leak, regenerate and
  re-register; nothing at risk is real money.
"""

import pathlib

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
)


OUT_DIR = pathlib.Path("secrets")
PRIVATE_PATH = OUT_DIR / "binance_ed25519_private.pem"
PUBLIC_PATH = OUT_DIR / "binance_ed25519_public.pem"


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    if PRIVATE_PATH.exists() or PUBLIC_PATH.exists():
        print("! Key files already exist. Delete them first if you want to regenerate:")
        print(f"    {PRIVATE_PATH}")
        print(f"    {PUBLIC_PATH}")
        return

    private_key = Ed25519PrivateKey.generate()

    private_pem = private_key.private_bytes(
        encoding=Encoding.PEM,
        format=PrivateFormat.PKCS8,
        encryption_algorithm=NoEncryption(),
    )
    public_pem = private_key.public_key().public_bytes(
        encoding=Encoding.PEM,
        format=PublicFormat.SubjectPublicKeyInfo,
    )

    PRIVATE_PATH.write_bytes(private_pem)
    PUBLIC_PATH.write_bytes(public_pem)

    print(f"Wrote private key → {PRIVATE_PATH}")
    print(f"Wrote public key  → {PUBLIC_PATH}")
    print()
    print("-" * 60)
    print("PUBLIC KEY (copy this entire block into the Binance testnet")
    print("portal when creating the Ed25519 API key):")
    print("-" * 60)
    print(public_pem.decode().rstrip())
    print("-" * 60)


if __name__ == "__main__":
    main()

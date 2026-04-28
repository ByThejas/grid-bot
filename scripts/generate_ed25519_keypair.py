"""
Generate an Ed25519 key pair for Binance WebSocket API authentication.

WHY this exists:
Binance retired HMAC-based `userDataStream.start`. The only supported path
for user data streams is `session.logon` over the WS API, which requires
an Ed25519-signed request. This script produces a key pair locally — the
private key never leaves your machine.

OUTPUT (testnet, default)
- secrets/binance_ed25519_private.pem            ← stays on disk, referenced by the bot
- secrets/binance_ed25519_public.pem             ← upload the CONTENTS to Binance testnet

OUTPUT (mainnet, --env mainnet)
- secrets/binance_ed25519_mainnet_private.pem    ← stays on disk, referenced by the bot
- secrets/binance_ed25519_mainnet_public.pem     ← upload the CONTENTS to Binance mainnet

USAGE
    python -m scripts.generate_ed25519_keypair                  # testnet
    python -m scripts.generate_ed25519_keypair --env mainnet    # mainnet

SAFETY
- `*.pem` is already in .gitignore, so these files won't be committed.
- The private key is PKCS8 with no passphrase. Testnet keys are disposable.
  Mainnet keys gate real-money order placement — keep `secrets/` out of any
  cloud-sync folder and back up the file somewhere offline.
- If you suspect a key leak, regenerate, re-register on Binance, and update
  .env. The old key is invalidated server-side as soon as you replace it.
"""

import argparse
import pathlib
import sys

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
)


OUT_DIR = pathlib.Path("secrets")


def _paths(env: str) -> tuple[pathlib.Path, pathlib.Path]:
    if env == "mainnet":
        return (
            OUT_DIR / "binance_ed25519_mainnet_private.pem",
            OUT_DIR / "binance_ed25519_mainnet_public.pem",
        )
    return (
        OUT_DIR / "binance_ed25519_private.pem",
        OUT_DIR / "binance_ed25519_public.pem",
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--env",
        choices=("testnet", "mainnet"),
        default="testnet",
        help="Which environment these keys are for (changes the output filenames).",
    )
    args = parser.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    private_path, public_path = _paths(args.env)

    if private_path.exists() or public_path.exists():
        print(f"! Key files already exist for env={args.env}. Delete them first if you want to regenerate:")
        print(f"    {private_path}")
        print(f"    {public_path}")
        sys.exit(1)

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

    private_path.write_bytes(private_pem)
    public_path.write_bytes(public_pem)

    label = "MAINNET" if args.env == "mainnet" else "TESTNET"
    print(f"Wrote private key → {private_path}")
    print(f"Wrote public key  → {public_path}")
    print()
    print("-" * 60)
    print(f"PUBLIC KEY (copy this entire block into the Binance {label}")
    print("portal when creating the Ed25519 API key):")
    print("-" * 60)
    print(public_pem.decode().rstrip())
    print("-" * 60)


if __name__ == "__main__":
    main()

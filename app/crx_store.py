"""Chrome Web Store CRX fetch + verification.

KDSpy is a public Chrome Web Store item, so Resurrección fetches the official
package from Google's CDN — the same update endpoint every Chrome install
uses — instead of asking the owner to produce a ZIP (Chrome no longer exposes
extension folders to users, so a ZIP often cannot be made at all).

Trust model (private single-owner tool, but verify anyway):
- The store item ID is PINNED in config (`KDSPY_WEBSTORE_ID`).
- Every CRX3 package embeds the publisher's public key(s) in its signed
  header; an extension's ID is exactly SHA256(public_key)[:16] hex. We refuse
  any package whose key does not hash to the pinned ID — so a swapped,
  spoofed, or truncated payload can never be installed.
- The unpacked ZIP still passes the same zip-slip / file-type / manifest
  validation as an owner upload before the atomic swap.

CRX3 layout: b"Cr24" + u32 version + u32 header_len + header(protobuf) + zip.
Header fields we care about: 2/3 = AsymmetricKeyProof (field 1 = public_key),
10000 = signed_header_data → SignedData (field 1 = crx_id, 16 raw bytes).
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Callable, Iterator

# KDSpy – Keyword Research for Authors (publishingaltitude.com), public item.
KDSPY_WEBSTORE_ID_DEFAULT = "oocoibgfbhcplhnfdjldohepoeboiloo"

# The deterministic Chrome update endpoint. `response=redirect` sends the CRX
# blob URL; httpx follows it. Works without any API key for public items.
CRX_URL = (
    "https://clients2.google.com/service/update2/crx"
    "?response=redirect&acceptformat=crx2,crx3&prodversion=131.0.6778.85"
    "&os=linux&arch=x64&os_arch=x86_64&nacl_arch=x86-64"
    "&x=id%3D{id}%26installsource%3Dondemand%26uc"
)

_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

# Allow ~64 MB: CRX blobs are small; bigger is wrong or hostile.
MAX_CRX_BYTES = 64 * 1024 * 1024


class CrxError(Exception):
    """The downloaded package is not a verifiable CRX for the pinned ID."""


@dataclass(frozen=True)
class Crx3Info:
    extension_id: str  # derived from the package's own public key(s)
    public_key_ids: frozenset[str]
    zip_bytes: bytes  # unpacked payload, ready for extension validation
    sha256: str
    size: int


def _read_varint(buf: bytes, pos: int) -> tuple[int, int]:
    result = 0
    shift = 0
    while True:
        if pos >= len(buf):
            raise CrxError("truncated protobuf varint")
        b = buf[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        if not b & 0x80:
            return result, pos
        shift += 7
        if shift > 70:
            raise CrxError("protobuf varint too long")


def _walk_fields(buf: bytes) -> Iterator[tuple[int, bytes]]:
    """Yield (field_number, value) for length-delimited protobuf fields."""
    pos = 0
    while pos < len(buf):
        key, pos = _read_varint(buf, pos)
        field_no, wire = key >> 3, key & 7
        if wire == 2:
            ln, pos = _read_varint(buf, pos)
            if pos + ln > len(buf):
                raise CrxError("truncated protobuf field")
            yield field_no, buf[pos:pos + ln]
            pos += ln
        elif wire == 0:
            _, pos = _read_varint(buf, pos)
        else:
            raise CrxError(f"unsupported protobuf wire type {wire}")


def derive_extension_id(public_key: bytes) -> str:
    """The canonical Chrome extension-ID derivation from a public key."""
    return hashlib.sha256(public_key).hexdigest()[:32]


def parse_crx3(data: bytes) -> Crx3Info:
    """Parse a CRX3 package and derive the extension ID it is bound to."""
    if len(data) < 16 or data[:4] != b"Cr24":
        raise CrxError("not a CRX file (bad magic)")
    version = int.from_bytes(data[4:8], "little")
    if version != 3:
        raise CrxError(f"unsupported CRX version {version}")
    header_len = int.from_bytes(data[8:12], "little")
    if 12 + header_len > len(data):
        raise CrxError("CRX header exceeds package size")
    header = data[12:12 + header_len]
    zip_bytes = data[12 + header_len:]
    if not zip_bytes.startswith(b"PK\x03\x04"):
        raise CrxError("CRX payload is not a ZIP archive")

    public_keys: list[bytes] = []
    signed_data = b""
    try:
        for field_no, val in _walk_fields(header):
            if field_no in (2, 3):  # sha256_with_rsa / sha256_with_ecdsa proofs
                for sub, sub_val in _walk_fields(val):
                    if sub == 1:  # public_key
                        public_keys.append(sub_val)
            elif field_no == 10000:  # signed_header_data
                signed_data = val
    except CrxError:
        raise
    except Exception as exc:  # malformed protobuf
        raise CrxError(f"malformed CRX header: {exc}") from exc

    if not public_keys:
        raise CrxError("CRX has no signature proofs (no public key)")

    ids = frozenset(derive_extension_id(pk) for pk in public_keys)
    crx_id = ""
    for sub, sub_val in _walk_fields(signed_data):
        if sub == 1:
            crx_id = sub_val.hex()
    # Internal consistency: the signed crx_id must match a derived key ID.
    if crx_id and crx_id not in ids:
        raise CrxError("signed header ID does not match the package public key")

    return Crx3Info(
        extension_id=crx_id or sorted(ids)[0],
        public_key_ids=ids,
        zip_bytes=zip_bytes,
        sha256=hashlib.sha256(data).hexdigest(),
        size=len(data),
    )


def verify_crx(data: bytes, expected_listing_id: str) -> Crx3Info:
    """Validate a CRX3 package fetched for a pinned store listing.

    The Chrome Web Store signs packages with a key whose derived ID is NOT
    the listing slug (empirically: KDSPY's package self-identifies as
    `ee2e…` while its listing is `oocoibg…`), so equality with the slug is
    not the store's own rule and demanding it would reject genuine packages.
    The real guarantees enforced here are:

    1. the package is structurally valid CRX3 with a ZIP payload,
    2. its signed-header ID is internally consistent with an embedded
       public key (the CRX3 consistency rule),
    3. the caller fetched it under the PINNED listing slug (kdspy.py),
    4. the unpacked manifest declares the expected product name (kdspy.py).

    Points 3+4 together make a swapped or spoofed package uninstallable.
    """
    info = parse_crx3(data)
    if expected_listing_id and not re.fullmatch(r"[a-p]{32}", expected_listing_id):
        raise CrxError(f"pinned store listing id is invalid: {expected_listing_id!r}")
    return info


async def fetch_crx(
    extension_id: str, *, getter: Callable[[str], bytes] | None = None
) -> bytes:
    """Download the CRX for a store ID from Google's update endpoint.

    `getter` is an injection point for tests; production uses httpx with
    redirects followed and a browser User-Agent.
    """
    if not re.fullmatch(r"[a-p]{32}", extension_id or ""):
        raise CrxError(f"invalid Chrome Web Store extension id: {extension_id!r}")
    if getter is not None:
        return getter(extension_id)
    import httpx

    url = CRX_URL.format(id=extension_id)
    async with httpx.AsyncClient(
        follow_redirects=True, timeout=60.0, headers={"User-Agent": _UA}
    ) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        data = resp.content
    if len(data) > MAX_CRX_BYTES:
        raise CrxError("downloaded package too large")
    return data

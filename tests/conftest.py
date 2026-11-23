from dataclasses import replace

import pytest

from dj_sync.models import Track


@pytest.fixture
def source():
    return Track(
        "spotify",
        "s1",
        "Night Drive - Extended Mix",
        ("Artist",),
        420000,
        "USAAA2600001",
        available=True,
        explicit=False,
    )


@pytest.fixture
def target(source):
    return replace(source, service="tidal", id="t1", title="Night Drive", version="Extended Mix")


def write_aiff(path, *, audio=b"\x00\x01" * 8, cover_encoding=None, tidal_id=None, cover=None):
    """A tiny valid AIFF, optionally tagged like the converter: ID3v2.3, cover, TIDAL URL."""
    import struct

    from mutagen.aiff import AIFF
    from mutagen.id3 import APIC, TIT2, TXXX

    rate_44100 = bytes.fromhex("400EAC44000000000000")
    comm = struct.pack(">hIh", 1, len(audio) // 2, 16) + rate_44100
    ssnd = struct.pack(">II", 0, 0) + audio
    body = b"AIFF" + b"COMM" + struct.pack(">I", len(comm)) + comm
    body += b"SSND" + struct.pack(">I", len(ssnd)) + ssnd
    path.write_bytes(b"FORM" + struct.pack(">I", len(body)) + body)
    if cover_encoding is None and tidal_id is None:
        return path
    a = AIFF(path)
    a.add_tags()
    a.tags.add(TIT2(encoding=3, text="Song"))
    if tidal_id is not None:
        a.tags.add(
            TXXX(encoding=3, desc="TIDAL URL", text=f"https://tidal.com/browse/track/{tidal_id}")
        )
    if cover_encoding is not None:
        data = cover or b"\xff\xd8\xff\xe0cover"
        a.tags.add(APIC(encoding=cover_encoding, mime="image/jpeg", type=3, desc="", data=data))
    a.save(v2_version=3)
    return path


@pytest.fixture
def make_aiff():
    return write_aiff

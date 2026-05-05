"""Intelligent Mail Barcode (IMb) encoder.

Adapted from the Python implementation originally written by Sam Rushing
and bundled in https://github.com/1997cui/envelope. Released by the
upstream author under the Simplified BSD License
(https://www.opensource.org/licenses/bsd-license.html).

Spec: USPS Publication SPUSPSG (
https://ribbs.usps.gov/intelligentmail_mailpieces/documents/tech_guides/SPUSPSG.pdf
).

To render the barcode visually, install the USPSIMBStandard TrueType font
(bundled under static/) and apply it to the encoded letter sequence.

Letter encoding:
    'A' Ascender, 'D' Descender, 'F' Full, 'T' Tracker (neither).
"""

from __future__ import annotations

from functools import lru_cache

_GEN_POLY = 0x0F35


def _crc11(data_bytes: list[int]) -> int:
    fcs = 0x07FF
    data = data_bytes[0] << 5
    for _ in range(2, 8):
        if (fcs ^ data) & 0x400:
            fcs = (fcs << 1) ^ _GEN_POLY
        else:
            fcs = fcs << 1
        fcs &= 0x7FF
        data <<= 1
    for byte_index in range(1, 13):
        data = data_bytes[byte_index] << 3
        for _ in range(8):
            if (fcs ^ data) & 0x400:
                fcs = (fcs << 1) ^ _GEN_POLY
            else:
                fcs = fcs << 1
            fcs &= 0x7FF
            data <<= 1
    return fcs


def _reverse_int16(value: int) -> int:
    out = 0
    for _ in range(16):
        out <<= 1
        out |= value & 1
        value >>= 1
    return out


def _init_n_of_13(n: int, table_length: int) -> dict[int, int]:
    table: dict[int, int] = {}
    index_low = 0
    index_hi = table_length - 1
    for i in range(8192):
        if bin(i).count("1") != n:
            continue
        reverse = _reverse_int16(i) >> 3
        if reverse < i:
            continue
        if i == reverse:
            table[index_hi] = i
            index_hi -= 1
        else:
            table[index_low] = i
            index_low += 1
            table[index_low] = reverse
            index_low += 1
    if index_low != index_hi + 1:
        raise ValueError("IMb table generation failed")
    return table


def _binary_to_codewords(n: int) -> list[int]:
    out: list[int] = []
    n, x = divmod(n, 636)
    out.append(x)
    for _ in range(9):
        n, x = divmod(n, 1365)
        out.append(x)
    out.reverse()
    return out


def _convert_routing_code(zip_code: str) -> int:
    if len(zip_code) == 0:
        return 0
    if len(zip_code) == 5:
        return int(zip_code) + 1
    if len(zip_code) == 9:
        return int(zip_code) + 100000 + 1
    if len(zip_code) == 11:
        return int(zip_code) + 1000000000 + 100000 + 1
    raise ValueError(f"invalid routing code length: {zip_code!r}")


def _convert_tracking_code(enc: int, track: str) -> int:
    if len(track) != 20:
        raise ValueError("tracking code must be 20 digits")
    enc = (enc * 10) + int(track[0])
    enc = (enc * 5) + int(track[1])
    for i in range(2, 20):
        enc = (enc * 10) + int(track[i])
    return enc


def _to_bytes(val: int, nbytes: int) -> list[int]:
    out: list[int] = []
    for _ in range(nbytes):
        out.append(val & 0xFF)
        val >>= 8
    out.reverse()
    return out


@lru_cache(maxsize=1)
def _bar_tables() -> tuple[dict[int, tuple[int, int]], dict[int, tuple[int, int]]]:
    bar_table = [
        "H 2 E 3", "B 10 A 0", "J 12 C 8", "F 5 G 11", "I 9 D 1",
        "A 1 F 12", "C 5 B 8", "E 4 J 11", "G 3 I 10", "D 9 H 6",
        "F 11 B 4", "I 5 C 12", "J 10 A 2", "H 1 G 7", "D 6 E 9",
        "A 3 I 6", "G 4 C 7", "B 1 J 9", "H 10 F 2", "E 0 D 8",
        "G 2 A 4", "I 11 B 0", "J 8 D 12", "C 6 H 7", "F 1 E 10",
        "B 12 G 9", "H 3 I 0", "F 8 J 7", "E 6 C 10", "D 4 A 5",
        "I 4 F 7", "H 11 B 9", "G 0 J 6", "A 6 E 8", "C 1 D 2",
        "F 9 I 12", "E 11 G 1", "J 5 H 4", "D 3 B 2", "A 7 C 0",
        "B 3 E 1", "G 10 D 5", "I 7 J 4", "C 11 F 6", "A 8 H 12",
        "E 2 I 1", "F 10 D 0", "J 3 A 9", "G 5 C 4", "H 8 B 7",
        "F 0 E 5", "C 3 A 10", "G 12 J 2", "D 11 B 6", "I 8 H 9",
        "F 4 A 11", "B 5 C 2", "J 1 E 12", "I 3 G 6", "H 0 D 7",
        "E 7 H 5", "A 12 B 11", "C 9 J 0", "G 8 F 3", "D 10 I 2",
    ]  # fmt: skip
    table_a: dict[int, tuple[int, int]] = {}
    table_d: dict[int, tuple[int, int]] = {}
    for i in range(65):
        i0_s, d_s, i1_s, a_s = bar_table[i].split()
        table_d[i] = (ord(i0_s) - 65, int(d_s))
        table_a[i] = (ord(i1_s) - 65, int(a_s))
    return table_a, table_d


@lru_cache(maxsize=1)
def _codeword_tables() -> tuple[dict[int, int], dict[int, int]]:
    return _init_n_of_13(5, 1287), _init_n_of_13(2, 78)


def _make_bars(code: list[int]) -> str:
    table_a, table_d = _bar_tables()
    out: list[str] = []
    for i in range(65):
        a_idx, a_bit = table_a[i]
        d_idx, d_bit = table_d[i]
        ascend = (code[a_idx] & (1 << a_bit)) != 0
        descend = (code[d_idx] & (1 << d_bit)) != 0
        out.append("TADF"[descend << 1 | ascend])
    return "".join(out)


def encode(
    barcode_id: int,
    service_type_id: int,
    mailer_id: int,
    serial: int,
    delivery: str,
) -> str:
    """Encode an IMb tracking code.

    delivery: 0, 5, 9, or 11 digit routing code (zip5, zip5+zip4, or zip5+zip4+dp).
    Returns the 65-character A/D/F/T letter sequence.
    """
    n = _convert_routing_code(delivery)
    if str(mailer_id)[0] == "9":
        tracking = f"{barcode_id:02d}{service_type_id:03d}{mailer_id:09d}{serial:06d}"
    else:
        tracking = f"{barcode_id:02d}{service_type_id:03d}{mailer_id:06d}{serial:09d}"
    n = _convert_tracking_code(n, tracking)

    fcs = _crc11(_to_bytes(n, 13))
    codewords = _binary_to_codewords(n)
    codewords[9] *= 2
    if fcs & (1 << 10):
        codewords[0] += 659

    tab5, tab2 = _codeword_tables()
    encoded: list[int] = []
    for b in codewords:
        if b < 1287:
            encoded.append(tab5[b])
        elif 127 <= b <= 1364:
            encoded.append(tab2[b - 1287])
        else:
            raise ValueError(f"codeword out of range: {b}")
    for i in range(10):
        if fcs & (1 << i):
            encoded[i] = encoded[i] ^ 0x1FFF
    return _make_bars(encoded)


def human_readable(
    barcode_id: int,
    service_type_id: int,
    mailer_id: int,
    serial: int,
    delivery: str,
) -> str:
    """Produce the dash-separated human-readable IMb representation."""
    return f"{barcode_id:02d}-{service_type_id:03d}-{mailer_id:d}-{serial:06d}-{delivery}"


def to_raw_imb(
    barcode_id: int,
    service_type_id: int,
    mailer_id: int,
    serial: int,
    delivery: str,
) -> str:
    """Concatenated digits used as the IMb tracking key in USPS APIs."""
    return f"{barcode_id:02d}{service_type_id:03d}{mailer_id:d}{serial:06d}{delivery}"

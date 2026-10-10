"""Colors ARM wire transform, recovered from Colors.exe e9a08/e9ae4.
This is legacy obfuscation, not transport authentication or modern encryption.
"""
import base64
import binascii
import xml.etree.ElementTree as ET
KEY = bytes.fromhex("2923be84e16cd6ae529049f1f1bbe9ebb3a6db3c870c3e99245e0d1c06b747de")
ROTATIONS = bytes.fromhex("0302050003030306070302050100050705040304060505030305010204030602")
FEEDBACK = bytes.fromhex("209a50ee407836fd124932f69e7ddcad4f14f24466d06bc430b73ba122919de18b1fdab0ca9902b9722c807ec5d5e9b2eac9cc53bf67d62d8e83ef5761ff698fcdd11e9c16e61df0844a77d7e839cb33740cf4d49fa45935cfd3904875d92ae5c0f72b81870e5f008dbe7b051507821870926454ceb1853ef8466a0496732f6876fabb11ae248879fe52db25433c45d8280bf5603d97278ac208a5c18ca9f1959ba8a786b5e72355386c5e4e71e2b44765897a6326df296d62e034a63fe303410f1bf3eba0064d7faa5b13fcb80d3a104cec31427c5adee42193b3af6f09011756c6c8f91c37bd6e5cc3a398c75db651192ebc944b58d2ac1a0aa2edfbddbaab")
MAX_XML = 2 * 1024 * 1024


def transform(data: bytes, decrypt: bool = False) -> bytes:
    key = bytearray(KEY)
    previous = 0
    result = bytearray()
    for index, value in enumerate(data):
        if decrypt:
            decoded = ((value - previous) & 255) ^ key[index % 32]
            previous = FEEDBACK[value]
            result.append(decoded)
        else:
            encoded = ((value ^ key[index % 32]) + previous) & 255
            previous = FEEDBACK[encoded]
            result.append(encoded)
        if index % 32 == 31:
            shift = ROTATIONS[(index // 32) % 32]
            if shift:
                old = bytes(key)
                for j in range(32):
                    key[j] = ((old[j] << shift) | (old[(j + 1) % 32] >> (8-shift))) & 255
    return bytes(result)


def parse_xml(data: bytes) -> ET.Element:
    if len(data) > MAX_XML or b'<!DOCTYPE' in data.upper() or b'<!ENTITY' in data.upper():
        raise ValueError('XML size or declarations rejected')
    root = ET.fromstring(data)
    if sum(1 for _ in root.iter()) > 10000:
        raise ValueError('Too many XML elements')
    return root


def decode_request(data: str) -> ET.Element:
    root = parse_xml(data.encode('utf-8'))
    if root.tag != 'c':
        raise ValueError('Expected encrypted <c> envelope')
    text = root.text or ''
    if len(text) > MAX_XML * 2:
        raise ValueError('Encoded request too large')
    try:
        raw = base64.b64decode(text, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ValueError('Invalid request Base64') from exc
    decoded = transform(raw, decrypt=True)
    # The client encrypts strlen(XML)+1 bytes, including the terminator.
    if not decoded.endswith(b'\0') or b'\0' in decoded[:-1]:
        raise ValueError('Missing or embedded XML terminator')
    return parse_xml(decoded[:-1])


def encode_request(xml: bytes) -> str:
    parse_xml(xml)
    return '<c>' + base64.b64encode(transform(xml+b'\0')).decode('ascii') + '</c>'

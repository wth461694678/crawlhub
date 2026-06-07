"""
Kuaishou __NS_hxfalcon Signature Generator — Pure Python
=========================================================
Generates the __NS_hxfalcon signature without Node.js Jose VM.

Algorithm:
1. Build signInput from URL + sorted query params
2. Generate HUDR_ device info (ChaCha20 encrypted)
3. Compute custom BLAKE2s hash of (signInput + "HUDR_" + deviceInfo)
4. Encrypt hash with CTS stream cipher
5. Build 45-byte metadata header with checksum
6. XOR metadata bytes with checksum byte as mask
7. Assemble: "HUDR_" + deviceInfo + "$HE_" + masked_metadata_hex
"""

import struct
import time
import json
import random
import base64

MASK32 = 0xFFFFFFFF

# ======================== Custom BLAKE2s ========================

BLAKE2S_IV = [
    2837534710, 2845986804, 2436420605, 706843635,
    719254516, 2557931286, 2596197199, 2432949778
]

BLAKE2S_SIGMA = [
    [0,1,2,3,4,5,6,7,8,9,10,11,12,13,14,15],
    [14,10,4,8,9,15,13,6,1,12,0,2,11,7,5,3],
    [11,8,12,0,5,2,15,13,10,14,3,6,7,1,9,4],
    [7,9,3,1,13,12,11,14,2,6,5,10,4,0,15,8],
    [9,0,5,7,2,4,10,15,14,1,11,12,6,8,3,13],
    [2,12,6,10,0,11,8,3,4,13,7,5,15,14,1,9],
    [12,5,1,15,14,13,4,10,0,7,6,3,9,2,8,11],
    [13,11,7,14,12,1,3,9,5,0,15,4,8,6,2,10],
    [6,15,14,9,11,3,0,8,12,2,13,7,1,4,10,5],
    [10,2,8,4,7,6,1,5,15,11,9,14,3,12,13,0],
]


def _rotr32(x, n):
    return ((x >> n) | (x << (32 - n))) & MASK32


def _blake2s_G(v, a, b, c, d, x, y):
    v[a] = (v[a] + v[b]) & MASK32
    v[a] = (v[a] + x) & MASK32
    v[d] = _rotr32(v[d] ^ v[a], 16)
    v[c] = (v[c] + v[d]) & MASK32
    v[b] = _rotr32(v[b] ^ v[c], 12)
    v[a] = (v[a] + v[b]) & MASK32
    v[a] = (v[a] + y) & MASK32
    v[d] = _rotr32(v[d] ^ v[a], 8)
    v[c] = (v[c] + v[d]) & MASK32
    v[b] = _rotr32(v[b] ^ v[c], 7)


def _blake2s_compress(h, m, t, last, iv):
    v = list(h[:8]) + [iv[i] & MASK32 for i in range(8)]
    v[12] ^= (t & MASK32)
    if last:
        v[14] ^= MASK32
    for r in range(10):
        s = BLAKE2S_SIGMA[r]
        _blake2s_G(v, 0, 4, 8, 12, m[s[0]], m[s[1]])
        _blake2s_G(v, 1, 5, 9, 13, m[s[2]], m[s[3]])
        _blake2s_G(v, 2, 6, 10, 14, m[s[4]], m[s[5]])
        _blake2s_G(v, 3, 7, 11, 15, m[s[6]], m[s[7]])
        _blake2s_G(v, 0, 5, 10, 15, m[s[8]], m[s[9]])
        _blake2s_G(v, 1, 6, 11, 12, m[s[10]], m[s[11]])
        _blake2s_G(v, 2, 7, 8, 13, m[s[12]], m[s[13]])
        _blake2s_G(v, 3, 4, 9, 14, m[s[14]], m[s[15]])
    for i in range(8):
        h[i] = (h[i] ^ v[i] ^ v[i + 8]) & MASK32
    return h


def blake2s_hash(input_str):
    """Custom BLAKE2s hash -> 64-char hex string."""
    raw = input_str.encode('utf-8')
    pad_len = (4 - len(raw) % 4) % 4
    padded = raw + b'\x00' * pad_len
    words = [struct.unpack('<i', padded[i:i+4])[0] & MASK32 for i in range(0, len(padded), 4)]

    iv = list(BLAKE2S_IV)
    h = list(iv)
    h[0] ^= 0x01010020

    n = len(words)
    offset = 0
    t = 0

    while 64 < n:
        n -= 64
        t += 64
        m = [0] * 16
        for p in range(64):
            m[p % 16] = (m[p % 16] ^ words[offset + p]) & MASK32
        _blake2s_compress(h, m, t, False, iv)
        offset += 64

    t += n
    m = [0] * 16
    for p in range(n):
        m[p % 16] = (m[p % 16] ^ words[offset + p]) & MASK32
    _blake2s_compress(h, m, t, True, iv)

    return ''.join(format(w & MASK32, '08x') for w in h)


# ======================== CTS Stream Cipher ========================

_CTS_B = bytes([
    98,0,0,128,49,117,185,253,224,172,104,36,223,155,87,19,
    32,0,0,64,2,0,0,16,255,255,255,127,255,255,255,63,
    0,0,0,240,0,0,0,192,0,0,0,128,255,255,255,15
])


def _to_s32(v):
    v = v & MASK32
    return v - 0x100000000 if v >= 0x80000000 else v


def cts_encrypt(input_bytes):
    """Custom stream cipher encrypt."""
    b = _CTS_B

    def ri32(off):
        return struct.unpack('<i', b[off:off+4])[0]

    c = ri32(12); l = ri32(8); u = ri32(4)
    p_val = ri32(0); d = ri32(16); f = ri32(20)
    h = ri32(24); m = ri32(28)
    g = ri32(44); v = ri32(40); y = ri32(36); us = ri32(32)

    key = b"Vuz4fCHxn1CO"
    for t in range(4):
        kb = key[t + 4]
        c = _to_s32((c << 8) | kb)
        l = _to_s32((l << 8) | kb)
        u = _to_s32((u << 8) | kb)

    if c == 0: c = 324508639
    if l == 0: l = 610839776
    if u == 0: u = _to_s32(4256789809)

    def js_rshift(val, n):
        return _to_s32(val) >> n

    result = bytearray(len(input_bytes))
    for idx in range(len(input_bytes)):
        t_val = 0
        n_bit = 1 & l
        r_bit = 1 & u

        for _ in range(8):
            if 1 & c:
                c = _to_s32((c ^ (js_rshift(p_val, 1) & MASK32)) | v)
                if 1 & l:
                    l = _to_s32((l ^ (js_rshift(d, 1) & MASK32)) | y)
                    n_bit = 1
                else:
                    l = _to_s32((js_rshift(l, 1) & MASK32) & m)
                    n_bit = 0
            else:
                c = _to_s32((js_rshift(c, 1) & MASK32) & h)
                if 1 & u:
                    u = _to_s32((u ^ (js_rshift(f, 1) & MASK32)) | us)
                    r_bit = 1
                else:
                    u = _to_s32((js_rshift(u, 1) & MASK32) & g)
                    r_bit = 0

            o = ((t_val << 1) & MASK32) | (n_bit ^ r_bit)
            t_val = o - 256 if o > 127 else (o + 256 if o < -128 else o)

        eb = input_bytes[idx] & 0xFF
        result[idx] = (eb ^ ((t_val + 3) & 0xFF)) & 0xFF

    return result


# ======================== ChaCha20 ========================

def _rotl32(x, n):
    return ((x << n) & MASK32) | (x >> (32 - n))


def _chacha_qr(s, a, b, c, d):
    s[a] = (s[a] + s[b]) & MASK32; s[d] = _rotl32(s[d] ^ s[a], 16)
    s[c] = (s[c] + s[d]) & MASK32; s[b] = _rotl32(s[b] ^ s[c], 12)
    s[a] = (s[a] + s[b]) & MASK32; s[d] = _rotl32(s[d] ^ s[a], 8)
    s[c] = (s[c] + s[d]) & MASK32; s[b] = _rotl32(s[b] ^ s[c], 7)


def chacha20_encrypt(key_words, nonce_words, plaintext):
    """ChaCha20 encryption matching KsGuard JS."""
    # Custom constants matching JS: r[0]=394484062, r[1]=2378328696, r[2]=630790222, r[3]=1922531795
    state = [394484062, 2378328696, 630790222, 1922531795]
    state += list(key_words[:8])
    state += [1] + list(nonce_words[:3])

    def chacha_block(st):
        w = list(st)
        for _ in range(10):
            _chacha_qr(w, 0, 4, 8, 12); _chacha_qr(w, 1, 5, 9, 13)
            _chacha_qr(w, 2, 6, 10, 14); _chacha_qr(w, 3, 7, 11, 15)
            _chacha_qr(w, 0, 5, 10, 15); _chacha_qr(w, 1, 6, 11, 12)
            _chacha_qr(w, 2, 7, 8, 13); _chacha_qr(w, 3, 4, 9, 14)
        return [(w[i] + st[i]) & MASK32 for i in range(16)]

    out_block = chacha_block(state)
    result = bytearray(len(plaintext))
    bi = 0
    for i in range(len(plaintext)):
        if bi == 64:
            state[12] += 1
            out_block = chacha_block(state)
            bi = 0
        ks = (out_block[bi >> 2] >> ((bi & 3) << 3)) & 0xFF
        result[i] = (plaintext[i] ^ ks) & 0xFF
        bi += 1
    return result


# ======================== KsGuard ========================

def _i2b_le(value, num_bytes=4):
    return [(value >> (8 * i)) & 0xFF for i in range(num_bytes)]


class KsGuard:
    _instance = None
    
    def __init__(self):
        self.count = 100
        self.info_cache = [68, 0] + _i2b_le(10, 4)  # scripts.length=10
    
    @classmethod
    def get_instance(cls):
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance
    
    def collect_device_info(self, secs_s="", secs_c=0):
        count = self.count
        self.count += 1
        
        secs_bytes = [ord(c) & 0xFF for c in secs_s] if secs_s else []
        info = [45, 61, 0, 2]
        info.extend(self.info_cache)
        info.extend([112, 0] + _i2b_le(count, 4))
        info.extend([114, 1] + _i2b_le(len(secs_bytes), 2) + secs_bytes)
        info.extend([115, 0] + _i2b_le(secs_c, 4))
        
        xored = [(b ^ 35) & 0xFF for b in info]
        
        key = [4183807412, 394484062, 1106561997, 2378328696,
               630790222, 2546784104, 2891127470, 1922531795]
        nonce = [2215853858, 1643070585, 1849059804]
        encrypted = chacha20_encrypt(key, nonce, xored)
        
        b64 = base64.b64encode(bytes(encrypted)).decode('ascii')
        return b64.replace('+', '-').replace('/', '_').replace('=', '.')


# ======================== Helper Functions ========================

def _h2b(hex_str):
    return [int(hex_str[i:i+2], 16) for i in range(0, len(hex_str), 2)]


def _b2h(byte_arr):
    return ''.join(format(b & 0xFF, '02x') for b in byte_arr)


def _xcb(a, b):
    """XOR byte arrays, cycling b over a."""
    result = []
    idx = 0
    while idx < len(a):
        for j in range(len(b)):
            if idx >= len(a):
                break
            result.append((a[idx] ^ b[j]) & 0xFF)
            idx += 1
    return result


def _i2h_le(value, num_bytes):
    """Integer to little-endian hex string."""
    mask = (1 << (num_bytes * 8)) - 1
    v = value & mask
    bs = [(v >> (8 * i)) & 0xFF for i in range(num_bytes)]
    return ''.join(format(b, '02x') for b in bs)


def _bxor48(a, b):
    """XOR two values as 48-bit integers."""
    return (a ^ b) & 0xFFFFFFFFFFFF


# ======================== Sign Input Builder ========================

def build_sign_input(url, query=None, form=None, request_body=None):
    """Build the signing input string (matches jmpOnw_ms)."""
    import re
    
    path = url
    if re.match(r'https?://', url):
        slash_idx = url.split('//')[1].index('/')
        path = url.split('//')[1][slash_idx:]
    if '?' in path:
        path = path.split('?')[0]
    
    params = []
    all_params = {}
    if query:
        all_params.update(query)
    if form:
        all_params.update(form)
    
    for k, v in all_params.items():
        if "__NS" in k:
            continue
        params.append(f"{k}={v}" if not isinstance(v, dict) else f"{k}=[object Object]")
    
    result = path + ''.join(sorted(params))
    
    if request_body and len(request_body) > 0:
        result += json.dumps(request_body, separators=(',', ':'))
    
    return result


# ======================== Metadata Constants ========================

MAGIC = "4b54"                              # "KT"
VERSION = _i2h_le(43468, 2)                 # "cca9"
MARKER = "ab"                               # 0xAB separator
FIXED_BLOCK = "01000000"                    # 4 bytes
FIXED_BYTE = "01"                           # 1 byte
CONST_BASE = 3131873467                     # XOR'd with count for const_hash
BXOR_MASK = 3360347992                      # For timestamp XOR
XCB_MASK = [0x2D, 0xD3, 0x45, 0xC0]        # CTS first-4 XOR mask
GEH_MASK = [0x7B, 0x56, 0x3E, 0xDA]        # Environment header XOR mask
GEH_HEX = "e0000000000000"                 # Raw environment header
BYTE43 = 0xE8                               # Fixed penultimate byte


# ======================== Signature Generator ========================

# Fake browser-like stack trace for SECS.s (100 chars, mimicking Chrome stack)
_FAKE_SECS_S = (
    "at Object.<anonymous> (https://www.kuaishou.com/static/js/main.js:1:2345)\n"
    "    at Module._compile"
)[:100]


class HxFalconSigner:
    """Stateful __NS_hxfalcon signature generator."""
    
    def __init__(self):
        self.guard = KsGuard()
        self.count = 100  # Metadata count (separate from KsGuard count)
        self.startup_time = int(time.time() * 1000)
    
    def sign(self, url, query=None, form=None, request_body=None,
             cookies="", secs_s=None, secs_c=None):
        """
        Generate __NS_hxfalcon signature.
        
        Args:
            url: API path (e.g., "/rest/v/profile/get")
            query: Query parameters dict (exclude __NS_* params)
            form: Form parameters dict
            request_body: Request body dict (for POST with JSON body)
            cookies: Cookie string (currently unused in sign input)
            secs_s: SECS.s value (default: fake browser stack trace)
            secs_c: SECS.c value (default: current count)
        
        Returns:
            dict with 'hxfalcon' and 'caver' keys
        """
        query = query or {}
        form = form or {}
        request_body = request_body or {}
        
        count = self.count
        self.count += 1
        
        # SECS defaults: mimic VM behavior (stack trace + count)
        if secs_s is None:
            secs_s = _FAKE_SECS_S
        if secs_c is None:
            secs_c = count
        
        # Step 1: Build sign input
        sign_input = build_sign_input(url, query, form, request_body)
        
        # Step 2: Generate HUDR_ device info
        device_info = self.guard.collect_device_info(secs_s, secs_c)
        
        # Step 3: Custom BLAKE2s hash
        hash_input = sign_input + "HUDR_" + device_info
        hash_hex = blake2s_hash(hash_input)
        hash_bytes = bytes([ord(c) for c in hash_hex])  # ASCII bytes of hex chars
        
        # Step 4: CTS stream cipher encrypt
        cts_output = cts_encrypt(hash_bytes)
        cts_hex = _b2h(cts_output)
        
        # Step 5: Build 45-byte metadata header
        now_ms = int(time.time() * 1000)
        
        # (a) XOR first 4 CTS bytes with mask
        cts_first4 = _h2b(cts_hex[:8])
        xcb_result = _xcb(cts_first4, XCB_MASK)
        
        # (b) Timestamp fields
        ts1_hex = _i2h_le(self.startup_time, 6)    # startupRandom (init time)
        ts2_hex = _i2h_le(random.randint(0, (1 << 48) - 1), 6)  # random 48-bit
        
        # (c) Const hash: 3131873467 ^ count
        const_val = (CONST_BASE ^ count) & MASK32
        const_hex = _i2h_le(_to_s32(const_val), 4) if const_val < 0x80000000 else _i2h_le(const_val, 4)
        
        # (d) Timestamp XOR
        ts_xor_hex = _i2h_le(_bxor48(now_ms, BXOR_MASK), 6)
        
        # (e) Environment header XOR
        geh_bytes = _h2b(GEH_HEX)
        geh_xored = _xcb(geh_bytes, GEH_MASK)
        
        # (f) Assemble first 43 bytes (86 hex chars)
        meta_hex = MAGIC + VERSION + MARKER
        meta_hex += ts1_hex           # 6 bytes
        meta_hex += ts2_hex           # 6 bytes
        meta_hex += FIXED_BLOCK       # 4 bytes
        meta_hex += FIXED_BYTE        # 1 byte
        meta_hex += const_hex         # 4 bytes
        meta_hex += _b2h(xcb_result)  # 4 bytes
        meta_hex += ts_xor_hex        # 6 bytes
        meta_hex += _b2h(geh_xored)   # 7 bytes
        # Total: 2+2+1+6+6+4+1+4+4+6+7 = 43 bytes ✓
        
        # (g) byte43 = fixed 0xE8
        meta_hex += format(BYTE43, '02x')
        # Total: 44 bytes = 88 hex chars
        
        # (h) byte44 = checksum: (256 - sum(first_44_bytes)) & 0xFF
        first44 = _h2b(meta_hex)
        checksum = (256 - (sum(first44) & 0xFF)) & 0xFF
        meta_hex += format(checksum, '02x')
        # Total: 45 bytes = 90 hex chars ✓
        
        # Step 6: XOR masking — each byte XOR'd with checksum, last byte unchanged
        meta_bytes = _h2b(meta_hex)
        he_bytes = []
        for i in range(45):
            mask = checksum if i < 44 else 0x00
            he_bytes.append((meta_bytes[i] ^ mask) & 0xFF)
        
        he_hex = ''.join(format(b, '02x') for b in he_bytes)
        
        # Step 7: Assemble final signature
        hxfalcon = f"HUDR_{device_info}$HE_{he_hex}"
        
        return {
            "hxfalcon": hxfalcon,
            "caver": "2"
        }


# ======================== Convenience Function ========================

_default_signer = None

def generate_hxfalcon(url, query=None, form=None, request_body=None,
                       cookies="", secs_s="", secs_c=0):
    """Convenience function using a global signer instance."""
    global _default_signer
    if _default_signer is None:
        _default_signer = HxFalconSigner()
    return _default_signer.sign(url, query, form, request_body, cookies, secs_s, secs_c)


# ======================== Test ========================

if __name__ == "__main__":
    signer = HxFalconSigner()
    
    result = signer.sign(
        url="/rest/v/profile/get",
        query={"caver": "2"},
    )
    
    he_part = result['hxfalcon'].split('$HE_')[1]
    hudr_part = result['hxfalcon'].split('$HE_')[0]
    
    print(f"hxfalcon length: {len(result['hxfalcon'])}")
    print(f"HUDR_ part: {hudr_part}")
    print(f"$HE_ part:  {he_part}")
    print(f"$HE_ length: {len(he_part)} (expected: 90)")
    print(f"caver: {result['caver']}")
    
    # Verify checksum
    meta_hex = ''.join(format(b ^ (int(he_part[-2:], 16) if i < 44 else 0), '02x') 
                       for i, b in enumerate(_h2b(he_part)))
    meta_bytes = _h2b(meta_hex)
    print(f"\nChecksum verification: sum mod 256 = {sum(meta_bytes) & 0xFF} (should be 0)")
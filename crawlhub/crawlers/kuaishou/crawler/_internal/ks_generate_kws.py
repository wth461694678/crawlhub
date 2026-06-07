"""
Generate kwscode and kwssectoken using front-end fallback logic.
No kws VM needed - pure AES-256-CBC encryption.
"""
import time
import random
import string
import base64
from urllib.parse import quote
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad


# Constants extracted from index-WivPQtlN.js
KWS_AES_KEY = "H4tL6rNd3vB9xM5k"  # u$5 - for kwscode/kwssectoken
KWF_AES_KEY = "K8wm5PvY9nX7qJc2"  # h$6 - for kwfv1 fingerprint
PRODUCT_NAME = "KUAISHOU_VISION"


def random_string(length):
    """rt$1(n) — generate random alphanumeric string"""
    chars = string.ascii_letters + string.digits
    return ''.join(random.choice(chars) for _ in range(length))


def aes_encrypt(plaintext, key_str):
    """
    st$1(e, t) — AES-256-CBC encrypt, key=IV, PKCS7 padding.
    CryptoJS uses UTF-8 parsed key, and key also serves as IV.
    The output is CryptoJS default base64 format.
    """
    key = key_str.encode('utf-8')
    # CryptoJS with 16-byte key uses AES-128-CBC
    # key is also used as IV
    iv = key
    cipher = AES.new(key, AES.MODE_CBC, iv)
    padded = pad(plaintext.encode('utf-8'), AES.block_size)
    encrypted = cipher.encrypt(padded)
    return base64.b64encode(encrypted).decode('utf-8')


def generate_kwscode_kwssectoken(href="https://www.kuaishou.com/", did="", product_name=PRODUCT_NAME):
    """
    Replicates getDefaultData() from index-WivPQtlN.js.
    Generates kwscode and kwssectoken without kws VM.
    """
    timestamp = int(time.time() * 1000)
    
    # Generate secToken (kwssectoken) — 64-char random string
    sec_token = random_string(64)
    
    # Build plaintext for kwscode encryption
    encoded_href = quote(href[:80], safe='/:?#[]@!$&\'()*+,;=-._~')
    plaintext = f"{encoded_href}|{did}|{product_name}|{timestamp}|{sec_token[:8]}"
    
    # AES-256-CBC encrypt with key u$5
    encrypted = aes_encrypt(plaintext, KWS_AES_KEY)
    
    # Format kwscode: "K" + slice(0,4) + "W" + slice(4,-2) + "S" + slice(-2)
    kwscode = f"K{encrypted[:4]}W{encrypted[4:-2]}S{encrypted[-2:]}"
    
    return kwscode, sec_token


def generate_kwfv1(href="https://www.kuaishou.com/", did="", product_name=PRODUCT_NAME):
    """
    Replicates getDefaultData() fingerprint generation from index-WivPQtlN.js.
    NOTE: This is a fallback fingerprint, not the full kwf VM fingerprint.
    """
    timestamp = int(time.time() * 1000)
    random_suffix = random_string(8)
    
    encoded_href = quote(href[:80], safe='/:?#[]@!$&\'()*+,;=-._~')
    plaintext = f"{encoded_href}|{did}|{product_name}|{timestamp}|{random_suffix}"
    
    encrypted = aes_encrypt(plaintext, KWF_AES_KEY)
    
    # Format: "K" + slice(0,4) + "W" + slice(4,-2) + "F" + slice(-2)
    kwfv1 = f"K{encrypted[:4]}W{encrypted[4:-2]}F{encrypted[-2:]}"
    
    return kwfv1


if __name__ == "__main__":
    print("=" * 60)
    print("kwscode / kwssectoken Generator (Front-end Fallback)")
    print("=" * 60)
    
    kwscode, kwssectoken = generate_kwscode_kwssectoken()
    kwfv1 = generate_kwfv1()
    
    print(f"\nkwscode:      {kwscode}")
    print(f"  length:     {len(kwscode)}")
    print(f"\nkwssectoken:  {kwssectoken}")
    print(f"  length:     {len(kwssectoken)}")
    print(f"\nkwfv1:        {kwfv1}")
    print(f"  length:     {len(kwfv1)}")
    
    print("\n" + "=" * 60)
    print("Cookie format:")
    print("=" * 60)
    print(f"kwscode={kwscode};")
    print(f"kwssectoken={kwssectoken};")
    print(f"kwfv1={kwfv1};")
    
    print("\n" + "=" * 60)
    print("Regenerating after 1 second...")
    print("=" * 60)
    import time as t
    t.sleep(1)
    kwscode2, kwssectoken2 = generate_kwscode_kwssectoken()
    print(f"kwscode:      {kwscode2}")
    print(f"kwssectoken:  {kwssectoken2}")
    print(f"Values different: {kwscode != kwscode2}")

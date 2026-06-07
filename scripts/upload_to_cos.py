"""Upload wheels to COS and regenerate PEP 503 index.

Usage:
    python scripts/upload_to_cos.py <dist_dir>

Environment variables (required):
    COS_SECRET_ID   - Tencent Cloud SecretId
    COS_SECRET_KEY  - Tencent Cloud SecretKey
    COS_BUCKET      - Bucket name, e.g. "crawlhub-pypi-1234567890"
    COS_REGION      - Bucket region, e.g. "ap-guangzhou"

The script will:
1. Upload all .whl files from <dist_dir> to COS at packages/crawlhub/
2. List all wheels already in COS at that prefix
3. Regenerate packages/crawlhub/index.html (PEP 503 simple index)
4. Upload the updated index.html
"""

import os
import sys
from html import escape
from pathlib import Path

from qcloud_cos import CosConfig, CosS3Client

# PEP 503 index prefix on COS (standard: /simple/<package>/)
PREFIX = "simple/crawlhub"

# COS bucket info (static website hosting enabled)
BUCKET_NAME = "crawlhub-pypi-1340752493"
BUCKET_REGION = "ap-guangzhou"
STATIC_WEBSITE_URL = f"{BUCKET_NAME}.cos-website.{BUCKET_REGION}.myqcloud.com"


def get_client() -> CosS3Client:
    secret_id = os.environ["COS_SECRET_ID"]
    secret_key = os.environ["COS_SECRET_KEY"]
    bucket = os.environ["COS_BUCKET"]
    region = os.environ["COS_REGION"]

    config = CosConfig(Region=region, SecretId=secret_id, SecretKey=secret_key)
    return CosS3Client(config), bucket


def upload_wheels(client: CosS3Client, bucket: str, dist_dir: str) -> list[str]:
    """Upload all .whl files from dist_dir to COS. Returns list of filenames."""
    uploaded = []
    dist_path = Path(dist_dir)

    for whl in sorted(dist_path.glob("*.whl")):
        key = f"{PREFIX}/{whl.name}"
        print(f"  Uploading {whl.name} ...")
        client.upload_file(Bucket=bucket, Key=key, LocalFilePath=str(whl))
        uploaded.append(whl.name)
        print(f"    [OK] {key}")

    return uploaded


def list_existing_wheels(client: CosS3Client, bucket: str) -> list[str]:
    """List all .whl files already in COS under the prefix."""
    filenames = []
    marker = ""
    while True:
        resp = client.list_objects(
            Bucket=bucket, Prefix=PREFIX, Delimiter="", Marker=marker, MaxKeys=1000
        )
        for obj in resp.get("Contents", []):
            name = obj["Key"]
            if name.endswith(".whl"):
                filenames.append(Path(name).name)
        if resp.get("IsTruncated") == "true":
            marker = resp["NextMarker"]
        else:
            break
    return sorted(set(filenames))


def generate_index_html(wheel_filenames: list[str]) -> str:
    """Generate PEP 503 simple API index HTML."""
    # Parse package name from first wheel (e.g. "crawlhub-1.1.0-..." -> "crawlhub")
    pkg_name = "crawlhub"
    lines = [
        "<!DOCTYPE html>",
        "<html><head><title>Links for crawlhub</title></head>",
        "<body>",
        f"<h1>Links for {escape(pkg_name)}</h1>",
    ]
    for fn in wheel_filenames:
        lines.append(f'<a href="{escape(fn)}">{escape(fn)}</a>')
    lines.append("</body></html>")
    return "\n".join(lines)


def main():
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <dist_dir>")
        sys.exit(1)

    dist_dir = sys.argv[1]
    if not Path(dist_dir).is_dir():
        print(f"[ERR] {dist_dir} is not a directory")
        sys.exit(1)

    client, bucket = get_client()

    # Step 1: Upload new wheels
    print("[1/3] Uploading wheels...")
    uploaded = upload_wheels(client, bucket, dist_dir)
    print(f"  Uploaded {len(uploaded)} wheels")

    # Step 2: List all wheels (existing + new)
    print("[2/3] Listing all wheels in COS...")
    all_wheels = list_existing_wheels(client, bucket)
    print(f"  Found {len(all_wheels)} wheels total")

    # Step 3: Generate and upload PEP 503 index for crawlhub/
    print("[3/3] Regenerating PEP 503 index...")
    index_html = generate_index_html(all_wheels)
    index_key = f"{PREFIX}/index.html"

    import tempfile

    with tempfile.NamedTemporaryFile(mode="w", suffix=".html", delete=False, encoding="utf-8") as f:
        f.write(index_html)
        tmp_path = f.name

    client.upload_file(
        Bucket=bucket,
        Key=index_key,
        LocalFilePath=tmp_path,
        ContentType="text/html",
    )
    os.unlink(tmp_path)
    print(f"  [OK] {index_key}")

    # Step 4: Generate root /simple/index.html listing all packages
    print("[4/4] Regenerating root index...")
    root_index = '<!DOCTYPE html>\n<html><head><title>Simple Index</title></head>\n<body>\n'
    root_index += '<a href="crawlhub/">crawlhub</a>\n'
    root_index += '</body></html>'
    root_key = "simple/index.html"

    with tempfile.NamedTemporaryFile(mode="w", suffix=".html", delete=False, encoding="utf-8") as f:
        f.write(root_index)
        tmp_path = f.name

    client.upload_file(
        Bucket=bucket,
        Key=root_key,
        LocalFilePath=tmp_path,
        ContentType="text/html",
    )
    os.unlink(tmp_path)
    print(f"  [OK] {root_key}")

    print("\n[OK] All done!")
    print(f"  Install: pip install crawlhub --extra-index-url https://{STATIC_WEBSITE_URL}/simple/")


if __name__ == "__main__":
    main()

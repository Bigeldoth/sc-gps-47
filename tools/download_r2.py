"""Download artifacts from Cloudflare R2."""
import boto3
import os
import sys


def main():
    account_id = os.environ["CF_ACCOUNT_ID"]
    access_key = os.environ["CF_R2_ACCESS_KEY_ID"]
    secret_key = os.environ["CF_R2_SECRET_ACCESS_KEY"]
    bucket = os.environ.get("CF_R2_BUCKET", "spacedrive")

    prefix = sys.argv[1] if len(sys.argv) > 1 else ""
    dest_dir = sys.argv[2] if len(sys.argv) > 2 else "."

    os.makedirs(dest_dir, exist_ok=True)

    s3 = boto3.client(
        "s3",
        endpoint_url=f"https://{account_id}.r2.cloudflarestorage.com",
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        region_name="auto",
    )

    response = s3.list_objects_v2(Bucket=bucket, Prefix=prefix)
    objects = response.get("Contents", [])
    if not objects:
        print(f"Error: no objects found with prefix '{prefix}'", file=sys.stderr)
        sys.exit(1)

    for obj in objects:
        key = obj["Key"]
        dest_path = os.path.join(dest_dir, os.path.basename(key))
        try:
            s3.download_file(bucket, key, dest_path)
            print(f"Downloaded: {key} -> {dest_path}")
        except Exception as e:
            print(f"Error downloading {key}: {e}", file=sys.stderr)
            sys.exit(1)


if __name__ == "__main__":
    main()

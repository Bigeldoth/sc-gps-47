"""Upload build artifacts to Cloudflare R2."""
import boto3
import glob
import os
import sys


def main():
    account_id = os.environ["CF_ACCOUNT_ID"]
    access_key = os.environ["CF_R2_ACCESS_KEY_ID"]
    secret_key = os.environ["CF_R2_SECRET_ACCESS_KEY"]
    bucket = os.environ.get("CF_R2_BUCKET", "spacedrive")

    s3 = boto3.client(
        "s3",
        endpoint_url=f"https://{account_id}.r2.cloudflarestorage.com",
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        region_name="auto",
    )

    pattern = sys.argv[1] if len(sys.argv) > 1 else "dist/SpaceDrive-Setup-*.exe"
    files = glob.glob(pattern)
    if not files:
        print(f"Error: no files matched '{pattern}'", file=sys.stderr)
        sys.exit(1)

    for file_path in files:
        file_name = os.path.basename(file_path)
        try:
            s3.upload_file(file_path, bucket, file_name)
            print(f"Uploaded: {file_name} -> r2://{bucket}/{file_name}")
        except Exception as e:
            print(f"Error uploading {file_name}: {e}", file=sys.stderr)
            sys.exit(1)


if __name__ == "__main__":
    main()

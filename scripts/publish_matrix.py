#!/usr/bin/env python3
"""Extract the roles matrix from Excel and publish it to S3 in one step.

The Lambda reads this JSON at cold start, so after publishing a new version the
change is picked up on the next cold start (or force one by redeploying / bumping
an env var).

Usage:
    python publish_matrix.py EPH_Security_Roles_Matrix_v108.xlsx \
        --bucket my-config-bucket --key config/roles_matrix.json

    python publish_matrix.py matrix.xlsx --out roles_matrix.json   # local only, no upload
"""
import argparse
import json
import sys
import tempfile

from extract_matrix import extract


def main():
    ap = argparse.ArgumentParser(description="Publish the EPH roles matrix to S3.")
    ap.add_argument("xlsx", help="Path to the EPH_Security_Roles_Matrix Excel file.")
    ap.add_argument("--bucket", help="Target S3 bucket.")
    ap.add_argument("--key", default="config/roles_matrix.json", help="Target S3 key.")
    ap.add_argument("--region", help="AWS region (optional).")
    ap.add_argument("--out", help="Write JSON locally to this path instead of / in addition to S3.")
    args = ap.parse_args()

    data = extract(args.xlsx)
    body = json.dumps(data, indent=2)
    print(f"Extracted version={data['source_version']} use_cases={len(data['use_cases'])}")

    if args.out:
        with open(args.out, "w") as f:
            f.write(body)
        print(f"Wrote {args.out}")

    if args.bucket:
        import boto3

        session = boto3.session.Session(region_name=args.region) if args.region else boto3.session.Session()
        s3 = session.client("s3")
        s3.put_object(
            Bucket=args.bucket,
            Key=args.key,
            Body=body.encode("utf-8"),
            ContentType="application/json",
        )
        print(f"Uploaded to s3://{args.bucket}/{args.key}")
    elif not args.out:
        print("Nothing to do: pass --bucket to upload or --out to write locally.", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()

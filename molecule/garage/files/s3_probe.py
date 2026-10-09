#!/usr/bin/env python3
"""Minimal S3 client (AWS Signature Version 4) for the Molecule verify.

Usage: s3_probe.py METHOD URL REGION CAFILE [BODY]
Credentials come from AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY. Prints "<status> <body>".
"""
import datetime
import hashlib
import hmac
import os
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request


def sign(key, msg):
    return hmac.new(key, msg.encode(), hashlib.sha256).digest()


def main():
    method, url, region, cafile = sys.argv[1:5]
    body = sys.argv[5].encode() if len(sys.argv) > 5 else b""
    access_key = os.environ["AWS_ACCESS_KEY_ID"]
    secret_key = os.environ["AWS_SECRET_ACCESS_KEY"]
    parts = urllib.parse.urlsplit(url)
    now = datetime.datetime.now(datetime.timezone.utc)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    date = now.strftime("%Y%m%d")
    payload_hash = hashlib.sha256(body).hexdigest()
    host = parts.netloc
    canonical_query = "&".join(
        "%s=%s" % (urllib.parse.quote(k, safe="-_.~"), urllib.parse.quote(v, safe="-_.~"))
        for k, v in sorted(urllib.parse.parse_qsl(parts.query, keep_blank_values=True))
    )
    headers = {"host": host, "x-amz-content-sha256": payload_hash, "x-amz-date": amz_date}
    signed_headers = ";".join(sorted(headers))
    canonical_headers = "".join("%s:%s\n" % (k, headers[k]) for k in sorted(headers))
    canonical_request = "\n".join(
        [method, urllib.parse.quote(parts.path or "/", safe="/-_.~"), canonical_query, canonical_headers, signed_headers, payload_hash]
    )
    scope = "%s/%s/s3/aws4_request" % (date, region)
    string_to_sign = "\n".join(["AWS4-HMAC-SHA256", amz_date, scope, hashlib.sha256(canonical_request.encode()).hexdigest()])
    k = sign(("AWS4" + secret_key).encode(), date)
    k = sign(k, region)
    k = sign(k, "s3")
    k = sign(k, "aws4_request")
    signature = hmac.new(k, string_to_sign.encode(), hashlib.sha256).hexdigest()
    auth = "AWS4-HMAC-SHA256 Credential=%s/%s, SignedHeaders=%s, Signature=%s" % (access_key, scope, signed_headers, signature)
    req = urllib.request.Request(url, data=body if method in ("PUT", "POST") else None, method=method)
    req.add_header("Authorization", auth)
    req.add_header("x-amz-content-sha256", payload_hash)
    req.add_header("x-amz-date", amz_date)
    ctx = ssl.create_default_context(cafile=cafile)
    try:
        with urllib.request.urlopen(req, context=ctx, timeout=15) as resp:
            print(resp.status, resp.read().decode(errors="replace"))
    except urllib.error.HTTPError as err:
        print(err.code, err.read().decode(errors="replace"))


if __name__ == "__main__":
    main()

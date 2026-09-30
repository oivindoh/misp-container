"""Unit tests for the minimal S3 client (misp_container.s3).

The signatures are AWS's published Signature Version 4 examples for S3.
"""

import datetime
import hashlib
import io
import os
import sys
import urllib.error

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "files"))

from misp_container import s3  # noqa: E402

AWS = {"region": "us-east-1", "access_key": "AKIAIOSFODNN7EXAMPLE",
       "secret_key": "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
       "now": datetime.datetime(2013, 5, 24, tzinfo=datetime.timezone.utc)}
HOST = "examplebucket.s3.amazonaws.com"


class TestSignature:
    def test_get_object(self):
        headers = s3.sign("GET", HOST, "/test.txt", {}, {"Range": "bytes=0-9"}, s3.EMPTY_SHA256, **AWS)
        assert headers["authorization"] == (
            "AWS4-HMAC-SHA256 Credential=AKIAIOSFODNN7EXAMPLE/20130524/us-east-1/s3/aws4_request, "
            "SignedHeaders=host;range;x-amz-content-sha256;x-amz-date, "
            "Signature=f0e8bdb87c964420e857bd35b5d6ed310bd44f0170aba48dd91039c6036bdb41")

    def test_put_object_with_an_encoded_key(self):
        body = b"Welcome to Amazon S3."
        headers = s3.sign("PUT", HOST, "/test$file.text", {},
                          {"Date": "Fri, 24 May 2013 00:00:00 GMT", "x-amz-storage-class": "REDUCED_REDUNDANCY"},
                          hashlib.sha256(body).hexdigest(), **AWS)
        assert headers["authorization"].endswith("98ad721746da40c64f1a55b78f14c238d841ea1380cd77a1b5971af0ece108bd")

    def test_list_objects(self):
        headers = s3.sign("GET", HOST, "/", {"max-keys": "2", "prefix": "J"}, {}, s3.EMPTY_SHA256, **AWS)
        assert headers["authorization"].endswith("34b48302e7b5fa45bde8084f4b7868a86f0a534bc59db6670ed5711ef69dc6f7")


class TestTarget:
    def test_path_style_with_an_endpoint(self):
        bucket = s3.Bucket("misp", "a", "s", endpoint="http://garage:3900")
        assert bucket._target("12/34") == ("http", "garage:3900", "/misp/12/34")
        assert bucket._target("") == ("http", "garage:3900", "/misp/")

    def test_virtual_hosted_on_aws(self):
        bucket = s3.Bucket("misp", "a", "s", region="eu-north-1")
        assert bucket._target("12/34") == ("https", "misp.s3.eu-north-1.amazonaws.com", "/12/34")


def page(keys, token=""):
    ns = "http://s3.amazonaws.com/doc/2006-03-01/"
    body = "".join(f"<Contents><Key>{k}</Key></Contents>" for k in keys)
    more = f"<IsTruncated>true</IsTruncated><NextContinuationToken>{token}</NextContinuationToken>" if token else \
        "<IsTruncated>false</IsTruncated>"
    return io.BytesIO(f'<ListBucketResult xmlns="{ns}">{body}{more}</ListBucketResult>'.encode())


class TestRequests:
    def test_keys_follow_the_continuation_token(self, monkeypatch):
        seen = []

        def request(self, method, key="", query=None, **kwargs):
            seen.append(query)
            return page(["1/1", "1/2"], token="next") if "continuation-token" not in query else page(["2/1"])

        monkeypatch.setattr(s3.Bucket, "_request", request)
        assert list(s3.Bucket("misp", "a", "s").keys()) == ["1/1", "1/2", "2/1"]
        assert seen[1]["continuation-token"] == "next"

    def test_an_error_names_the_request(self, monkeypatch):
        def refuse(request, timeout, context):
            raise urllib.error.HTTPError(request.full_url, 403, "Forbidden", {}, io.BytesIO(b"<Code>AccessDenied</Code>"))

        monkeypatch.setattr(s3.urllib.request, "urlopen", refuse)
        with pytest.raises(s3.S3Error, match=r"S3 403 GET s3://misp at http://garage:3900/12/34: <Code>AccessDenied"):
            s3.Bucket("misp", "a", "s", endpoint="http://garage:3900").download("12/34", __import__("pathlib").Path("/dev/null"))

    def test_an_upload_signs_the_file_hash(self, monkeypatch, tmp_path):
        sent = {}

        def accept(request, timeout, context):
            sent.update(request.headers)
            sent["url"] = request.full_url
            return io.BytesIO(b"")

        monkeypatch.setattr(s3.urllib.request, "urlopen", accept)
        path = tmp_path / "blob"
        path.write_bytes(b"attachment")
        s3.Bucket("misp", "a", "s", endpoint="http://garage:3900").upload("shadow/12/34", path)
        assert sent["url"] == "http://garage:3900/misp/shadow/12/34"
        assert sent["X-amz-content-sha256"] == hashlib.sha256(b"attachment").hexdigest()
        assert sent["Content-length"] == "10"

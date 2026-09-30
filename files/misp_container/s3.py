"""A minimal S3 client for the migrate Job: list, get and put objects.

Requests are signed with AWS Signature Version 4. With an endpoint set, the
client sends path-style requests to it, as MISP's own client does for an
AWS-compatible store (Plugin.S3_aws_compatible); without one it sends
virtual-hosted requests to AWS. It signs with an access key; it does not
read instance or web-identity credentials.
"""

from __future__ import annotations

import datetime
import hashlib
import hmac
import shutil
import ssl
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Iterator

EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()
S3_NS = "{http://s3.amazonaws.com/doc/2006-03-01/}"
CHUNK = 1024 * 1024


class S3Error(Exception):
    """An S3 request failed: the status (0 without an answer) and S3's message."""

    def __init__(self, status: int, message: str, what: str):
        self.status = status
        super().__init__(f"S3 {status} {what}: {message}")


def _quote(value: str, safe: str = "-_.~") -> str:
    return urllib.parse.quote(value, safe=safe)


def _hmac(key: bytes, text: str) -> bytes:
    return hmac.new(key, text.encode(), hashlib.sha256).digest()


def sign(method: str, host: str, path: str, query: dict[str, str], headers: dict[str, str],
         payload_sha256: str, region: str, access_key: str, secret_key: str,
         now: datetime.datetime) -> dict[str, str]:
    """The headers of a Signature Version 4 request, Authorization included."""
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    day = amz_date[:8]
    all_headers = {**{k.lower(): v.strip() for k, v in headers.items()},
                   "host": host, "x-amz-content-sha256": payload_sha256, "x-amz-date": amz_date}
    signed = ";".join(sorted(all_headers))
    canonical = "\n".join([
        method,
        _quote(path, safe="/-_.~"),
        "&".join(f"{_quote(k)}={_quote(v)}" for k, v in sorted(query.items())),
        "".join(f"{k}:{all_headers[k]}\n" for k in sorted(all_headers)),
        signed,
        payload_sha256,
    ])
    scope = f"{day}/{region}/s3/aws4_request"
    to_sign = "\n".join(["AWS4-HMAC-SHA256", amz_date, scope, hashlib.sha256(canonical.encode()).hexdigest()])
    key = _hmac(_hmac(_hmac(_hmac(f"AWS4{secret_key}".encode(), day), region), "s3"), "aws4_request")
    signature = hmac.new(key, to_sign.encode(), hashlib.sha256).hexdigest()
    all_headers["authorization"] = (f"AWS4-HMAC-SHA256 Credential={access_key}/{scope}, "
                                    f"SignedHeaders={signed}, Signature={signature}")
    return all_headers


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass
class Bucket:
    name: str
    access_key: str
    secret_key: str
    region: str = "eu-west-1"
    endpoint: str = ""
    # True, False, or the path of a CA bundle
    verify: bool | str = True

    def __str__(self) -> str:
        return f"s3://{self.name}" + (f" at {self.endpoint}" if self.endpoint else "")

    def _target(self, key: str) -> tuple[str, str, str]:
        """(scheme, host, path) of an object, or of the bucket for an empty key."""
        if self.endpoint:
            parsed = urllib.parse.urlsplit(self.endpoint)
            base = parsed.path.rstrip("/")
            return parsed.scheme, parsed.netloc, f"{base}/{self.name}/{key}" if key else f"{base}/{self.name}/"
        return "https", f"{self.name}.s3.{self.region}.amazonaws.com", f"/{key}"

    def _context(self) -> ssl.SSLContext:
        if self.verify is False:
            return ssl._create_unverified_context()
        return ssl.create_default_context(cafile=self.verify if isinstance(self.verify, str) else None)

    def _request(self, method: str, key: str = "", query: dict[str, str] | None = None,
                 body: BinaryIO | None = None, length: int = 0, payload_sha256: str = EMPTY_SHA256):
        scheme, host, path = self._target(key)
        query = query or {}
        headers = sign(method, host, path, query, {"content-length": str(length)} if body else {},
                       payload_sha256, self.region, self.access_key, self.secret_key,
                       datetime.datetime.now(datetime.timezone.utc))
        headers.pop("host")
        url = f"{scheme}://{host}{_quote(path, safe='/-_.~')}"
        if query:
            url += "?" + "&".join(f"{_quote(k)}={_quote(v)}" for k, v in sorted(query.items()))
        request = urllib.request.Request(url, data=body, method=method, headers=headers)
        what = f"{method} {self}/{key}"
        try:
            return urllib.request.urlopen(request, timeout=300, context=self._context() if scheme == "https" else None)
        except urllib.error.HTTPError as e:
            raise S3Error(e.code, e.read().decode(errors="replace")[:500], what) from None
        except (urllib.error.URLError, OSError) as e:
            raise S3Error(0, str(getattr(e, "reason", e)), what) from None

    def keys(self, prefix: str = "") -> Iterator[str]:
        """Every key under prefix, in S3's order."""
        token = ""
        while True:
            query = {"list-type": "2", "prefix": prefix}
            if token:
                query["continuation-token"] = token
            with self._request("GET", query=query) as response:
                root = ET.fromstring(response.read())
            for item in root.iter(f"{S3_NS}Contents"):
                yield item.findtext(f"{S3_NS}Key")
            token = root.findtext(f"{S3_NS}NextContinuationToken") or ""
            if root.findtext(f"{S3_NS}IsTruncated") != "true" or not token:
                return

    def download(self, key: str, path: Path) -> int:
        """Write an object to path; returns its size."""
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._request("GET", key) as response, open(path, "wb") as f:
            shutil.copyfileobj(response, f, CHUNK)
        return path.stat().st_size

    def upload(self, key: str, path: Path) -> None:
        with open(path, "rb") as f:
            self._request("PUT", key, body=f, length=path.stat().st_size,
                          payload_sha256=file_sha256(path)).close()

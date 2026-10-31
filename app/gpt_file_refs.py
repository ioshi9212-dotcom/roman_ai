"""Read a conversation TXT passed by the official GPT Actions file-reference contract.

The model sends only openaiFileIdRefs; the Roman server fetches the exact file
bytes directly. Signed URLs are short-lived. Never log them or accept arbitrary
hosts, redirects, credentials, unsupported file formats, or large files.
"""
from __future__ import annotations

from typing import Any
import re
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from urllib.error import HTTPError, URLError

from fastapi import HTTPException

MAX_TXT_BYTES = 1_000_000
OFFICIAL_HOST = "files.oaiusercontent.com"
# Actions also delivers user attachments through regional OpenAI file hosts.
# Match one complete DNS label; never trust arbitrary oaiusercontent subdomains
# or a hostname which merely contains/starts with an approved hostname.
_REGIONAL_FILE_HOST = re.compile(r"sdmntpr[a-z0-9]{1,56}\.oaiusercontent\.com")


def _is_official_file_host(host: str | None) -> bool:
    return host == OFFICIAL_HOST or bool(host and _REGIONAL_FILE_HOST.fullmatch(host))


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def read_gpt_txt_file_refs(refs: list[Any]) -> str:
    """Return verbatim decoded UTF-8; do not strip BOM, spaces or newlines."""
    if not isinstance(refs, list) or len(refs) != 1:
        raise HTTPException(
            status_code=422,
            detail="Exactly one attached TXT is required in openaiFileIdRefs.",
        )
    ref = refs[0]
    if not isinstance(ref, dict):
        raise HTTPException(
            status_code=422,
            detail="GPT Actions must supply a file reference object with download_link.",
        )
    name = str(ref.get("name") or "")
    mime = str(ref.get("mime_type") or "").casefold().split(";", 1)[0].strip()
    if not name.casefold().endswith(".txt") and mime != "text/plain":
        raise HTTPException(status_code=422, detail="Only attached TXT files are accepted.")

    raw_url = ref.get("download_link")
    if not isinstance(raw_url, str):
        raise HTTPException(status_code=422, detail="Missing signed GPT Actions file download link.")
    # The Actions runtime occasionally sends a sandbox path instead of an
    # externally fetchable signed URL. Diagnose that precisely rather than
    # silently widening the network allowlist to arbitrary untrusted hosts.
    if raw_url.startswith(("/mnt/data/", "sandbox:/mnt/data/", "file:///")):
        raise HTTPException(
            status_code=422,
            detail={
                "code": "GPT_FILE_REF_LOCAL_PATH",
                "link_kind": "local_sandbox_path",
                "message": (
                    "ChatGPT supplied a local sandbox path, not an HTTPS download_link. "
                    "The Amvera server cannot read ChatGPT's /mnt/data. Do not retry "
                    "this same path; request a genuine signed GPT Actions file URL "
                    "or continue the exact stored RAW using raw_text chunks."
                ),
            },
        )
    try:
        uri = urlsplit(raw_url)
        host = uri.hostname
        allowed = (
            uri.scheme == "https"
            and _is_official_file_host(host)
            and uri.port in (None, 443)
            and not uri.username
            and not uri.password
            and bool(uri.path and uri.path != "/")
            and not uri.fragment
        )
    except ValueError:
        host = None
        allowed = False
    if not allowed:
        # Hostnames are safe to display; the complete signed URL, path, query
        # and fragment might contain private data or credentials and must
        # never appear in the Action result or logs.
        public_host = (
            host if isinstance(host, str)
            and re.fullmatch(r"[a-z0-9.-]{1,253}", host) else None
        )
        raise HTTPException(
            status_code=422,
            detail={
                "code": "GPT_FILE_REF_UNSUPPORTED_OR_UNSAFE_URL",
                "link_kind": "external_https_url" if raw_url.startswith("https://") else "non_https_or_invalid",
                "received_host": public_host,
                "expected_host": OFFICIAL_HOST,
                "accepted_host_patterns": [OFFICIAL_HOST, "sdmntpr<region>.oaiusercontent.com"],
                "message": (
                    "The GPT Actions file reference was not an approved signed "
                    "HTTPS download URL. Only the hostname is reported; URL "
                    "path/query/signature are secret. Do not invent download "
                    "URLs or enable unrestricted hosts. Use the exact original "
                    "RAW text in consecutive chunks if the official file "
                    "reference transport is unavailable."
                ),
            },
        )

    # Do not leak the short-lived signature to application logs or errors.
    request = Request(
        raw_url, method="GET",
        headers={"User-Agent": "RomanAI-GPT-Actions-TXT/1.0"},
    )
    opener = build_opener(_NoRedirect)
    try:
        with opener.open(request, timeout=12) as response:
            if response.status != 200:
                raise HTTPException(status_code=502, detail="GPT Actions file download did not return 200.")
            length = response.headers.get("Content-Length")
            if length is not None:
                try:
                    if int(length) > MAX_TXT_BYTES:
                        raise HTTPException(status_code=413, detail="TXT file exceeds 1 MB safety limit.")
                except ValueError:
                    pass
            data = response.read(MAX_TXT_BYTES + 1)
    except HTTPException:
        raise
    except (HTTPError, URLError, TimeoutError, OSError) as exc:
        raise HTTPException(
            status_code=502,
            detail="Could not download the attached TXT from the temporary GPT Actions link. Reinvoke the same action with a fresh file reference.",
        ) from None

    if len(data) > MAX_TXT_BYTES:
        raise HTTPException(status_code=413, detail="TXT file exceeds 1 MB safety limit.")
    if not data:
        raise HTTPException(status_code=422, detail="Attached TXT file is empty.")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise HTTPException(
            status_code=422, detail="Attached TXT must be valid UTF-8; no lossy character conversion was attempted.",
        ) from None
    if not text.strip():
        raise HTTPException(status_code=422, detail="Attached TXT has no substantive text.")
    return text

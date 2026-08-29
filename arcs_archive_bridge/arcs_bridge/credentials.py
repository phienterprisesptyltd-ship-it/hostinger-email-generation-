"""Refuse to ingest authentication material.

The bridge must never extract or store ChatGPT cookies or tokens.  Two controls
implement that promise:

1. The UI-assisted capture script never reads ``document.cookie`` and never
   copies an ``Authorization`` header; it relies on the browser attaching the
   user's existing session to same-origin requests.  See
   ``browser_capture/arcs-ui-capture.user.js`` and the test that asserts the
   shipped script contains no cookie access.
2. This module scans every byte that arrives at the ingestion boundary.  If it
   looks like credential material the input is quarantined and ingestion fails
   closed.  A capture that has to carry a token is a capture we do not want.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

#: Named patterns for material that must never enter the archive.
CREDENTIAL_PATTERNS: dict[str, re.Pattern] = {
    "openai_api_key": re.compile(r"\bsk-[A-Za-z0-9_\-]{20,}"),
    "openai_project_key": re.compile(r"\bsk-proj-[A-Za-z0-9_\-]{20,}"),
    "bearer_header": re.compile(r"\bAuthorization\s*[:=]\s*['\"]?Bearer\s+[A-Za-z0-9._\-]{20,}", re.I),
    "jwt": re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}"),
    "chatgpt_session_cookie": re.compile(r"__Secure-next-auth\.session-token", re.I),
    "chatgpt_puid_cookie": re.compile(r"\b_puid\s*[:=]", re.I),
    "cf_clearance_cookie": re.compile(r"\bcf_clearance\s*[:=]", re.I),
    "cookie_header": re.compile(r"\bCookie\s*[:=]\s*['\"][^'\"]{20,}", re.I),
    "set_cookie_header": re.compile(r"\bSet-Cookie\s*:", re.I),
    "document_cookie_access": re.compile(r"document\.cookie"),
    "aws_access_key": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    "private_key_block": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
}

#: Keys whose *presence* in a capture bundle is disallowed regardless of value.
FORBIDDEN_JSON_KEYS = {
    "cookie",
    "cookies",
    "authorization",
    "access_token",
    "id_token",
    "refresh_token",
    "session_token",
    "sessiontoken",
    "accesstoken",
    "bearer",
    "auth_token",
    "api_key",
    "apikey",
}


#: Findings that mean the *capture tool* is carrying authentication material.
#: These fail ingestion closed: a capture that needs a token is not a capture we
#: want, and the archive must never hold one.
AUTH_BOUNDARY_KINDS = frozenset({
    "bearer_header",
    "chatgpt_session_cookie",
    "chatgpt_puid_cookie",
    "cf_clearance_cookie",
    "cookie_header",
    "set_cookie_header",
    "document_cookie_access",
})

#: Findings that mean the *user pasted a secret into a conversation*.  That is
#: source material and preserving it is the archive's first duty, so these
#: never block ingestion: the conversation is ingested, flagged, and escalated
#: to the Restricted security class so it cannot leave in an evidence packet
#: without an explicit acknowledgement.
EMBEDDED_SECRET_KINDS = frozenset({
    "openai_api_key",
    "openai_project_key",
    "jwt",
    "aws_access_key",
    "private_key_block",
})


@dataclass(frozen=True)
class CredentialFinding:
    kind: str
    where: str
    excerpt: str

    def __str__(self) -> str:  # pragma: no cover - display only
        return f"{self.kind} at {self.where}: {self.excerpt}"


def _excerpt(text: str, start: int, end: int, pad: int = 12) -> str:
    lo = max(0, start - pad)
    hi = min(len(text), end + pad)
    snippet = text[lo:hi].replace("\n", "\\n")
    # Never echo the secret itself back into logs.
    return snippet[:12] + "…[redacted]…" + snippet[-6:] if len(snippet) > 24 else "…[redacted]…"


def scan_text(text: str, where: str = "<input>") -> list[CredentialFinding]:
    findings: list[CredentialFinding] = []
    for kind, pattern in CREDENTIAL_PATTERNS.items():
        m = pattern.search(text)
        if m:
            findings.append(CredentialFinding(kind, where, _excerpt(text, m.start(), m.end())))
    return findings


def scan_bytes(data: bytes, where: str = "<input>") -> list[CredentialFinding]:
    return scan_text(data.decode("utf-8", errors="replace"), where)


def scan_json(obj, where: str = "$") -> list[CredentialFinding]:
    """Walk a parsed structure looking for forbidden keys and secret-shaped values."""
    findings: list[CredentialFinding] = []
    stack = [(obj, where)]
    while stack:
        node, path = stack.pop()
        if isinstance(node, dict):
            for key, value in node.items():
                child = f"{path}.{key}"
                if str(key).strip().lower() in FORBIDDEN_JSON_KEYS:
                    findings.append(
                        CredentialFinding("forbidden_key:" + str(key).lower(), child, "…[redacted]…")
                    )
                stack.append((value, child))
        elif isinstance(node, list):
            for i, value in enumerate(node):
                stack.append((value, f"{path}[{i}]"))
        elif isinstance(node, str):
            findings.extend(scan_text(node, path))
    return findings


def scan_keys(obj, where: str = "$") -> list[CredentialFinding]:
    """Forbidden *keys* anywhere in a parsed structure.

    Legitimate conversation JSON has no ``cookie`` or ``access_token`` key, so a
    hit here is structural evidence that the input carries credentials.
    """
    findings: list[CredentialFinding] = []
    stack = [(obj, where)]
    while stack:
        node, path = stack.pop()
        if isinstance(node, dict):
            for key, value in node.items():
                child = f"{path}.{key}"
                if str(key).strip().lower() in FORBIDDEN_JSON_KEYS:
                    findings.append(
                        CredentialFinding("forbidden_key:" + str(key).lower(), child, "…[redacted]…")
                    )
                stack.append((value, child))
        elif isinstance(node, list):
            for i, value in enumerate(node):
                stack.append((value, f"{path}[{i}]"))
    return findings


def is_auth_boundary(finding: CredentialFinding) -> bool:
    return finding.kind in AUTH_BOUNDARY_KINDS or finding.kind.startswith("forbidden_key:")


def partition(findings: Iterable[CredentialFinding]):
    """Split findings into ``(refuse, flag)``."""
    refuse, flag = [], []
    for finding in findings:
        (refuse if is_auth_boundary(finding) else flag).append(finding)
    return refuse, flag


def summarise(findings: Iterable[CredentialFinding]) -> str:
    findings = list(findings)
    if not findings:
        return "no credential material detected"
    kinds = sorted({f.kind for f in findings})
    return f"{len(findings)} finding(s): " + ", ".join(kinds)

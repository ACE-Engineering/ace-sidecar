"""ace.sidecar.routing.sensitivity — Lightweight, zero-network data sensitivity classifier."""

from __future__ import annotations

import re
from typing import Iterable, List, Optional

from ace.sidecar.routing.types import SensitivityLevel

SECRET_PATTERNS = [
    re.compile(r"-----BEGIN[ A-Z0-9_-]+PRIVATE KEY-----", re.IGNORECASE),
    re.compile(r"AKIA[0-9A-Z]{16}"),                       # AWS Access Key
    re.compile(r"ghp_[0-9a-zA-Z]{36}"),                   # GitHub Personal Access Token
    re.compile(r"github_pat_[0-9a-zA-Z_]{82}"),           # GitHub Fine-Grained Token
    re.compile(r"sk-[a-zA-Z0-9]{48}"),                    # OpenAI API Key format
    re.compile(r"eyJ[A-Za-z0-9-_=]+\.[A-Za-z0-9-_=]+\.[A-Za-z0-9-_.+/=]+"), # JWT
    re.compile(r"(?:bearer\s+[a-zA-Z0-9\-\._~\+\/]+=*)", re.IGNORECASE),
    re.compile(r"(?:api[_-]?key|secret[_-]?token|auth[_-]?token)\s*[:=]\s*['\"][a-zA-Z0-9\-\._~]{16,}['\"]", re.IGNORECASE),
]

SENSITIVE_PATH_PATTERNS = [
    re.compile(r"(^|/)\.env(\.[a-zA-Z0-9_-]+)?$", re.IGNORECASE),
    re.compile(r"(^|/)(?:credentials|secrets?|passwords?|tokens?)\.(?:json|ya?ml|txt|ini|toml)$", re.IGNORECASE),
    re.compile(r"(^|/)id_(?:rsa|dsa|ecdsa|ed25519)(\.pub)?$", re.IGNORECASE),
    re.compile(r"\.(?:pem|key|pkcs12|pfx|keystore|jks)$", re.IGNORECASE),
    re.compile(r"(^|/)\.aws/(?:credentials|config)$", re.IGNORECASE),
    re.compile(r"(^|/)\.ssh/(?:id_.*|config|authorized_keys)$", re.IGNORECASE),
]

CONFIDENTIAL_KEYWORDS = [
    "confidential", "proprietary", "internal only", "trade secret",
    "do not distribute", "restricted access", "strict compliance"
]


class SensitivityClassifier:
    """Predicts data sensitivity level from prompt text and touched file paths."""

    def __init__(
        self,
        custom_secret_patterns: Optional[List[re.Pattern]] = None,
        custom_path_patterns: Optional[List[re.Pattern]] = None,
    ) -> None:
        self.secret_patterns = SECRET_PATTERNS + (custom_secret_patterns or [])
        self.path_patterns = SENSITIVE_PATH_PATTERNS + (custom_path_patterns or [])

    def classify_paths(self, file_paths: Iterable[str]) -> SensitivityLevel:
        """Evaluate sensitivity based on touched or referenced file paths."""
        highest = SensitivityLevel.PUBLIC
        for path in file_paths:
            normalized = path.replace("\\", "/")
            for pat in self.path_patterns:
                if pat.search(normalized):
                    return SensitivityLevel.RESTRICTED
            if any(k in normalized.lower() for k in ("internal", "private", "prod", "finance", "billing")):
                highest = max(highest, SensitivityLevel.CONFIDENTIAL)
            else:
                highest = max(highest, SensitivityLevel.INTERNAL)
        return highest

    def classify_text(self, text: str) -> SensitivityLevel:
        """Scan text for credentials, tokens, or confidential markers."""
        if not text:
            return SensitivityLevel.PUBLIC

        for pat in self.secret_patterns:
            if pat.search(text):
                return SensitivityLevel.RESTRICTED

        text_lower = text.lower()
        if any(kw in text_lower for kw in CONFIDENTIAL_KEYWORDS):
            return SensitivityLevel.CONFIDENTIAL

        return SensitivityLevel.INTERNAL

    def classify(self, prompt: str, file_paths: Optional[Iterable[str]] = None) -> SensitivityLevel:
        """Compute composite sensitivity level across prompt and file paths."""
        path_level = self.classify_paths(file_paths or [])
        if path_level == SensitivityLevel.RESTRICTED:
            return SensitivityLevel.RESTRICTED

        text_level = self.classify_text(prompt)
        return max(path_level, text_level)

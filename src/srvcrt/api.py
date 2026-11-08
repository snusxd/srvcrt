"""Small clients for the documented Modrinth and CurseForge endpoints."""

from __future__ import annotations

import json
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from . import __version__


MODRINTH_URL = "https://api.modrinth.com/v2/version_files"
CURSEFORGE_URL = "https://api.curseforge.com/v1/fingerprints/432"
BATCH_SIZE = 100


class APIError(Exception):
    pass


class ModAPI:
    def __init__(self, curseforge_key: str | None = None, timeout: int = 15):
        self.curseforge_key = curseforge_key
        self.timeout = timeout

    def _post(self, url: str, body: dict, headers: dict[str, str]) -> object:
        request = Request(
            url,
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": f"srvcrt/{__version__} (Minecraft server modpack builder)",
                **headers,
            },
            method="POST",
        )
        try:
            with urlopen(request, timeout=self.timeout) as response:
                return json.load(response)
        except HTTPError as error:
            raise APIError(f"HTTP {error.code} from {url}") from error
        except (URLError, TimeoutError, OSError, ValueError) as error:
            raise APIError(f"Request to {url} failed: {error}") from error

    def modrinth_versions(self, hashes: list[str]) -> dict[str, dict]:
        if not hashes:
            return {}
        response = self._post(MODRINTH_URL, {"hashes": hashes, "algorithm": "sha1"}, {})
        if not isinstance(response, dict):
            raise APIError("Modrinth returned an unexpected response")
        return {key.lower(): value for key, value in response.items()
                if isinstance(key, str) and isinstance(value, dict)}

    def curseforge_matches(self, fingerprints: list[int]) -> dict[int, list[dict]]:
        if not fingerprints:
            return {}
        if not self.curseforge_key:
            raise APIError("CURSEFORGE_API_KEY is not set")
        response = self._post(
            CURSEFORGE_URL,
            {"fingerprints": fingerprints},
            {"x-api-key": self.curseforge_key},
        )
        if not isinstance(response, dict) or not isinstance(response.get("data"), dict):
            raise APIError("CurseForge returned an unexpected response")
        matches = response["data"].get("exactMatches")
        if not isinstance(matches, list):
            raise APIError("CurseForge did not return exactMatches")
        found: dict[int, list[dict]] = {}
        for match in matches:
            if not isinstance(match, dict) or not isinstance(match.get("file"), dict):
                continue
            fingerprint = match["file"].get("fileFingerprint")
            if isinstance(fingerprint, int) and fingerprint in fingerprints:
                found.setdefault(fingerprint, []).append(match)
        return found

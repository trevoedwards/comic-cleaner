"""Asking GitHub whether a newer release exists.

Opt-in only. The one request made is for the latest release's version number;
nothing about the library, the machine or its files is sent.
"""

from __future__ import annotations

import json
import logging
import re
import urllib.error
import urllib.request
from dataclasses import dataclass

from .. import APP_NAME, GITHUB_URL, __version__

log = logging.getLogger(__name__)

TIMEOUT_SECONDS = 8


@dataclass(slots=True)
class Release:
    version: str
    url: str


class UpdateCheckError(RuntimeError):
    pass


def api_url(repo_url: str = GITHUB_URL) -> str:
    """The releases/latest endpoint for a github.com repository URL."""
    match = re.match(r"https://github\.com/([^/]+)/([^/]+?)/?$", repo_url)
    if not match:
        raise UpdateCheckError(f"not a GitHub repository URL: {repo_url}")
    owner, repo = match.groups()
    return f"https://api.github.com/repos/{owner}/{repo}/releases/latest"


def version_key(version: str) -> tuple[int, ...]:
    """Numeric parts of a version, ignoring a leading v and any suffix.

    "v0.2.0" and "0.2.0-beta" both give (0, 2, 0); anything unparseable gives ().
    """
    match = re.match(r"v?(\d+(?:\.\d+)*)", version.strip())
    if not match:
        return ()
    return tuple(int(part) for part in match.group(1).split("."))


def is_newer(candidate: str, current: str = __version__) -> bool:
    new, old = version_key(candidate), version_key(current)
    if not new or not old:
        return False
    width = max(len(new), len(old))
    return new + (0,) * (width - len(new)) > old + (0,) * (width - len(old))


def latest_release(url: str | None = None, timeout: float = TIMEOUT_SECONDS) -> Release:
    """The newest published release. Raises UpdateCheckError if it cannot be told."""
    request = urllib.request.Request(
        url or api_url(),
        headers={
            # GitHub's API refuses requests without a User-Agent.
            "User-Agent": f"{APP_NAME.replace(' ', '')}/{__version__}",
            "Accept": "application/vnd.github+json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise UpdateCheckError("no release has been published yet") from exc
        raise UpdateCheckError(f"GitHub answered {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise UpdateCheckError(f"could not reach GitHub ({exc})") from exc
    except ValueError as exc:
        raise UpdateCheckError("GitHub sent something that is not JSON") from exc

    tag = payload.get("tag_name") if isinstance(payload, dict) else None
    page = payload.get("html_url") if isinstance(payload, dict) else None
    if not isinstance(tag, str) or not version_key(tag):
        raise UpdateCheckError("the latest release has no version number")
    if not isinstance(page, str) or not page.startswith("https://github.com/"):
        page = f"{GITHUB_URL}/releases/latest"
    return Release(version=tag.lstrip("v"), url=page)

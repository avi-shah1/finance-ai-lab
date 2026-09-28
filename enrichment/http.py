"""One HTTP client for all providers: timeouts, a single retry, and HTTP
status codes translated into the typed errors in base.py."""

import time

import requests

from .base import CredentialsMissing, NotFound, RateLimited, SourceUnavailable

DEFAULT_USER_AGENT = "finance-ai-lab/0.1 (M&A screening research prototype)"


class Http:
    def __init__(self, user_agent=DEFAULT_USER_AGENT, timeout=20):
        self.session = requests.Session()
        self.session.headers["User-Agent"] = user_agent
        self.timeout = timeout

    def get(self, url, *, params=None, headers=None, auth=None, allow_redirects=True):
        """GET a URL and return the Response, or raise a typed error.
        `auth` is passed per request (never stored on the session) so
        requests strips it when a redirect leaves the original host, e.g.
        Companies House document downloads redirecting to Amazon S3."""
        return self._request("GET", url, params=params, headers=headers, auth=auth,
                             allow_redirects=allow_redirects)

    def post_json(self, url, payload, *, headers=None):
        """POST a JSON body and return the decoded JSON response."""
        resp = self._request("POST", url, json=payload, headers=headers)
        try:
            return resp.json()
        except ValueError:
            raise SourceUnavailable(f"{_host(url)} returned a response that isn't valid JSON.")

    def _request(self, method, url, **kwargs):
        for attempt in (1, 2):
            try:
                resp = self.session.request(method, url, timeout=self.timeout, **kwargs)
            except requests.RequestException as exc:
                if attempt == 1:
                    time.sleep(1)
                    continue
                raise SourceUnavailable(f"Could not reach {_host(url)}: {type(exc).__name__}")

            if resp.status_code == 429:
                wait = _retry_after(resp)
                if attempt == 1 and wait <= 10:
                    time.sleep(wait)
                    continue
                raise RateLimited(f"{_host(url)} rate limit reached. Try again in a few minutes.")
            if resp.status_code >= 500 and attempt == 1:
                time.sleep(1)
                continue
            break

        if resp.status_code == 404:
            raise NotFound(f"Not found at {_host(url)}.")
        if resp.status_code in (401, 403):
            raise CredentialsMissing(f"{_host(url)} rejected the request (HTTP {resp.status_code}). "
                                     "Check the API key / User-Agent in .env.")
        if resp.status_code >= 400:
            raise SourceUnavailable(f"{_host(url)} returned HTTP {resp.status_code}.")
        return resp

    def get_json(self, url, **kwargs):
        resp = self.get(url, **kwargs)
        try:
            return resp.json()
        except ValueError:
            raise SourceUnavailable(f"{_host(url)} returned a response that isn't valid JSON.")


def _host(url):
    return requests.utils.urlparse(url).netloc or url


def _retry_after(resp):
    try:
        return int(resp.headers.get("Retry-After", "5"))
    except ValueError:
        return 5

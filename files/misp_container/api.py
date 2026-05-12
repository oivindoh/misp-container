"""Thin MISP REST API client using only urllib (stdlib).

No PyMISP dependency. Returns plain dicts indexed by natural keys
for O(1) lookup during reconciliation.
"""

import json
import ssl
import urllib.request
import urllib.error

from misp_container.log import get as getlog

log = getlog("api")


class APIError(Exception):
    """MISP API returned an error."""
    def __init__(self, status, message, path=""):
        self.status = status
        self.path = path
        super().__init__(f"MISP API {status} {path}: {message}")


class MISPClient:
    """HTTP client for the MISP REST API."""

    def __init__(self, base_url: str, api_key: str, verify_ssl: bool = True):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self._ctx = ssl.create_default_context()
        if not verify_ssl:
            self._ctx.check_hostname = False
            self._ctx.verify_mode = ssl.CERT_NONE

    def get(self, path: str) -> dict | list:
        return self._request("GET", path)

    def post(self, path: str, data: dict) -> dict:
        return self._request("POST", path, data)

    def _request(self, method: str, path: str, data: dict | None = None):
        url = f"{self.base_url}/{path.lstrip('/')}"
        body = json.dumps(data).encode() if data else None
        req = urllib.request.Request(url, data=body, method=method, headers={
            "Authorization": self.api_key,
            "Accept": "application/json",
            "Content-Type": "application/json",
        })
        try:
            with urllib.request.urlopen(req, context=self._ctx, timeout=30) as resp:
                raw = resp.read()
                try:
                    return json.loads(raw)
                except (json.JSONDecodeError, ValueError):
                    # Some MISP endpoints (e.g. warninglists/add) return HTML
                    # after a redirect. Treat non-JSON 200 as success.
                    return {"success": True, "raw_length": len(raw)}
        except urllib.error.HTTPError as e:
            body_text = ""
            try:
                body_text = e.read().decode()[:500]
            except Exception:
                pass
            raise APIError(e.code, body_text, path) from None
        except urllib.error.URLError as e:
            raise APIError(0, str(e.reason), path) from None

    # -- Resource fetchers (return indexed dicts) --
    # MISP responses are inconsistent: sometimes [{"Key": {...}}, ...],
    # sometimes [{...}, ...], sometimes mixed with strings. All parsers
    # skip non-dict items defensively.

    @staticmethod
    def _unwrap(item, key):
        """Unwrap MISP's nested {"Key": {...}} pattern, skipping non-dicts."""
        if not isinstance(item, dict):
            return None
        inner = item.get(key, item)
        return inner if isinstance(inner, dict) else None

    def get_organisations(self) -> dict[str, dict]:
        """Returns {uuid: org_dict}."""
        result = {}
        for item in self.get("/organisations"):
            org = self._unwrap(item, "Organisation")
            if org and "uuid" in org:
                result[org["uuid"].lower()] = org
        return result

    def get_users(self) -> dict[str, dict]:
        """Returns {email_lower: user_dict}."""
        result = {}
        for item in self.get("/admin/users"):
            user = self._unwrap(item, "User")
            if not user or "email" not in user:
                continue
            user["Role"] = item.get("Role", {})
            user["Organisation"] = item.get("Organisation", {})
            result[user["email"].lower()] = user
        return result

    def get_roles(self) -> dict[str, dict]:
        """Returns {name: role_dict}."""
        result = {}
        for item in self.get("/roles"):
            role = self._unwrap(item, "Role")
            if role and "name" in role:
                result[role["name"]] = role
        return result

    def get_servers(self) -> dict[str, dict]:
        """Returns {normalized_url: server_dict}."""
        result = {}
        for item in self.get("/servers"):
            server = self._unwrap(item, "Server")
            if not server:
                continue
            url = server.get("url", "").rstrip("/").lower()
            if url:
                result[url] = server
        return result

    def get_tags(self) -> dict[str, dict]:
        """Returns {name: tag_dict}."""
        result = {}
        resp = self.get("/tags")
        # MISP may return {"Tag": [...]} or just [...]
        items = resp.get("Tag", resp) if isinstance(resp, dict) else resp
        if not isinstance(items, list):
            items = []
        for item in items:
            tag = self._unwrap(item, "Tag")
            if tag and "name" in tag:
                result[tag["name"]] = tag
        return result

    def get_taxonomies(self) -> dict[str, dict]:
        """Returns {namespace: taxonomy_dict}."""
        result = {}
        for item in self.get("/taxonomies"):
            tax = self._unwrap(item, "Taxonomy")
            if tax and "namespace" in tax:
                result[tax["namespace"]] = tax
        return result

    def get_warninglists(self) -> dict[str, dict]:
        """Returns {name: warninglist_dict}."""
        result = {}
        resp = self.get("/warninglists")
        # MISP may return {"Warninglists": [...]} or just [...]
        items = resp.get("Warninglists", resp) if isinstance(resp, dict) else resp
        if not isinstance(items, list):
            items = []
        for item in items:
            wl = self._unwrap(item, "Warninglist")
            if wl and "name" in wl:
                result[wl["name"]] = wl
        return result

    def get_sharing_groups(self) -> dict[str, dict]:
        """Returns {uuid_or_name: sg_dict}. Includes SharingGroupOrg membership."""
        result = {}
        for item in self.get("/sharing_groups"):
            sg = self._unwrap(item, "SharingGroup")
            if not sg or "name" not in sg:
                continue
            sg["SharingGroupOrg"] = item.get("SharingGroupOrg", [])
            key = sg.get("uuid", "").lower() or sg["name"]
            result[key] = sg
        return result

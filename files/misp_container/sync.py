"""Declarative org/team sync engine.

Loads YAML config (local file + optional remote source), diffs against
current MISP state via REST API, and applies changes. Designed to be
fast on warm runs (nothing changed → reads only, no writes).
"""

import json
import os
import secrets
import ssl
import urllib.request
import urllib.error

import yaml

from misp_container.api import MISPClient, APIError
from misp_container.env import env
from misp_container.log import get as getlog

log = getlog("sync")

# Fields compared for each resource type (only update if these differ)
ORG_FIELDS = ("name", "description", "sector", "nationality", "type", "contacts")
TAG_FIELDS = ("colour", "exportable", "hide_tag", "org_id")
SERVER_FIELDS = ("name", "internal", "push", "pull", "self_signed",
                 "caching_enabled", "pull_rules", "push_rules", "remote_org_id")
SG_FIELDS = ("name", "description", "releasability", "active", "roaming", "local")
ROLE_FIELDS = ("name", "permission", "perm_site_admin", "perm_admin", "perm_sync",
               "perm_auth", "perm_tagger", "perm_tag_editor", "perm_template",
               "perm_sharing_group", "perm_delegate", "perm_sighting",
               "perm_galaxy_editor", "perm_publish_zmq", "perm_publish_kafka",
               "perm_analyst_data", "perm_audit", "perm_decaying",
               "perm_regexp_access", "perm_object_template", "perm_warninglist",
               "perm_view_feed_correlations", "perm_skip_otp", "perm_server_sign",
               "max_execution_time", "memory_limit", "rate_limit_count")

TAG_DEFAULTS = {"colour": "#004daa", "exportable": True, "hide_tag": False}

import re


def _expand_env(value: str) -> str:
    """Replace ${VAR} and $VAR with environment variable values."""
    return re.sub(
        r'\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)',
        lambda m: os.environ.get(m.group(1) or m.group(2), ""),
        value,
    )
_UUID_RE = re.compile(
    r'^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$',
    re.IGNORECASE,
)


def _validate_uuid(value: str, context: str = "") -> str:
    """Validate and lowercase a UUID. Raises ValueError if invalid."""
    value = value.strip().lower()
    if not _UUID_RE.match(value):
        raise ValueError(f"invalid UUID v4 '{value}'{f' in {context}' if context else ''}")
    return value


def _values_equal(desired, current) -> bool:
    """Compare values handling MISP's string booleans (\"1\"/\"0\"/true/false)."""
    if isinstance(desired, bool):
        # MISP stores booleans as "1"/"0", "true"/"false", or actual bools
        return str(current).lower() in (
            str(desired).lower(), str(int(desired)), "1" if desired else "0",
        )
    return str(desired) == str(current)
SERVER_DEFAULTS = {
    "internal": False, "push": False, "pull": True, "self_signed": False,
    "caching_enabled": False, "pull_tags": [], "push_tags": [],
    "pull_tags_exclude": [], "push_tags_exclude": [],
}


# ---------------------------------------------------------------------------
# Config loading and merging
# ---------------------------------------------------------------------------

def load_config(
    file_path: str | None = None,
    url: str | None = None,
    token: str | None = None,
) -> dict:
    """Load and merge local + remote config. Local takes precedence."""
    file_path = file_path or env("ORG_CONFIG_FILE")
    url = url or env("ORG_CONFIG_URL")
    token = token or env("ORG_CONFIG_TOKEN")

    local = _load_local(file_path)
    remote = _fetch_remote(url, token) if url else {}

    merged = _merge_configs(local, remote) if remote else local
    return _normalize(_expand_env_recursive(merged))


def _load_local(path: str) -> dict:
    if not os.path.isfile(path):
        return {}
    with open(path) as f:
        return yaml.safe_load(f) or {}


def _fetch_remote(url: str, token: str) -> dict:
    """Fetch config from an HTTPS endpoint with bearer token."""
    headers = {"Accept": "application/json, application/yaml"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers)
    ctx = ssl.create_default_context()
    try:
        with urllib.request.urlopen(req, context=ctx, timeout=30) as resp:
            body = resp.read().decode()
            if "json" in resp.headers.get("Content-Type", ""):
                return json.loads(body)
            return yaml.safe_load(body) or {}
    except Exception as e:
        log.warning("failed to fetch remote config from %s: %s", url, e)
        return {}


USER_LIST_KEYS = ("users", "feide_users", "sync_users", "service_users")


def _merge_configs(local: dict, remote: dict) -> dict:
    """Merge local and remote. Local wins for top-level lists; teams merge by UUID.

    Users with allow_external: true in the local config can be moved to a
    different team by the external source. All other users are pinned to
    whatever team the local config specifies.
    """
    merged = {}

    # Top-level lists: local replaces remote entirely
    for key in ("taxonomies", "warninglists", "tags", "sharing_groups", "roles"):
        if key in local:
            merged[key] = local[key]
        elif key in remote:
            merged[key] = remote[key]

    # Index local users by email to find allow_external flags
    local_teams = {t["uuid"]: t for t in local.get("teams", []) if "uuid" in t}
    remote_teams = {t["uuid"]: t for t in remote.get("teams", []) if "uuid" in t}

    # Build index: email -> (team_uuid, user_dict, allow_external)
    local_user_index = {}
    for team_uuid, team in local_teams.items():
        for key in USER_LIST_KEYS:
            for user in team.get(key, []):
                email = user.get("email", "").lower()
                if email:
                    local_user_index[email] = (
                        team_uuid, key, user,
                        user.get("allow_external", False),
                    )

    # Build index of remote user placements: email -> (team_uuid, user_list_key, user_dict)
    remote_user_index = {}
    for team_uuid, team in remote_teams.items():
        for key in USER_LIST_KEYS:
            for user in team.get(key, []):
                email = user.get("email", "").lower()
                if email:
                    remote_user_index[email] = (team_uuid, key, user)

    # Determine final user placements.
    # For allow_external users, external source can override the team.
    # Result: team_uuid -> user_list_key -> [user_dicts]
    from collections import defaultdict
    user_placements = defaultdict(lambda: defaultdict(list))

    # Start with local placements
    placed_emails = set()
    for email, (team_uuid, list_key, user, allow_ext) in local_user_index.items():
        if allow_ext and email in remote_user_index:
            # External source can override team placement
            ext_team, ext_key, ext_user = remote_user_index[email]
            # Merge: local user fields on top of external, but use external team
            merged_user = {**ext_user, **user}
            user_placements[ext_team][ext_key].append(merged_user)
        else:
            user_placements[team_uuid][list_key].append(user)
        placed_emails.add(email)

    # Add remote-only users (not in local at all)
    for email, (team_uuid, list_key, user) in remote_user_index.items():
        if email not in placed_emails:
            user_placements[team_uuid][list_key].append(user)

    # Merge teams
    all_uuids = list(dict.fromkeys(
        list(local_teams.keys()) + list(remote_teams.keys())
    ))

    merged_teams = []
    for uuid in all_uuids:
        lt = local_teams.get(uuid, {})
        rt = remote_teams.get(uuid, {})

        # Team metadata: local wins
        team = {**rt, **lt}

        # User lists: use computed placements (not raw local/remote lists)
        for key in USER_LIST_KEYS:
            team[key] = user_placements.get(uuid, {}).get(key, [])

        # Non-user lists (servers, tags): local wins if present
        for key in ("servers", "tags"):
            if key in lt:
                team[key] = lt[key]
            elif key in rt:
                team[key] = rt[key]

        merged_teams.append(team)

    merged["teams"] = merged_teams
    return merged


def _normalize(raw: dict) -> dict:
    """Normalize shorthand forms and set defaults."""
    config = {
        "taxonomies": raw.get("taxonomies", []),
        "warninglists": raw.get("warninglists", []),
        "tags": [_normalize_tag(t) for t in raw.get("tags", [])],
        "sharing_groups": raw.get("sharing_groups", []),
        "roles": raw.get("roles", []),
        "teams": [],
    }

    for team in raw.get("teams", []):
        default_role = team.get("default_role", "User")

        t = {
            "uuid": _validate_uuid(team["uuid"], f"team '{team.get('name', '?')}'"),
            "name": team["name"],
            "description": team.get("description", ""),
            "sector": team.get("sector", ""),
            "nationality": team.get("nationality", "Not specified"),
            "type": team.get("type", ""),
            "contacts": team.get("contacts", ""),
            "default_role": default_role,
            "users": team.get("users", []),
            "feide_users": team.get("feide_users", []),
            "sync_users": team.get("sync_users", []),
            "service_users": team.get("service_users", []),
            "servers": [{**SERVER_DEFAULTS, **s} for s in team.get("servers", [])],
            "tags": [_normalize_tag(tg) for tg in team.get("tags", [])],
        }
        # Apply team's default_role and disabled default to all user types
        for u in t["users"]:
            u.setdefault("role", default_role)
            u.setdefault("disabled", False)
        for u in t["feide_users"]:
            u.setdefault("role", default_role)
            u.setdefault("disabled", False)
        for u in t["sync_users"]:
            u.setdefault("role", "Sync user")
            u.setdefault("disabled", False)
        for u in t["service_users"]:
            u.setdefault("role", default_role)
            u.setdefault("disabled", False)
        config["teams"].append(t)

    return config


def _normalize_tag(t) -> dict:
    if isinstance(t, str):
        return {"name": t, **TAG_DEFAULTS}
    return {**TAG_DEFAULTS, **t}


def _expand_env_recursive(obj):
    """Walk a config structure and expand ${VAR} in all string values."""
    if isinstance(obj, str):
        return _expand_env(obj) if "$" in obj else obj
    if isinstance(obj, dict):
        return {k: _expand_env_recursive(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_expand_env_recursive(item) for item in obj]
    return obj


# ---------------------------------------------------------------------------
# State snapshot
# ---------------------------------------------------------------------------

class MISPState:
    """Snapshot of current MISP state, fetched once per apply."""

    def __init__(self, client: MISPClient):
        self.orgs = client.get_organisations()
        self.users = client.get_users()
        self.roles = client.get_roles()
        self.servers = client.get_servers()
        self.tags = client.get_tags()
        self.taxonomies = client.get_taxonomies()
        self.warninglists = client.get_warninglists()
        self.sharing_groups = client.get_sharing_groups()

    def role_id(self, name: str) -> int | None:
        role = self.roles.get(name)
        return int(role["id"]) if role else None

    def org_id(self, uuid: str) -> int | None:
        org = self.orgs.get(uuid.lower())
        return int(org["id"]) if org else None

    def tag_id(self, name: str) -> int | None:
        tag = self.tags.get(name)
        return int(tag["id"]) if tag else None

    def refresh_tags(self, client: MISPClient):
        self.tags = client.get_tags()

    def refresh_orgs(self, client: MISPClient):
        self.orgs = client.get_organisations()

    def refresh_users(self, client: MISPClient):
        self.users = client.get_users()

    def refresh_roles(self, client: MISPClient):
        self.roles = client.get_roles()


# ---------------------------------------------------------------------------
# Apply functions
# ---------------------------------------------------------------------------

def _apply_roles(client: MISPClient, state: MISPState, roles: list) -> dict:
    """Create or update MISP roles. Returns {created: N, updated: N}."""
    counts = {"created": 0, "updated": 0}
    for role_def in roles:
        name = role_def.get("name")
        if not name:
            log.warning("skipping role with no name")
            continue

        existing = state.roles.get(name)
        role_data = {k: role_def[k] for k in role_def if k in ROLE_FIELDS}

        if not existing:
            log.info("creating role '%s'", name)
            client.post("/admin/roles/add", {"Role": role_data})
            counts["created"] += 1
        else:
            changes = {}
            for field, value in role_data.items():
                if field == "name":
                    continue
                if not _values_equal(value, existing.get(field)):
                    changes[field] = value
            if changes:
                log.info("updating role '%s': %s", name, list(changes.keys()))
                client.post(f"/admin/roles/edit/{existing['id']}", {"Role": changes})
                counts["updated"] += 1

    if counts["created"]:
        state.refresh_roles(client)
    return counts


def _apply_orgs(client: MISPClient, state: MISPState, teams: list) -> dict:
    """Create or update organisations. Returns {created: N, updated: N}."""
    counts = {"created": 0, "updated": 0}
    for team in teams:
        uuid = team["uuid"]
        existing = state.orgs.get(uuid)

        org_data = {k: team[k] for k in ORG_FIELDS}
        org_data["uuid"] = uuid
        org_data["local"] = True

        if not existing:
            log.info("creating org '%s' (%s)", team["name"], uuid)
            client.post("/admin/organisations/add", {"Organisation": org_data})
            counts["created"] += 1
        else:
            changes = {k: org_data[k] for k in ORG_FIELDS
                       if str(existing.get(k, "")) != str(org_data[k])}
            if changes:
                log.info("updating org '%s': %s", team["name"], list(changes.keys()))
                client.post(f"/admin/organisations/edit/{existing['id']}",
                            {"Organisation": changes})
                counts["updated"] += 1

    if counts["created"]:
        state.refresh_orgs(client)
    return counts


def _apply_tags(client: MISPClient, state: MISPState,
                global_tags: list, teams: list) -> dict:
    """Create or update global and org-scoped tags."""
    counts = {"created": 0, "updated": 0}

    # Global tags (org_id=0)
    for tag in global_tags:
        c = _apply_one_tag(client, state, tag, org_id=0)
        if c in counts:
            counts[c] += 1

    # Org-scoped tags
    for team in teams:
        org_id = state.org_id(team["uuid"])
        if not org_id:
            log.warning("skipping tags for unknown org %s", team["uuid"])
            continue
        for tag in team.get("tags", []):
            c = _apply_one_tag(client, state, tag, org_id=org_id)
            if c in counts:
                counts[c] += 1

    return counts


def _apply_one_tag(client: MISPClient, state: MISPState,
                   tag: dict, org_id: int) -> str:
    """Apply a single tag. Returns 'created', 'updated', or 'unchanged'."""
    existing = state.tags.get(tag["name"])
    tag_data = {
        "name": tag["name"],
        "colour": tag.get("colour", TAG_DEFAULTS["colour"]),
        "exportable": tag.get("exportable", TAG_DEFAULTS["exportable"]),
        "hide_tag": tag.get("hide_tag", TAG_DEFAULTS["hide_tag"]),
        "org_id": org_id,
    }

    if not existing:
        log.info("creating tag '%s' (org_id=%d)", tag["name"], org_id)
        client.post("/tags/add", {"Tag": tag_data})
        return "created"

    changes = {}
    for field in TAG_FIELDS:
        desired = tag_data[field]
        current = existing.get(field)
        if not _values_equal(desired, current):
            changes[field] = desired
    if changes:
        log.info("updating tag '%s': %s", tag["name"], list(changes.keys()))
        client.post(f"/tags/edit/{existing['id']}", {"Tag": changes})
        return "updated"

    return "unchanged"


def _apply_users(client: MISPClient, state: MISPState,
                 teams: list) -> dict:
    """Create or update all user types. Returns counts and set of managed emails."""
    counts = {"created": 0, "updated": 0}
    managed_emails = set()

    for team in teams:
        org_id = state.org_id(team["uuid"])
        if not org_id:
            log.warning("skipping users for unknown org %s", team["uuid"])
            continue

        # Regular users
        for user in team.get("users", []):
            managed_emails.add(user["email"].lower())
            c = _apply_one_user(client, state, user, org_id, external_auth=False)
            if c in counts:
                counts[c] += 1

        # Feide users (external auth)
        for user in team.get("feide_users", []):
            managed_emails.add(user["email"].lower())
            c = _apply_one_user(client, state, user, org_id, external_auth=True)
            if c in counts:
                counts[c] += 1

        # Sync users
        for user in team.get("sync_users", []):
            managed_emails.add(user["email"].lower())
            c = _apply_one_user(client, state, user, org_id, external_auth=False)
            if c in counts:
                counts[c] += 1

        # Service users
        for user in team.get("service_users", []):
            managed_emails.add(user["email"].lower())
            c = _apply_one_user(client, state, user, org_id, external_auth=False)
            if c in counts:
                counts[c] += 1

    return counts, managed_emails


def _apply_one_user(client: MISPClient, state: MISPState,
                    user: dict, org_id: int, external_auth: bool) -> str:
    """Apply a single user. Returns 'created', 'updated', or 'unchanged'."""
    email = user["email"].lower()
    role_id = state.role_id(user.get("role", "User"))
    if not role_id:
        log.warning("unknown role '%s' for user %s, skipping", user.get("role"), email)
        return "unchanged"

    existing = state.users.get(email)

    user_data = {
        "email": user["email"],
        "org_id": org_id,
        "role_id": role_id,
        "disabled": user.get("disabled", False),
        "termsaccepted": True,
        "change_pw": 0,
    }
    if external_auth:
        user_data["external_auth_required"] = True
        user_data["external_auth_key"] = user["email"]

    if not existing:
        # New user -- generate a strong random password (they'll use authkey or federated login)
        user_data["password"] = secrets.token_hex(32)
        log.info("creating user '%s' (org_id=%d, role=%s)", email, org_id, user.get("role"))
        result = client.post("/admin/users/add", {"User": user_data})
        # Set explicit authkey if provided in config (e.g. for sync users)
        config_authkey = user.get("authkey", "")
        if config_authkey:
            # Extract user ID from the create response (MISP nests it variably)
            user_obj = result.get("User", result) if isinstance(result, dict) else {}
            user_id = user_obj.get("id") if isinstance(user_obj, dict) else None
            if user_id:
                _set_user_authkey(int(user_id), config_authkey)
            else:
                log.warning("could not extract user id from create response for %s, "
                            "authkey not set", email)
        return "created"

    # Check for changes
    changes = {}
    for field in ("org_id", "role_id", "disabled"):
        desired = user_data[field]
        current = existing.get(field)
        if not _values_equal(desired, current):
            changes[field] = desired
    if external_auth:
        if not _values_equal(True, existing.get("external_auth_required")):
            changes["external_auth_required"] = True
        if existing.get("external_auth_key") != user["email"]:
            changes["external_auth_key"] = user["email"]

    if changes:
        log.info("updating user '%s': %s", email, list(changes.keys()))
        client.post(f"/admin/users/edit/{existing['id']}", {"User": changes})

    # Set explicit authkey if configured (always re-apply -- we can't diff hashed keys)
    config_authkey = user.get("authkey", "")
    if config_authkey:
        _set_user_authkey(int(existing["id"]), config_authkey)

    return "updated" if changes else "unchanged"


def _set_user_authkey(user_id: int, authkey: str):
    """Set a specific authkey for a user by writing directly to the DB.

    MISP's API does not allow setting custom authkeys (it always generates
    random ones). We bypass this by hashing the key with bcrypt and inserting
    into the auth_keys table directly.
    """
    try:
        import bcrypt
        import pymysql

        # Use $2y$ prefix (PHP bcrypt) instead of Python's $2b$ for compatibility
        # with PHP's password_verify() used by MISP
        authkey_hash = bcrypt.hashpw(authkey.encode(), bcrypt.gensalt()).decode()
        authkey_hash = authkey_hash.replace("$2b$", "$2y$", 1)
        authkey_start = authkey[:4]
        authkey_end = authkey[-4:]

        conn = pymysql.connect(
            host=env("MYSQL_HOST"),
            port=int(env("MYSQL_PORT")),
            user=env("MYSQL_USER"),
            password=env("MYSQL_PASSWORD"),
            database=env("MYSQL_DATABASE"),
        )
        try:
            import uuid as uuid_mod
            with conn.cursor() as cur:
                # Delete existing keys for this user, then insert the configured one
                cur.execute("DELETE FROM auth_keys WHERE user_id = %s", (user_id,))
                cur.execute(
                    "INSERT INTO auth_keys (uuid, authkey, authkey_start, authkey_end, "
                    "created, user_id, expiration) "
                    "VALUES (%s, %s, %s, %s, UNIX_TIMESTAMP(), %s, 0)",
                    (str(uuid_mod.uuid4()), authkey_hash, authkey_start, authkey_end, user_id),
                )
            conn.commit()
            log.info("set authkey for user id=%s (start=%s)", user_id, authkey_start)
        finally:
            conn.close()
    except Exception as e:
        log.warning("failed to set authkey for user id=%s: %s", user_id, e)


def _apply_servers(client: MISPClient, state: MISPState,
                   teams: list) -> dict:
    """Create or update sync servers. Returns counts and set of managed URLs."""
    counts = {"created": 0, "updated": 0}
    managed_urls = set()

    for team in teams:
        org_id = state.org_id(team["uuid"])
        if not org_id:
            continue

        for server in team.get("servers", []):
            url = server["url"].rstrip("/").lower()
            managed_urls.add(url)

            # Authkey = our credential on the remote instance.
            # 1. Explicit authkey in config (supports ${ENV_VAR} expansion)
            # 2. Keep existing authkey (on update -- MISP doesn't return it via API)
            # 3. Generate random placeholder (on create, should be replaced by config)
            config_authkey = server.get("authkey", "")
            authkey = config_authkey

            existing_server = state.servers.get(url)
            if not authkey:
                if existing_server:
                    authkey = existing_server.get("authkey", "") or secrets.token_hex(20)
                else:
                    authkey = secrets.token_hex(20)

            pull_rules = _build_rules(
                server.get("pull_tags", []),
                server.get("pull_tags_exclude", []),
                pull=True, state=state,
            )
            push_rules = _build_rules(
                server.get("push_tags", []),
                server.get("push_tags_exclude", []),
                pull=False, state=state,
            )

            server_data = {
                "name": server["name"],
                "url": server["url"].rstrip("/"),
                "authkey": authkey,
                "remote_org_id": org_id,
                "push": server.get("push", False),
                "pull": server.get("pull", True),
                "internal": server.get("internal", False),
                "self_signed": server.get("self_signed", False),
                "caching_enabled": server.get("caching_enabled", False),
                "pull_rules": json.dumps(pull_rules),
                "push_rules": json.dumps(push_rules),
            }

            if not existing_server:
                log.info("creating server '%s' (%s)", server["name"], url)
                client.post("/servers/add", {"Server": server_data})
                counts["created"] += 1
            else:
                changes = {}
                for field in SERVER_FIELDS:
                    desired = str(server_data.get(field, ""))
                    current = str(existing_server.get(field, ""))
                    if desired != current:
                        changes[field] = server_data[field]
                if changes:
                    log.info("updating server '%s': %s", server["name"], list(changes.keys()))
                    # Include config authkey in the payload if set (can't diff it)
                    if config_authkey:
                        changes["authkey"] = config_authkey
                    client.post(f"/servers/edit/{existing_server['id']}", {"Server": changes})
                    counts["updated"] += 1
                elif config_authkey:
                    # No field changes but authkey configured -- always re-apply
                    client.post(f"/servers/edit/{existing_server['id']}",
                                {"Server": {"authkey": config_authkey}})
                    log.debug("re-applied authkey for server '%s'", server["name"])

    return counts, managed_urls


def _build_rules(include_tags: list, exclude_tags: list,
                 pull: bool, state: MISPState) -> dict:
    """Build push/pull rules JSON.

    Users specify tag names (strings) in both pull_tags and push_tags config.
    This function handles the MISP protocol difference internally:
      - Pull rules: sent as tag names (remote server resolves to its own IDs)
      - Push rules: resolved to local tag IDs (integers) before sending
    """
    if pull:
        tags_or = include_tags
        tags_not = exclude_tags
    else:
        tags_or = [state.tag_id(t) for t in include_tags if state.tag_id(t)]
        tags_not = [state.tag_id(t) for t in exclude_tags if state.tag_id(t)]

    rules = {
        "tags": {"OR": tags_or, "NOT": tags_not},
        "orgs": {"OR": [], "NOT": []},
    }
    if pull:
        rules["type_attributes"] = {"NOT": []}
        rules["type_objects"] = {"NOT": []}
        rules["url_params"] = ""
    return rules


def _apply_taxonomies(client: MISPClient, state: MISPState,
                      desired: list) -> dict:
    """Enable desired taxonomies, disable unmanaged ones."""
    counts = {"enabled": 0, "disabled": 0}
    desired_set = set(desired)

    # Ensure taxonomies are loaded from disk into DB
    if desired:
        try:
            client.post("/taxonomies/update", {})
            state.taxonomies = client.get_taxonomies()
        except APIError:
            pass  # older MISP versions may not support this

    for namespace in desired:
        tax = state.taxonomies.get(namespace)
        if not tax:
            log.warning("taxonomy '%s' not found in MISP, skipping", namespace)
            continue
        if not tax.get("enabled"):
            log.info("enabling taxonomy '%s'", namespace)
            client.post(f"/taxonomies/enable/{tax['id']}", {})
            counts["enabled"] += 1

    # Disable unmanaged
    for namespace, tax in state.taxonomies.items():
        if namespace not in desired_set and tax.get("enabled"):
            log.info("disabling unmanaged taxonomy '%s'", namespace)
            client.post(f"/taxonomies/disable/{tax['id']}", {})
            counts["disabled"] += 1

    return counts


def _apply_warninglists(client: MISPClient, state: MISPState,
                        desired: list) -> dict:
    """Enable/create desired warninglists, disable unmanaged ones.

    Items can be either:
      - string: enable an existing built-in warninglist by name
      - dict: create/update a custom warninglist with inline values
    """
    counts = {"enabled": 0, "disabled": 0, "created": 0, "updated": 0}
    desired_names = set()

    for item in desired:
        if isinstance(item, str):
            # Enable existing warninglist by name
            desired_names.add(item)
            wl = state.warninglists.get(item)
            if not wl:
                log.warning("warninglist '%s' not found in MISP, skipping", item)
                continue
            if not wl.get("enabled"):
                log.info("enabling warninglist '%s'", item)
                client.post("/warninglists/toggleEnable", {"id": wl["id"], "enabled": True})
                counts["enabled"] += 1

        elif isinstance(item, dict) and "name" in item:
            # Custom warninglist with inline values
            name = item["name"]
            desired_names.add(name)
            existing = state.warninglists.get(name)

            wl_data = {
                "name": name,
                "description": item.get("description", ""),
                "version": str(item.get("version", 1)),
                "type": item.get("type", "string"),
                "category": item.get("category", "false_positive"),
            }

            # matching_attributes as indexed keys (MISP form-data style)
            matching = item.get("matching_attributes", [])
            for i, attr in enumerate(matching):
                wl_data[f"matching_attributes[{i}]"] = attr

            # Entries as CRLF-delimited string
            values = item.get("values", [])
            wl_data["entries"] = "\r\n".join(str(v) for v in values)

            if not existing:
                log.info("creating custom warninglist '%s' (%d entries)", name, len(values))
                try:
                    client.post("/warninglists/add", {"Warninglist": wl_data})
                    counts["created"] += 1
                except APIError as e:
                    if "already exists" in str(e):
                        # Warninglist exists but wasn't in our state -- refresh and update
                        log.info("warninglist '%s' already exists, refreshing state", name)
                        state.warninglists = client.get_warninglists()
                        existing = state.warninglists.get(name)
                    elif e.status == 302:
                        counts["created"] += 1
                    else:
                        log.error("failed to create warninglist '%s': %s", name, e)

            # Update if version changed (existing may have been set by the fallback above)
            if existing:
                if str(existing.get("version", "")) != str(item.get("version", 1)):
                    log.info("updating custom warninglist '%s' to version %s",
                             name, item.get("version"))
                    try:
                        client.post(f"/warninglists/edit/{existing['id']}",
                                    {"Warninglist": wl_data})
                        counts["updated"] += 1
                    except APIError as e:
                        log.warning("failed to update warninglist '%s': %s "
                                    "(may need manual update via MISP UI)", name, e)

            # Refresh state after creates to get IDs for enable
            if counts["created"] > 0:
                state.warninglists = client.get_warninglists()

            # Ensure enabled
            wl = state.warninglists.get(name)
            if wl and not wl.get("enabled") and item.get("enabled", True):
                log.info("enabling warninglist '%s'", name)
                client.post("/warninglists/toggleEnable",
                            {"id": wl["id"], "enabled": True})
                counts["enabled"] += 1

    # Refresh state after creates
    if counts["created"]:
        state.warninglists = client.get_warninglists()

    # Disable unmanaged
    for name, wl in state.warninglists.items():
        if name not in desired_names and wl.get("enabled"):
            log.info("disabling unmanaged warninglist '%s'", name)
            client.post("/warninglists/toggleEnable", {"id": wl["id"], "enabled": False})
            counts["disabled"] += 1

    return counts


def _apply_sharing_groups(client: MISPClient, state: MISPState,
                          desired_sgs: list) -> dict:
    """Create or update sharing groups and reconcile org membership."""
    counts = {"created": 0, "updated": 0}

    for sg in desired_sgs:
        sg_data = {k: sg.get(k, "") for k in SG_FIELDS}
        if "uuid" in sg:
            sg_data["uuid"] = _validate_uuid(sg["uuid"], f"sharing group '{sg['name']}'")

        # Lookup by UUID then name
        key = sg_data.get("uuid", "") or sg["name"]
        existing = state.sharing_groups.get(key)
        if not existing and "uuid" in sg_data:
            # Try name fallback
            existing = state.sharing_groups.get(sg["name"])

        if not existing:
            log.info("creating sharing group '%s'", sg["name"])
            result = client.post("/sharing_groups/add", {"SharingGroup": sg_data})
            sg_id = result.get("SharingGroup", result).get("id")
            if sg_id:
                _sync_sg_orgs(client, state, sg_id, sg.get("organisations", []))
            counts["created"] += 1
        else:
            sg_id = existing["id"]
            changes = {k: sg_data[k] for k in SG_FIELDS
                       if str(existing.get(k, "")) != str(sg_data[k])}
            if changes:
                log.info("updating sharing group '%s': %s", sg["name"], list(changes.keys()))
                client.post(f"/sharing_groups/edit/{sg_id}", {"SharingGroup": changes})
                counts["updated"] += 1
            _sync_sg_orgs(client, state, sg_id, sg.get("organisations", []),
                          current_orgs=existing.get("SharingGroupOrg", []))

    return counts


def _sync_sg_orgs(client: MISPClient, state: MISPState,
                  sg_id: int, desired_orgs: list,
                  current_orgs: list | None = None):
    """Add/remove orgs from a sharing group to match desired state."""
    # Parse desired orgs
    desired_map = {}  # org_id -> extend
    for org_ref in desired_orgs:
        if isinstance(org_ref, str):
            oid = state.org_id(org_ref)
            if oid:
                desired_map[oid] = False
        elif isinstance(org_ref, dict):
            oid = state.org_id(org_ref["uuid"])
            if oid:
                desired_map[oid] = org_ref.get("extend", False)

    # Parse current orgs
    current_ids = set()
    if current_orgs:
        for entry in current_orgs:
            if isinstance(entry, dict):
                org = entry.get("Organisation", entry)
                oid = org.get("id") or entry.get("org_id")
                if oid:
                    current_ids.add(int(oid))

    # Add missing
    for oid, extend in desired_map.items():
        if oid not in current_ids:
            log.info("adding org %d to sharing group %s (extend=%s)", oid, sg_id, extend)
            try:
                client.post(f"/sharing_groups/addOrg/{sg_id}",
                            {"org_id": oid, "extend": extend})
            except APIError as e:
                if "already" in str(e).lower():
                    log.debug("org %d already in sharing group %s", oid, sg_id)
                else:
                    raise

    # Remove extra
    for oid in current_ids:
        if oid not in desired_map:
            log.info("removing org %d from sharing group %s", oid, sg_id)
            client.post(f"/sharing_groups/removeOrg/{sg_id}", {"org_id": oid})


def _disable_unmanaged_users(client: MISPClient, state: MISPState,
                             managed_emails: set):
    """Disable users not in managed set (never touches user ID 1)."""
    count = 0

    for email, user in state.users.items():
        if email in managed_emails:
            continue
        # Never disable the site admin (user ID 1)
        if user.get("id") == 1 or str(user.get("id")) == "1":
            continue
        if not user.get("disabled"):
            log.info("disabling unmanaged user '%s'", email)
            try:
                client.post(f"/admin/users/edit/{user['id']}",
                            {"User": {"disabled": True}})
                count += 1
            except APIError as e:
                log.warning("failed to disable user %s: %s", email, e)

    return count


def _disable_unmanaged_servers(client: MISPClient, state: MISPState,
                               managed_urls: set):
    """Disable push/pull on servers not in managed set."""
    count = 0
    for url, server in state.servers.items():
        if url in managed_urls:
            continue
        if server.get("push") or server.get("pull"):
            log.info("disabling unmanaged server '%s'", url)
            try:
                client.post(f"/servers/edit/{server['id']}",
                            {"Server": {"push": False, "pull": False}})
                count += 1
            except APIError as e:
                log.warning("failed to disable server %s: %s", url, e)

    return count


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

def apply(client: MISPClient | None = None, config: dict | None = None) -> dict:
    """Apply declarative org/team config to MISP. Returns summary dict."""
    if not config:
        config = load_config()

    if not config.get("teams") and not config.get("taxonomies") and \
       not config.get("tags") and not config.get("sharing_groups") and \
       not config.get("warninglists") and not config.get("roles"):
        log.info("no org config to apply")
        return {}

    if not client:
        base_url = env("MISP_BASEURL")
        api_key = env("ADMIN_KEY")
        if not api_key:
            log.error("ADMIN_KEY not set, cannot run org sync")
            return {"error": "ADMIN_KEY not set"}
        client = MISPClient(base_url, api_key)

    log.info("fetching current MISP state")
    state = MISPState(client)

    summary = {}
    teams = config.get("teams", [])

    # 0. Roles (must exist before users reference them)
    try:
        summary["roles"] = _apply_roles(client, state, config.get("roles", []))
    except Exception as e:
        log.error("failed applying roles: %s", e)
        summary["roles"] = {"error": str(e)}

    # 1. Organisations
    try:
        summary["organisations"] = _apply_orgs(client, state, teams)
    except Exception as e:
        log.error("failed applying organisations: %s", e)
        summary["organisations"] = {"error": str(e)}
        return summary  # Can't continue without orgs

    # 2. Tags (global + org-scoped)
    try:
        summary["tags"] = _apply_tags(client, state, config.get("tags", []), teams)
    except Exception as e:
        log.error("failed applying tags: %s", e)
        summary["tags"] = {"error": str(e)}

    # 3. Refresh tags (need IDs for push rules)
    state.refresh_tags(client)

    # 4. Users
    try:
        user_counts, managed_emails = _apply_users(client, state, teams)
        summary["users"] = user_counts
    except Exception as e:
        log.error("failed applying users: %s", e)
        summary["users"] = {"error": str(e)}
        managed_emails = set()

    # 5. Servers
    state.refresh_users(client)
    try:
        server_counts, managed_urls = _apply_servers(client, state, teams)
        summary["servers"] = server_counts
    except Exception as e:
        log.error("failed applying servers: %s", e)
        summary["servers"] = {"error": str(e)}
        managed_urls = set()

    # 6. Taxonomies
    try:
        summary["taxonomies"] = _apply_taxonomies(
            client, state, config.get("taxonomies", []))
    except Exception as e:
        log.error("failed applying taxonomies: %s", e)
        summary["taxonomies"] = {"error": str(e)}

    # 7. Warninglists
    try:
        summary["warninglists"] = _apply_warninglists(
            client, state, config.get("warninglists", []))
    except Exception as e:
        log.error("failed applying warninglists: %s", e)
        summary["warninglists"] = {"error": str(e)}

    # 8. Sharing groups
    try:
        summary["sharing_groups"] = _apply_sharing_groups(
            client, state, config.get("sharing_groups", []))
    except Exception as e:
        log.error("failed applying sharing groups: %s", e)
        summary["sharing_groups"] = {"error": str(e)}

    # 9. Disable unmanaged
    try:
        summary["disabled_users"] = _disable_unmanaged_users(
            client, state, managed_emails)
        summary["disabled_servers"] = _disable_unmanaged_servers(
            client, state, managed_urls)
    except Exception as e:
        log.error("failed disabling unmanaged resources: %s", e)

    log.info("sync summary: %s", summary)
    return summary

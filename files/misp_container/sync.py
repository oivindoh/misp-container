"""Declarative org/team sync engine.

Loads the YAML config file, diffs it against the current MISP state through
the REST API, and applies the changes. Designed to be fast on warm runs:
with nothing changed, it only reads.
"""

import json
import os
import re
import secrets
import sys
import time
import urllib.request

import yaml

from misp_container import db
from misp_container.admin import has_authkey, has_password
from misp_container.api import MISPClient, APIError
from misp_container.env import env
from misp_container.log import get as getlog, setup as setup_logging

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

# Team keys of earlier formats, and what replaces each in the one users list
RETIRED_USER_LISTS = {
    "feide_users": "external_auth: true",
    "sync_users": "role: Sync user",
    "service_users": "a role",
}

# The lists apply() reconciles; a config without any of them is empty
RESOURCE_KEYS = ("teams", "roles", "tags", "taxonomies", "warninglists", "sharing_groups")

# The model MISP nests each kind of record under, and its add and edit paths
MODELS = {
    "role": ("Role", "/admin/roles/add", "/admin/roles/edit/{id}"),
    "org": ("Organisation", "/admin/organisations/add", "/admin/organisations/edit/{id}"),
    "tag": ("Tag", "/tags/add", "/tags/edit/{id}"),
    "user": ("User", "/admin/users/add", "/admin/users/edit/{id}"),
    "server": ("Server", "/servers/add", "/servers/edit/{id}"),
    "sharing group": ("SharingGroup", "/sharing_groups/add", "/sharing_groups/edit/{id}"),
}


class ConfigError(Exception):
    """The org config cannot be loaded as the deployment set it up."""


def is_empty(config: dict) -> bool:
    return not any(config.get(key) for key in RESOURCE_KEYS)


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
# Config loading
# ---------------------------------------------------------------------------

def load_config(file_path: str | None = None) -> dict:
    """Load the config file, expand ${VAR} and normalise it."""
    return _normalize(_expand_env_recursive(_load_local(file_path or env("ORG_CONFIG_FILE"))))


def _load_local(path: str) -> dict:
    if not os.path.isfile(path):
        return {}
    with open(path) as f:
        return yaml.safe_load(f) or {}


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
        for key, replacement in RETIRED_USER_LISTS.items():
            if key in team:
                raise ConfigError(f"team '{team.get('name', '?')}': {key} is gone; "
                                  f"list these users under users, with {replacement}")

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
            "servers": [{**SERVER_DEFAULTS, **s} for s in team.get("servers", [])],
            "tags": [_normalize_tag(tg) for tg in team.get("tags", [])],
        }
        for u in t["users"]:
            if "allow_external" in u:
                raise ConfigError(f"user {u.get('email', '?')}: allow_external is gone with the remote "
                                  "config source; delete it")
            u.setdefault("role", default_role)
            u.setdefault("disabled", False)
            u.setdefault("external_auth", False)
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
        self.refresh_users(client)
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
        """The users, with the login fields that only the database holds."""
        self.users = client.get_users()
        for row in db.dict_query("SELECT email, sub, external_auth_required, external_auth_key FROM users"):
            user = self.users.get(str(row["email"]).lower())
            if user is not None:
                user["sub"] = row["sub"] or ""
                user["external_auth_required"] = row["external_auth_required"]
                user["external_auth_key"] = row["external_auth_key"] or ""

    def refresh_roles(self, client: MISPClient):
        self.roles = client.get_roles()


# ---------------------------------------------------------------------------
# Apply functions
# ---------------------------------------------------------------------------

def _reconcile(client: MISPClient, kind: str, label: str, existing: dict | None, desired: dict,
               fields, unread: dict | None = None) -> tuple[str, dict | None]:
    """Create a missing record, or post the fields of desired that differ from existing.

    unread holds values MISP never returns, such as a server's authkey: they go
    with every edit, so a changed one reaches MISP. Returns the outcome
    ('created', 'updated' or 'unchanged') and MISP's answer to the post.
    """
    model, add, edit = MODELS[kind]
    if existing is None:
        log.info("creating %s '%s'", kind, label)
        return "created", client.post(add, {model: desired})
    changes = {f: desired[f] for f in fields
               if f in desired and not _values_equal(desired[f], existing.get(f, ""))}
    if changes:
        log.info("updating %s '%s': %s", kind, label, list(changes))
    elif not unread:
        return "unchanged", None
    result = client.post(edit.format(id=existing["id"]), {model: {**changes, **(unread or {})}})
    return ("updated" if changes else "unchanged"), result


def _apply_roles(client: MISPClient, state: MISPState, roles: list) -> dict:
    """Create or update MISP roles. Returns {created: N, updated: N}."""
    counts = {"created": 0, "updated": 0}
    for role_def in roles:
        name = role_def.get("name")
        if not name:
            log.warning("skipping role with no name")
            continue

        role_data = {k: role_def[k] for k in role_def if k in ROLE_FIELDS}
        outcome, _ = _reconcile(client, "role", name, state.roles.get(name), role_data,
                                [f for f in role_data if f != "name"])
        if outcome in counts:
            counts[outcome] += 1

    if counts["created"]:
        state.refresh_roles(client)
    return counts


def _apply_orgs(client: MISPClient, state: MISPState, teams: list) -> dict:
    """Create or update organisations. Returns {created: N, updated: N}."""
    counts = {"created": 0, "updated": 0}
    for team in teams:
        uuid = team["uuid"]
        org_data = {**{k: team[k] for k in ORG_FIELDS}, "uuid": uuid, "local": True}
        outcome, _ = _reconcile(client, "org", f"{team['name']} ({uuid})", state.orgs.get(uuid), org_data,
                                ORG_FIELDS)
        if outcome in counts:
            counts[outcome] += 1

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
    tag_data = {
        "name": tag["name"],
        "colour": tag.get("colour", TAG_DEFAULTS["colour"]),
        "exportable": tag.get("exportable", TAG_DEFAULTS["exportable"]),
        "hide_tag": tag.get("hide_tag", TAG_DEFAULTS["hide_tag"]),
        "org_id": org_id,
    }
    return _reconcile(client, "tag", tag["name"], state.tags.get(tag["name"]), tag_data, TAG_FIELDS)[0]


def _apply_users(client: MISPClient, state: MISPState,
                 teams: list) -> dict:
    """Create or update the users of every team. Returns counts and set of managed emails."""
    counts = {"created": 0, "updated": 0}
    managed_emails = set()

    for team in teams:
        org_id = state.org_id(team["uuid"])
        if not org_id:
            log.warning("skipping users for unknown org %s", team["uuid"])
            continue

        for user in team.get("users", []):
            managed_emails.add(user["email"].lower())
            c = _apply_one_user(client, state, user, org_id)
            if c in counts:
                counts[c] += 1

    return counts, managed_emails


def _apply_one_user(client: MISPClient, state: MISPState, user: dict, org_id: int) -> str:
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
    fields = ["org_id", "role_id", "disabled"]
    if user.get("external_auth"):
        user_data["external_auth_required"] = True
        user_data["external_auth_key"] = user["email"]
        fields += ["external_auth_required", "external_auth_key"]

    # A user without a password in the file works through an API key or its identity provider
    password = user.get("password", "")
    unread = None
    if existing is None:
        user_data["password"] = password or secrets.token_hex(32)
    elif password and not has_password(int(existing["id"]), password):
        unread = {"password": password}
    outcome, result = _reconcile(client, "user", email, existing, user_data, fields, unread)

    authkey = user.get("authkey", "")
    if authkey:
        if existing is None:
            # MISP nests the created user variably
            user_obj = result.get("User", result) if isinstance(result, dict) else {}
            user_id = user_obj.get("id") if isinstance(user_obj, dict) else None
        else:
            user_id = existing["id"]
        if user_id:
            _set_user_authkey(int(user_id), authkey)
        else:
            log.warning("could not extract user id from create response for %s, authkey not set", email)
    if unread and outcome == "unchanged":
        log.info("setting the password of user '%s'", email)
        return "updated"
    return outcome


def _set_user_authkey(user_id: int, authkey: str):
    """Set a specific authkey for a user by writing directly to the DB.

    MISP's API does not allow setting custom authkeys (it always generates
    random ones). We bypass this by hashing the key with bcrypt and inserting
    into the auth_keys table directly. A key the table already holds is left
    as it is: the file is the truth for this user's key, and every run would
    otherwise replace the row.
    """
    try:
        import bcrypt
        import uuid as uuid_mod

        if has_authkey(user_id, authkey):
            return

        # Use $2y$ prefix (PHP bcrypt) instead of Python's $2b$ for compatibility
        # with PHP's password_verify() used by MISP
        authkey_hash = bcrypt.hashpw(authkey.encode(), bcrypt.gensalt()).decode()
        authkey_hash = authkey_hash.replace("$2b$", "$2y$", 1)
        authkey_start = authkey[:4]
        authkey_end = authkey[-4:]

        # Delete existing keys for this user, then insert the configured one
        db.execute("DELETE FROM auth_keys WHERE user_id = %s", (user_id,))
        db.execute(
            "INSERT INTO auth_keys (uuid, authkey, authkey_start, authkey_end, "
            f"created, user_id, expiration) VALUES (%s, %s, %s, %s, {db.NOW_EPOCH}, %s, 0)",
            (str(uuid_mod.uuid4()), authkey_hash, authkey_start, authkey_end, user_id),
        )
        log.info("set authkey for user id=%s (start=%s)", user_id, authkey_start)
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
                server["pull_tags"],
                server["pull_tags_exclude"],
                pull=True, state=state,
            )
            push_rules = _build_rules(
                server["push_tags"],
                server["push_tags_exclude"],
                pull=False, state=state,
            )

            server_data = {
                "name": server["name"],
                "url": server["url"].rstrip("/"),
                "authkey": authkey,
                "remote_org_id": org_id,
                # _normalize gave every field its SERVER_DEFAULTS value
                "push": server["push"],
                "pull": server["pull"],
                "internal": server["internal"],
                "self_signed": server["self_signed"],
                "caching_enabled": server["caching_enabled"],
                "pull_rules": json.dumps(pull_rules),
                "push_rules": json.dumps(push_rules),
            }

            # MISP never returns a server's authkey, so a configured one goes with every edit
            outcome, _ = _reconcile(client, "server", f"{server['name']} ({url})", existing_server, server_data,
                                    SERVER_FIELDS, {"authkey": config_authkey} if config_authkey else None)
            if outcome in counts:
                counts[outcome] += 1

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
        # Only the fields the file sets: MISP keeps its defaults for the rest, and
        # an empty string sent for them would differ from those on every run
        sg_data = {k: sg[k] for k in SG_FIELDS if k in sg}
        if "uuid" in sg:
            sg_data["uuid"] = _validate_uuid(sg["uuid"], f"sharing group '{sg['name']}'")

        # Lookup by UUID then name
        key = sg_data.get("uuid", "") or sg["name"]
        existing = state.sharing_groups.get(key)
        if not existing and "uuid" in sg_data:
            # Try name fallback
            existing = state.sharing_groups.get(sg["name"])

        outcome, result = _reconcile(client, "sharing group", sg["name"], existing, sg_data, list(sg_data))
        if outcome in counts:
            counts[outcome] += 1
        if existing is None:
            sg_id = (result or {}).get("SharingGroup", result or {}).get("id")
            if sg_id:
                _sync_sg_orgs(client, state, sg_id, sg.get("organisations", []))
        else:
            _sync_sg_orgs(client, state, existing["id"], sg.get("organisations", []),
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
                             managed_emails: set | None):
    """Disable the local users the file does not name (never user ID 1).

    A user with an OIDC subject or external auth belongs to its identity
    provider. LdapAuth marks none of the users it creates, so with an LDAP
    login plugin on, no unnamed user is disabled. None means the users step
    failed before it knew what it manages, so nothing is unmanaged.
    """
    count = 0
    if managed_emails is None:
        log.warning("the users step failed; no user is disabled this run")
        return count
    if "true" in (env("LDAPAUTH_ENABLE"), env("APACHESECUREAUTH_LDAP_ENABLE")):
        log.info("an LDAP login plugin is on and marks none of its users; no unnamed user is disabled")
        return count

    for email, user in state.users.items():
        if email in managed_emails:
            continue
        # Never disable the site admin (user ID 1)
        if user.get("id") == 1 or str(user.get("id")) == "1":
            continue
        if user.get("sub") or _values_equal(True, user.get("external_auth_required")):
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
                               managed_urls: set | None):
    """Disable push/pull on servers not in managed set; None (the servers step failed) disables none."""
    count = 0
    if managed_urls is None:
        log.warning("the servers step failed; no server is disabled this run")
        return count
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

def apply(client: MISPClient, config: dict) -> dict:
    """Apply declarative org/team config to MISP. Returns summary dict."""
    if is_empty(config):
        log.info("no org config to apply")
        return {}

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
        managed_emails = None

    # 5. Servers
    state.refresh_users(client)
    try:
        server_counts, managed_urls = _apply_servers(client, state, teams)
        summary["servers"] = server_counts
    except Exception as e:
        log.error("failed applying servers: %s", e)
        summary["servers"] = {"error": str(e)}
        managed_urls = None

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


# ---------------------------------------------------------------------------
# The org-sync role
# ---------------------------------------------------------------------------

def wait_for_login_page(base_url: str, timeout: int = 360) -> None:
    """Exit 1 unless the login page answers 200 within the timeout."""
    log.info("waiting for MISP at %s", base_url)
    deadline = time.monotonic() + timeout
    while True:
        try:
            req = urllib.request.Request(f"{base_url.rstrip('/')}/users/login", headers={"Accept": "text/html"})
            with urllib.request.urlopen(req, timeout=5) as resp:
                if resp.status == 200:
                    log.info("MISP is ready")
                    return
        except Exception:
            pass
        if time.monotonic() > deadline:
            log.error("MISP did not answer within %d s", timeout)
            sys.exit(1)
        time.sleep(3)


def main(argv: list[str]) -> None:
    """Wait for MISP, then apply orgs.yaml (or the file argv names) through the API.

    Runs as the org-sync Job, or by hand:
    python3 -m misp_container.sync [/path/to/orgs.yaml]
    """
    setup_logging("sync")
    log.info("MISP org sync starting")
    base_url = env("SYNC_BASE_URL", env("MISP_BASEURL"))
    api_key = env("ADMIN_KEY")
    if not api_key:
        log.info("ADMIN_KEY not set, skipping org sync")
        sys.exit(0)
    # The web pods of an older revision may still serve: apply orgs.yaml only once
    # this revision's configure run finished
    db.wait_for_db()
    db.wait_for_configure()
    try:
        config = load_config(file_path=argv[0] if argv else None)
    except (ConfigError, ValueError) as e:
        log.error("cannot load the org config: %s", e)
        sys.exit(1)
    if is_empty(config):
        log.info("no org config to apply, exiting")
        sys.exit(0)

    wait_for_login_page(base_url)
    # The configure step and the migrate Job hold this lock while they write; a
    # sync that starts during one waits for it
    db.acquire_config_lock()
    try:
        summary = apply(client=MISPClient(base_url, api_key), config=config)
    finally:
        db.release_config_lock()

    errors = [v for v in summary.values() if isinstance(v, dict) and "error" in v]
    # The run's count in its system_settings row, for the metrics exporter
    try:
        db.record_run("org-sync", not errors)
    except Exception as e:
        log.warning("could not record the sync run: %s", e)
    if errors:
        log.error("sync completed with errors: %s", summary)
        sys.exit(1)
    log.info("sync completed successfully")


if __name__ == "__main__":
    main(sys.argv[1:])

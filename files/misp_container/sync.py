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


class StepError(Exception):
    """Items of a step failed; the step applied the others."""


def is_empty(config: dict) -> bool:
    return not any(config.get(key) for key in RESOURCE_KEYS)


def _expand_env(value: str) -> str:
    """Replace each ${VAR} with the variable's value; an unset variable is an error.

    A $ without braces stays as it is, so a password can hold one.
    """
    def value_of(match):
        name = match.group(1)
        if name not in os.environ:
            raise ConfigError(f"${{{name}}} is not set")
        return os.environ[name]
    return re.sub(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", value_of, value)


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
    """Normalize shorthand forms and set defaults.

    taxonomies and warninglists stay None when the file lacks the key: the run then
    leaves those of MISP as they are.
    """
    config = {
        "taxonomies": raw.get("taxonomies"),
        "warninglists": raw.get("warninglists"),
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
            "servers": [{**SERVER_DEFAULTS, **s} for s in team.get("servers", [])],
            "tags": [_normalize_tag(tg) for tg in team.get("tags", [])],
        }
        for u in t["users"]:
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
        return _expand_env(obj) if "${" in obj else obj
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


def _each(kind: str, items, label, apply_one, counts: dict) -> None:
    """apply_one(item) for every item, counting each outcome it returns.

    A failed item fails the step once the others ran: the run exits 1, and a
    failed step disables nothing of its kind.
    """
    failed = []
    for item in items:
        try:
            outcome = apply_one(item)
        except Exception as e:
            log.error("%s '%s': %s", kind, label(item), e)
            failed.append(label(item))
            continue
        if outcome in counts:
            counts[outcome] += 1
    if failed:
        raise StepError(f"{kind}: {len(failed)} failed ({', '.join(failed)})")


def _apply_roles(client: MISPClient, state: MISPState, roles: list) -> dict:
    """Create or update MISP roles. Returns {created: N, updated: N}."""
    counts = {"created": 0, "updated": 0}

    def apply_one(role_def):
        if not role_def.get("name"):
            raise ValueError("the role has no name")
        role_data = {k: role_def[k] for k in role_def if k in ROLE_FIELDS}
        return _reconcile(client, "role", role_def["name"], state.roles.get(role_def["name"]), role_data,
                          [f for f in role_data if f != "name"])[0]

    try:
        _each("role", roles, lambda r: r.get("name", "?"), apply_one, counts)
    finally:
        if counts["created"]:
            state.refresh_roles(client)
    return counts


def _apply_orgs(client: MISPClient, state: MISPState, teams: list) -> dict:
    """Create or update organisations. Returns {created: N, updated: N}."""
    counts = {"created": 0, "updated": 0}

    def apply_one(team):
        uuid = team["uuid"]
        org_data = {**{k: team[k] for k in ORG_FIELDS}, "uuid": uuid, "local": True}
        return _reconcile(client, "org", f"{team['name']} ({uuid})", state.orgs.get(uuid), org_data,
                          ORG_FIELDS)[0]

    try:
        _each("organisation", teams, lambda t: t["name"], apply_one, counts)
    finally:
        if counts["created"]:
            state.refresh_orgs(client)
    return counts


def _team_items(state: MISPState, teams: list, key: str) -> list:
    """(item, org ID) for each entry of a team list; the org ID is None for an org MISP lacks."""
    return [(item, state.org_id(team["uuid"])) for team in teams for item in team.get(key, [])]


def _in_org(apply_one):
    """apply_one(item, org_id) for an (item, org ID) pair, refusing an org MISP lacks."""
    def apply(pair):
        item, org_id = pair
        if org_id is None:
            raise ValueError("its team's organisation is not in MISP")
        return apply_one(item, org_id)
    return apply


def _apply_tags(client: MISPClient, state: MISPState,
                global_tags: list, teams: list) -> dict:
    """Create or update global (org_id 0) and org-scoped tags."""
    counts = {"created": 0, "updated": 0}
    items = [(tag, 0) for tag in global_tags] + _team_items(state, teams, "tags")
    _each("tag", items, lambda pair: pair[0]["name"],
          _in_org(lambda tag, org_id: _apply_one_tag(client, state, tag, org_id)), counts)
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
                 teams: list) -> tuple[dict, set]:
    """Create or update the users of every team. Returns counts and set of managed emails."""
    counts = {"created": 0, "updated": 0}
    items = _team_items(state, teams, "users")
    managed_emails = {user["email"].lower() for user, _ in items}
    _each("user", items, lambda pair: pair[0]["email"],
          _in_org(lambda user, org_id: _apply_one_user(client, state, user, org_id)), counts)
    return counts, managed_emails


def _apply_one_user(client: MISPClient, state: MISPState, user: dict, org_id: int) -> str:
    """Apply a single user. Returns 'created', 'updated', or 'unchanged'."""
    email = user["email"].lower()
    role_id = state.role_id(user.get("role", "User"))
    if not role_id:
        raise ValueError(f"role '{user.get('role')}' is not in MISP")

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
        if not user_id:
            raise ValueError("MISP's answer to the create holds no user ID, so the authkey is not set")
        _set_user_authkey(int(user_id), authkey)
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


def _apply_servers(client: MISPClient, state: MISPState,
                   teams: list) -> tuple[dict, set]:
    """Create or update sync servers. Returns counts and set of managed URLs."""
    counts = {"created": 0, "updated": 0}
    items = _team_items(state, teams, "servers")
    managed_urls = {server["url"].rstrip("/").lower() for server, _ in items}

    def apply_one(server, org_id):
        url = server["url"].rstrip("/").lower()
        # Our credential on the partner. MISP never returns it, so a configured one goes
        # with every edit; MISP needs one to create the server, and a random one stands in
        config_authkey = server.get("authkey", "")
        server_data = {
            "name": server["name"],
            "url": server["url"].rstrip("/"),
            "authkey": config_authkey or secrets.token_hex(20),
            "remote_org_id": org_id,
            # _normalize gave every field its SERVER_DEFAULTS value
            "push": server["push"],
            "pull": server["pull"],
            "internal": server["internal"],
            "self_signed": server["self_signed"],
            "caching_enabled": server["caching_enabled"],
            "pull_rules": json.dumps(_build_rules(server["pull_tags"], server["pull_tags_exclude"],
                                                  pull=True, state=state)),
            "push_rules": json.dumps(_build_rules(server["push_tags"], server["push_tags_exclude"],
                                                  pull=False, state=state)),
        }
        return _reconcile(client, "server", f"{server['name']} ({url})", state.servers.get(url), server_data,
                          SERVER_FIELDS, {"authkey": config_authkey} if config_authkey else None)[0]

    _each("server", items, lambda pair: pair[0]["url"], _in_org(apply_one), counts)
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
        # A push rule that lost its tag would push every event to the partner
        unknown = [t for t in include_tags + exclude_tags if state.tag_id(t) is None]
        if unknown:
            raise ValueError(f"push tags not in MISP: {', '.join(unknown)}")
        tags_or = [state.tag_id(t) for t in include_tags]
        tags_not = [state.tag_id(t) for t in exclude_tags]

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
    """Enable the taxonomies the file names, then disable the others."""
    counts = {"enabled": 0, "disabled": 0}
    if desired:
        # Loads the taxonomies of the image into the database
        client.post("/taxonomies/update", {})
        state.taxonomies = client.get_taxonomies()

    def enable(namespace):
        tax = state.taxonomies.get(namespace)
        if not tax:
            raise ValueError("not in MISP")
        if tax.get("enabled"):
            return "unchanged"
        log.info("enabling taxonomy '%s'", namespace)
        client.post(f"/taxonomies/enable/{tax['id']}", {})
        return "enabled"

    def disable(namespace):
        log.info("disabling unmanaged taxonomy '%s'", namespace)
        client.post(f"/taxonomies/disable/{state.taxonomies[namespace]['id']}", {})
        return "disabled"

    _each("taxonomy", desired, str, enable, counts)
    unnamed = [ns for ns, tax in state.taxonomies.items() if ns not in desired and tax.get("enabled")]
    _each("taxonomy", unnamed, str, disable, counts)
    return counts


def _apply_warninglists(client: MISPClient, state: MISPState,
                        desired: list) -> dict:
    """Enable or create the warninglists the file names, then disable the others.

    Items can be either:
      - string: enable an existing built-in warninglist by name
      - dict: create/update a custom warninglist with inline values
    """
    counts = {"enabled": 0, "disabled": 0, "created": 0, "updated": 0}

    def apply_one(item):
        if isinstance(item, dict) and "name" in item:
            return _apply_custom_warninglist(client, state, item, counts)
        if not isinstance(item, str):
            raise ValueError("an entry is neither a name nor a list with a name")
        wl = state.warninglists.get(item)
        if not wl:
            raise ValueError("not in MISP")
        if wl.get("enabled"):
            return "unchanged"
        log.info("enabling warninglist '%s'", item)
        client.post("/warninglists/toggleEnable", {"id": wl["id"], "enabled": True})
        return "enabled"

    def disable(name):
        log.info("disabling unmanaged warninglist '%s'", name)
        client.post("/warninglists/toggleEnable", {"id": state.warninglists[name]["id"], "enabled": False})
        return "disabled"

    _each("warninglist", desired, lambda item: item.get("name", "?") if isinstance(item, dict) else str(item),
          apply_one, counts)
    names = {item["name"] if isinstance(item, dict) else item for item in desired}
    unnamed = [name for name, wl in state.warninglists.items() if name not in names and wl.get("enabled")]
    _each("warninglist", unnamed, str, disable, counts)
    return counts


def _apply_custom_warninglist(client: MISPClient, state: MISPState, item: dict, counts: dict) -> None:
    """Create a custom warninglist, or update it when its version changed, and enable it."""
    name = item["name"]
    version = str(item.get("version", 1))
    wl_data = {
        "name": name,
        "description": item.get("description", ""),
        "version": version,
        "type": item.get("type", "string"),
        "category": item.get("category", "false_positive"),
    }
    # matching_attributes as indexed keys (MISP form-data style)
    for i, attr in enumerate(item.get("matching_attributes", [])):
        wl_data[f"matching_attributes[{i}]"] = attr
    # Entries as CRLF-delimited string
    values = item.get("values", [])
    wl_data["entries"] = "\r\n".join(str(v) for v in values)

    existing = state.warninglists.get(name)
    if existing is None:
        log.info("creating custom warninglist '%s' (%d entries)", name, len(values))
        try:
            client.post("/warninglists/add", {"Warninglist": wl_data})
            counts["created"] += 1
        except APIError as e:
            # A list that the index left out
            if "already exists" not in str(e):
                raise
        state.warninglists = client.get_warninglists()
        existing = state.warninglists.get(name)
        if existing is None:
            raise ValueError("MISP lists no warninglist of this name after the create")
    if str(existing.get("version", "")) != version:
        log.info("updating custom warninglist '%s' to version %s", name, version)
        client.post(f"/warninglists/edit/{existing['id']}", {"Warninglist": wl_data})
        counts["updated"] += 1
    if not existing.get("enabled") and item.get("enabled", True):
        log.info("enabling warninglist '%s'", name)
        client.post("/warninglists/toggleEnable", {"id": existing["id"], "enabled": True})
        counts["enabled"] += 1


def _apply_sharing_groups(client: MISPClient, state: MISPState,
                          desired_sgs: list) -> dict:
    """Create or update sharing groups and reconcile org membership."""
    counts = {"created": 0, "updated": 0}
    # MISP gives every sharing group a UUID; a file entry without one finds it by its name
    by_name = {sg["name"]: sg for sg in state.sharing_groups.values()}

    def apply_one(sg):
        # Only the fields the file sets: MISP keeps its defaults for the rest, and
        # an empty string sent for them would differ from those on every run
        sg_data = {k: sg[k] for k in SG_FIELDS if k in sg}
        if "uuid" in sg:
            sg_data["uuid"] = _validate_uuid(sg["uuid"], f"sharing group '{sg['name']}'")
        existing = state.sharing_groups.get(sg_data.get("uuid", "")) or by_name.get(sg["name"])
        outcome, result = _reconcile(client, "sharing group", sg["name"], existing, sg_data, list(sg_data))
        if existing is None:
            sg_id = (result or {}).get("SharingGroup", result or {}).get("id")
            if not sg_id:
                raise ValueError("MISP's answer to the create holds no sharing group ID")
            _sync_sg_orgs(client, state, sg_id, sg.get("organisations", []))
        else:
            _sync_sg_orgs(client, state, existing["id"], sg.get("organisations", []),
                          current_orgs=existing.get("SharingGroupOrg", []))
        return outcome

    _each("sharing group", desired_sgs, lambda sg: sg.get("name", "?"), apply_one, counts)
    return counts


def _sync_sg_orgs(client: MISPClient, state: MISPState,
                  sg_id: int, desired_orgs: list,
                  current_orgs: list | None = None):
    """Add/remove orgs from a sharing group to match desired state."""
    desired_map = {}  # org_id -> extend
    for org_ref in desired_orgs:
        uuid, extend = (org_ref, False) if isinstance(org_ref, str) else (org_ref["uuid"], org_ref.get("extend", False))
        oid = state.org_id(uuid)
        if oid is None:
            raise ValueError(f"organisation {uuid} is not in MISP")
        desired_map[oid] = extend

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
                             managed_emails: set | None) -> int:
    """Disable the local users the file does not name (never user ID 1).

    A user with an OIDC subject or external auth belongs to its identity
    provider. LdapAuth marks none of the users it creates, so with an LDAP
    login plugin on, no unnamed user is disabled. None means the users step
    failed, so nothing is unmanaged.
    """
    counts = {"disabled": 0}
    if managed_emails is None:
        log.warning("the users step failed; no user is disabled this run")
        return 0
    if env("LDAPAUTH_ENABLE") == "true":
        log.info("the LDAP login plugin is on and marks none of its users; no unnamed user is disabled")
        return 0

    def disable(email):
        log.info("disabling unmanaged user '%s'", email)
        client.post(f"/admin/users/edit/{state.users[email]['id']}", {"User": {"disabled": True}})
        return "disabled"

    unnamed = [email for email, user in state.users.items()
               if email not in managed_emails and str(user.get("id")) != "1"
               and not (user.get("sub") or _values_equal(True, user.get("external_auth_required")))
               and not user.get("disabled")]
    _each("user", unnamed, str, disable, counts)
    return counts["disabled"]


def _disable_unmanaged_servers(client: MISPClient, state: MISPState,
                               managed_urls: set | None) -> int:
    """Disable push/pull on servers not in managed set; None (the servers step failed) disables none."""
    counts = {"disabled": 0}
    if managed_urls is None:
        log.warning("the servers step failed; no server is disabled this run")
        return 0

    def disable(url):
        log.info("disabling unmanaged server '%s'", url)
        client.post(f"/servers/edit/{state.servers[url]['id']}", {"Server": {"push": False, "pull": False}})
        return "disabled"

    unnamed = [url for url, server in state.servers.items()
               if url not in managed_urls and (server.get("push") or server.get("pull"))]
    _each("server", unnamed, str, disable, counts)
    return counts["disabled"]


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

    # 6. Taxonomies, when the file has the key
    if config.get("taxonomies") is not None:
        try:
            summary["taxonomies"] = _apply_taxonomies(client, state, config["taxonomies"])
        except Exception as e:
            log.error("failed applying taxonomies: %s", e)
            summary["taxonomies"] = {"error": str(e)}

    # 7. Warninglists, when the file has the key
    if config.get("warninglists") is not None:
        try:
            summary["warninglists"] = _apply_warninglists(client, state, config["warninglists"])
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
        summary["disabled_users"] = _disable_unmanaged_users(client, state, managed_emails)
    except Exception as e:
        log.error("failed disabling unmanaged users: %s", e)
        summary["disabled_users"] = {"error": str(e)}
    try:
        summary["disabled_servers"] = _disable_unmanaged_servers(client, state, managed_urls)
    except Exception as e:
        log.error("failed disabling unmanaged servers: %s", e)
        summary["disabled_servers"] = {"error": str(e)}

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

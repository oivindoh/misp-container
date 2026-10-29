# Declarative org sync

## TL;DR

- `orgs.yaml` declares the organisations, roles, tags, taxonomies, warninglists, sharing
  groups and sync servers. It declares only the users that do not log in through the identity
  provider: sync users, service accounts and break-glass accounts.
- People log in through Keycloak or another OpenID Connect provider. MISP creates each account
  at the first login, in the team named after the user's Keycloak organisation.
- A run disables the local accounts and the servers that the file does not name. It leaves
  every account that the identity provider backs.
- A run is idempotent, expands `${VAR}` in every value, and takes the configure lock. An unset
  variable stops the run before it changes anything.

```yaml
teams:
  - name: "cert-example"
    uuid: "2399b00e-b7f4-4fdb-aeb9-03d28e83a210"
    sector: "Government"
    users:
      - email: sync@partner.example
        role: Sync user
        authkey: "${PARTNER_SYNC_KEY}"
      - email: break-glass@cert.example
        role: Org Admin
        password: "${BREAK_GLASS_PASSWORD}"
    servers:
      - name: "Partner MISP"
        url: "https://misp.partner.example"
        authkey: "${PARTNER_AUTHKEY}"
        pull: true
        push: false
        pull_tags: ["tlp:clear", "tlp:green"]

tags:
  - name: "release-to:partners"
    colour: "#0088cc"

taxonomies:
  - tlp
  - admiralty-scale
```

`deploy/orgs.yaml.example` shows every key.

The `orgSync.orgs` value holds the content of `orgs.yaml`, which the chart puts in the
`misp-orgs` ConfigMap. `${VAR}` values come from `secrets.admin` (the `misp-admin` Secret),
or from `env` for a value that is no secret. A `$` without braces is part of the value, so a
password can hold one.

| Variable | Description |
|----------|-------------|
| `ORG_CONFIG_FILE` | Path of the file (default `/etc/misp-docker/orgs.yaml`) |
| `ADMIN_KEY` | Admin API key (required) |
| `SYNC_BASE_URL` | MISP URL the run connects to (default `MISP_BASEURL`) |

## Users

Each team has one `users` list:

| Key | Meaning |
|-----|---------|
| `email` | The account |
| `role` | A MISP role by name. Default: the team's `default_role`, else `User` |
| `authkey` | An API key the account holds, such as the key a sync partner uses here |
| `password` | The login password of a break-glass account ([operations.md](operations.md#break-glass-accounts)). MISP's password policy applies |
| `external_auth` | `true` sets MISP's external-auth fields, with the email as the key, for [header authentication](configuration.md#header-authentication) |
| `disabled` | `true` disables the account |

An account without a `password` gets a random one: it works through its API key only.

## Servers

Each team has a `servers` list: the sync partners of its organisation. `pull_tags` and
`pull_tags_exclude` go to the partner as tag names. `push_tags` and `push_tags_exclude` must
name tags in MISP, such as those of the `tags` list: a run refuses a server whose push rule
names another, because the rule would then push every event.

A server's `authkey` goes to MISP when it differs from the key the server last got: the run
keeps the SHA-256 of each in `system_settings`. A key changed in MISP's UI stays until the
file's key changes.

A team's organisation may already be in MISP as a remote organisation, such as a partner's
after a pull: a run makes it local.

## Warninglists

`warninglists` names MISP's built-in lists to enable, and defines custom ones by `name`, with
`type`, `category`, `matching_attributes` and `values`. A run creates a missing custom list
with the file's `version`, and edits one whose description, type, category, matching
attributes or values differ. MISP raises a list's version at each edit itself.

## Sharing groups

```yaml
sharing_groups:
  - name: "Sector sharing"
    uuid: "a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d"
    releasability: "Members of the sector"
    organisations:
      - "2399b00e-b7f4-4fdb-aeb9-03d28e83a210"
      - uuid: "6959402f-4acd-49bb-be1a-1f30e9064d89"
        extend: true
```

| Key | Meaning |
|-----|---------|
| `name` | The sharing group. Without a `uuid`, a run finds it by this name |
| `uuid` | Optional: the UUID of the sharing group |
| `description`, `releasability`, `active`, `roaming`, `local` | MISP's fields of the sharing group; a key the file leaves out keeps MISP's value |
| `organisations` | The members, each by its UUID, or as `uuid` and `extend`. A run adds the missing members, removes the others, and adds a member again whose `extend` changed |

Each member must be an organisation in MISP, such as a team of the file.

## Keycloak

People log in through Keycloak ([configuration.md](configuration.md#keycloak)). OidcAuth puts
each user in the MISP organisation named after the alias of the user's Keycloak organisation.
So name each team in `orgs.yaml` after its Keycloak organisation's alias, such as
`cert-example`: an alias holds no spaces. OidcAuth only finds that organisation and never
renames it, so the org sync keeps its description, sector and the rest.

Leave people out of `orgs.yaml`. OidcAuth finds an account by its OIDC subject, then by its
email, and refuses an account that has no subject yet unless `OidcAuth.allow_email_linking`
is on. So an account that the org sync creates for a person cannot log in through Keycloak.

## What a run disables

A run enables the taxonomies and warninglists that the file names and disables the others, but
only when the file has the `taxonomies` or `warninglists` key. Without the key, a run leaves
those of MISP as they are.

At the end of a run the org sync disables:

- each local account the file does not name: one with no OIDC subject and no external auth.
  It never disables user 1. Keycloak and the `check-user-validity` task handle the accounts the
  identity provider backs.
- each server the file does not name: it turns its push and pull off.

LdapAuth marks none of the accounts it creates. So with `LDAPAUTH_ENABLE` on, a run disables
no account that the file does not name.

A step applies each item, then fails when one of them failed: a rejected user, a refused
server, an unknown role or organisation, a failed disable. The run then exits 1, and the failed
step disables nothing of its kind. The next run starts over.

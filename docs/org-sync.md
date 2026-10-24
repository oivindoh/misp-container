# Declarative org sync

## TL;DR

- `orgs.yaml` declares the organisations, roles, tags, taxonomies, warninglists, sharing
  groups and sync servers. It declares only the users that do not log in through the identity
  provider: sync users, service accounts and break-glass accounts.
- People log in through Keycloak or another OpenID Connect provider. MISP creates each account
  at the first login, in the organisation whose UUID the token sends.
- A run disables the local accounts and the servers that the file does not name. It leaves
  every account that the identity provider backs.
- A run is idempotent, expands `${VAR}` in every value, and takes the configure lock.

```yaml
teams:
  - name: "CERT-Example"
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

| Where | How the file gets in | Where `${VAR}` values come from |
|-------|----------------------|---------------------------------|
| Kubernetes | The `orgSync.orgs` value: the content of `orgs.yaml`, which the chart puts in the `misp-orgs` ConfigMap | `secrets.admin` (the `misp-admin` Secret), or `env` for a value that is no secret |
| Compose | Mount it at `/etc/misp-docker/orgs.yaml` on the `sync` service | `compose-secrets.env` |

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

## Keycloak

People log in through Keycloak ([configuration.md](configuration.md#keycloak)). OidcAuth
puts each user in the organisation whose UUID the `organization` claim sends, from the
user's Keycloak group. Give that group the UUID of the team in `orgs.yaml`, so the org sync
and the logins agree on the organisation. A UUID only finds an organisation: OidcAuth never
creates or renames one, so `orgs.yaml` stays the one source of organisations.

Leave people out of `orgs.yaml`. OidcAuth finds an account by its OIDC subject, then by its
email, and refuses an account that has no subject yet unless `OidcAuth.allow_email_linking`
is on. So an account that the org sync creates for a person cannot log in through Keycloak.

## What a run disables

At the end of a run the org sync disables:

- each local account the file does not name: one with no OIDC subject and no external auth.
  It never disables user 1. Keycloak and the `userValidity` component handle the accounts the
  identity provider backs.
- each server the file does not name: it turns its push and pull off.

LdapAuth marks none of the accounts it creates. So with `LDAPAUTH_ENABLE` or
`APACHESECUREAUTH_LDAP_ENABLE` on, a run disables no account that the file does not name.

A step that fails part way (one rejected user, one refused server) disables nothing of its
kind that run: the run exits 1 and the next one starts over.

# Configuration

## TL;DR

- Every MISP setting has an env var derived from its name; `settings.yaml` holds the image's
  defaults. A set env var is enforced on every configure run, an unset one is written once.
- The variables every deployment sets are in the [README](../README.md#essential-variables).
- `DB_*` selects MariaDB, MySQL or PostgreSQL; the auth plugins switch on with one variable
  each; every pod logs to stdout and stderr, as JSON or text.

## Settings

`files/misp-config/settings.yaml` holds the defaults this image applies, grouped by where they
land. Every MISP setting has an env var derived from its name:

```
MISP.redis_host        ->  MISP_REDIS_HOST
Plugin.S3_bucket_name  ->  PLUGIN_S3_BUCKET_NAME
```

| Env var | Effect |
|---------|--------|
| set and non-empty | The value is enforced on every configure run |
| unset | The default from `settings.yaml` is written once; the setting then belongs to the MISP UI |

`files/misp-config/settings-upstream.yaml` is generated from a live MISP and lists every
setting `settings.yaml` does not name, with MISP's default and description. The image never
applies those values; the env var convention overrides any of them.

A few settings are rendered into `config.php` in every pod, because MISP reads them before
the database (Redis, supervisor, salt, encryption key, paths). Those come from the
`minimum_config` group and the same env vars.

Container-level defaults (database and Redis hosts, PHP limits, worker counts) are in
`deploy/chart/files/base.env`, which the chart and Compose both read.

## Database

| Variable | Description |
|----------|-------------|
| `DB_ENGINE` | `mysql` (MariaDB or MySQL, the default) or `postgres` |
| `DB_HOST`, `DB_PORT` | The server; the port defaults to the engine's |
| `DB_NAME`, `DB_USER`, `DB_PASSWORD` | The database and its owner (`misp-db` holds the credentials) |
| `DB_TLS` | `true` for a TLS connection |

The `MYSQL_*` names stay as aliases of `DB_*`. On PostgreSQL the database must exist with
UTF8 encoding and be owned by the user; the configure Job loads MISP's baseline into it. An
external PostgreSQL such as StackGres needs only the `DB_*` values. Keep a connection pooler
in front of it in session mode, as StackGres's is by default: the configure Job holds an
advisory lock for its whole run. The migrate Job copies a MySQL or MariaDB MISP onto
PostgreSQL ([migration.md](migration.md)). The On Demand correlation engine is
MySQL-only, and the integration suite runs on both engines. On PostgreSQL, MISP does not
create its high-performance indexes on the object, correlation, tag and warninglist tables
([MISP#11178](https://github.com/MISP/MISP/issues/11178)).
The image patches one line of CakePHP's PostgreSQL datasource (the `Dockerfile` names it) so
that settings inserts work. `MISP_REDIS_*` also fills the background-job and ZeroMQ Redis
settings, and `MISP_BASEURL` fills the external and REST client base URLs, unless those are
set explicitly.

## HTTPS

Caddy serves plain HTTP on 8080, for an ingress or load balancer in front. Set
`CADDY_ADDRESS` to a domain name for automatic HTTPS with Let's Encrypt instead:

```yaml
environment:
  CADDY_ADDRESS: misp.example.com
```

## Authentication plugins

MISP's auth plugins read their configuration from `config.php`, so their settings are groups
in `settings.yaml` that every pod renders when the plugin's switch is on. The documented
short names below are aliases of the derived ones (`OidcAuth.provider_url` is
`OIDCAUTH_PROVIDER_URL` as well).

| Switch | Group | Plugin |
|--------|-------|--------|
| `OIDC_ENABLE=true` | `oidc` | `OidcAuth`: OpenID Connect |
| `LDAPAUTH_ENABLE=true` | `ldap` | `LdapAuth`: LDAP bind with a reader account |
| `APACHESECUREAUTH_LDAP_ENABLE=true` | `apache_auth` | `ApacheSecureAuth`: a header from the proxy, looked up in LDAP |
| `CUSTOM_AUTH_ENABLE=true` | `custom_auth` | `Plugin.CustomAuth_*`: a header from the proxy, no lookup. The configure step applies this group to the database; the `CUSTOM_AUTH_*` names below are aliases of the derived ones |

### OpenID Connect

| Variable | Setting | Default |
|----------|---------|---------|
| `OIDC_PROVIDER_URL` | `OidcAuth.provider_url` | required |
| `OIDC_CLIENT_ID`, `OIDC_CLIENT_SECRET` | `OidcAuth.client_id`, `client_secret` | required; the secret belongs in `misp-app` |
| `OIDC_ISSUER` | `OidcAuth.issuer` | the provider URL |
| `OIDC_ROLES_PROPERTY` | `OidcAuth.roles_property` | `roles`: the claim with the user's roles |
| `OIDC_ROLES_MAPPING` | `OidcAuth.role_mapper` | `{}`: JSON, IdP role to MISP role id or name, first match wins. A user with no matching role is refused |
| `OIDC_DEFAULT_ORG` | `OidcAuth.default_org` | organisation id, UUID or name when the IdP sends none |
| `OIDC_SCOPES` | `OidcAuth.scopes` | `profile,email` |
| `OIDC_MIXEDAUTH` | `OidcAuth.mixedAuth` | `false`: every login goes to the IdP. `true` keeps the password form and adds a button |
| `OIDC_LOGOUT_URL` | `Plugin.CustomAuth_custom_logout` | the IdP's logout URL |
| `OIDC_AUTH_METHOD`, `OIDC_CODE_CHALLENGE_METHOD` | `authentication_method`, `code_challenge_method` | `client_secret_post`, `S256` |
| `OIDC_DISABLE_REQUEST_OBJECT` | `OidcAuth.disable_request_object` | `false`: `true` for an IdP that refuses signed request objects |
| `OIDC_SKIP_PROXY` | `OidcAuth.skipProxy` | `true`: the IdP is reached directly, not through `Proxy.*` |

The redirect URI is `MISP_BASEURL/users/login`; register it at the IdP. Every other
`OidcAuth.*` key in `settings.yaml` (offline access, user validity checks, email linking)
takes its derived env var. The integration suite logs in through a dex instance with a role
mapped by name and the default organisation.

### Header authentication

| Variable | Setting | Default |
|----------|---------|---------|
| `CUSTOM_AUTH_HEADER` | `Plugin.CustomAuth_header` | `X_FORWARDED_EMAIL` |
| `CUSTOM_AUTH_USE_HEADER_NAMESPACE`, `CUSTOM_AUTH_HEADER_NAMESPACE` | `Plugin.CustomAuth_use_header_namespace`, `header_namespace` | `true`, `HTTP_` |
| `CUSTOM_AUTH_REQUIRED` | `Plugin.CustomAuth_required` | `false`: without the header, the login page |
| `CUSTOM_AUTH_NAME` | `Plugin.CustomAuth_name` | `External Authentication` |
| `CUSTOM_AUTH_DISABLE_LOGOUT` | `Plugin.CustomAuth_disable_logout` | `false` |
| `CUSTOM_AUTH_ONLY_ALLOW_SOURCE` | `Plugin.CustomAuth_only_allow_source` | unset: the proxy's URL, when only it may log users in |
| `CUSTOM_AUTH_CUSTOM_PASSWORD_RESET`, `CUSTOM_AUTH_CUSTOM_LOGOUT` | `Plugin.CustomAuth_custom_password_reset`, `custom_logout` | unset: the external system's URLs |

### LDAP

`LDAPAUTH_LDAPSERVER`, `LDAPAUTH_LDAPDN`, `LDAPAUTH_LDAPREADERUSER`, `LDAPAUTH_LDAPREADERPASSWORD`
are required; `LDAPAUTH_LDAPSEARCHFILTER`, `LDAPAUTH_LDAPDEFAULTORGID`, `LDAPAUTH_LDAPDEFAULTROLEID`,
`LDAPAUTH_LDAPROLEFIELD` and the rest of the `ldap` group follow the plugin's README.

## Custom scripts

Two hook points for custom Python, mounted as files, skipped when absent:

| Script | Where | When |
|--------|-------|------|
| `/custom/setup.py` | configure Job | After the database is ready, before configuration |
| `/custom/pre-start.py` | every web replica | Before PHP-FPM starts |

## Logging

Every pod writes its log to stdout and stderr and keeps no log file. `LOG_FORMAT` selects the
line format: `json` (the `base.env` default, for the chart: one JSON object per line, for a
log collector) or `text` (the Compose default, coloured on a terminal).

| Source | Reaches the output as | Format |
|--------|-----------------------|--------|
| The entrypoints, the configure Job, org sync, tasks, metrics | `time`, `level`, `context`, `message` (and `exception`) | `LOG_FORMAT` |
| MISP's own log (CakeLog: job starts and ends, exceptions, warnings) | the same fields, `context` `misp` | `LOG_FORMAT` |
| Files MISP appends to directly (`server-sync.log`, `workflow-execution.log`, `exec-errors.log`, `kafka.error.log`) | one line per file line, `context` `misp:<file>`; a file is emptied past 10 MB | `LOG_FORMAT` |
| PHP errors and warnings in web pods | PHP's own line, through PHP-FPM | text |
| PHP-FPM itself | FPM's own line | text |
| Caddy access log | Caddy's own JSON | JSON |

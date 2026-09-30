# Declarative org sync

`orgs.yaml` describes organisations, users, roles, servers, tags, taxonomies, warninglists
and sharing groups. The org-sync run is idempotent, expands `${VAR}` in authkeys and URLs,
and takes the configure lock. See `deploy/orgs.yaml.example`.

```yaml
teams:
  - name: "CERT-Example"
    uuid: "2399b00e-b7f4-4fdb-aeb9-03d28e83a210"
    sector: "Government"
    users:
      - email: analyst@example.com
        role: User
      - email: sync@partner.com
        role: Sync user
        authkey: "${PARTNER_SYNC_KEY}"
    servers:
      - name: "Partner MISP"
        url: "https://misp.partner.com"
        authkey: "${PARTNER_AUTHKEY}"
        pull: true
        push: false
        pull_rules:
          tags: ["tlp:clear", "tlp:green"]

tags:
  - name: "release-to:partners"
    colour: "#0088cc"

taxonomies:
  - tlp
  - admiralty-scale
```

| Where | How the file gets in |
|-------|----------------------|
| Kubernetes | The `orgSync.orgs` value: the content of `orgs.yaml`, which the chart puts in the `misp-orgs` ConfigMap |
| Compose | Mount it at `/etc/misp-docker/orgs.yaml` on the `sync` service |

| Variable | Description |
|----------|-------------|
| `ORG_CONFIG_FILE` | Path of the file (default `/etc/misp-docker/orgs.yaml`) |
| `ORG_CONFIG_URL` | URL to fetch the file from instead |
| `ADMIN_KEY` | Admin API key (required) |
| `SYNC_BASE_URL` | MISP URL the run connects to (default `MISP_BASEURL`) |

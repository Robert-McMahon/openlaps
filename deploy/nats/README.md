# NATS credentials and TLS material

**Nothing in this directory that is secret is in this repository, and
nothing secret ever should be.** `deploy/nats/vehicle.conf` and
`deploy/nats/pit.conf` are checked in; every file they reference under
`/etc/nats/tls` and every value they expand from `$NATS_*` is generated per
deployment and mounted in. `.gitignore` already refuses `*.pem`, `*.key`,
`*.creds` and `.env`, but that is a backstop, not the rule.

## What each file is

| Path in the container | Vehicle | Pit | What it is |
| --- | --- | --- | --- |
| `/etc/nats/nats.conf` | `vehicle.conf` | `pit.conf` | Server config, read-only |
| `/etc/nats/tls/ca.pem` | ✓ | ✓ | The CA both sides trust. One CA per deployment |
| `/etc/nats/tls/server-cert.pem` + `server-key.pem` | ✓ | — | The vehicle's leafnode listener identity |
| `/etc/nats/tls/client-cert.pem` + `client-key.pem` | — | ✓ | The pit's leafnode client identity |
| `/data` | ✓ | ✓ | JetStream store. A real directory with real free space |

`$NATS_LEAF_PASSWORD` (vehicle) and `$NATS_LEAFNODE_URL` (pit) come from each
stack's own `.env`. NATS expands `$VAR` in a config file natively, so nothing
templates these files — but only when the variable is a **whole token**.
`url: tls://leaf:$NATS_LEAF_PASSWORD@$NATS_VEHICLE_HOST:7422` is dialled
*literally*, quoted or not, and the server logs
`lookup $NATS_VEHICLE_HOST: no such host` — which reads like a DNS problem
and is not one. That is why the pit's remote is one whole-URL variable
rather than a host and a password.

## Generating the TLS material

A private CA is sufficient and appropriate here: the leafnode has exactly
two endpoints, both under one owner, and neither needs to be trusted by a
browser. This is one self-contained sequence; run it somewhere outside the
repository.

```bash
mkdir -p ~/openlaps-secrets/tls && cd ~/openlaps-secrets/tls
```

```bash
openssl req -x509 -newkey rsa:4096 -sha256 -days 3650 -nodes -keyout ca-key.pem -out ca.pem -subj "/CN=openlaps-ca"
```

The vehicle's server certificate. `CN`/`subjectAltName` must match the host
the pit's `url:` names — if the pit dials `tls://…@192.168.12.176:7422`,
that IP has to be in the SAN, or the pit's TLS handshake fails with a name
mismatch that reads like a certificate problem and is a *configuration*
problem.

```bash
openssl req -newkey rsa:4096 -nodes -keyout server-key.pem -out server.csr -subj "/CN=openlaps-vehicle" -addext "subjectAltName=IP:192.168.12.176,DNS:openlaps-vehicle"
```

```bash
openssl x509 -req -in server.csr -CA ca.pem -CAkey ca-key.pem -CAcreateserial -days 825 -sha256 -out server-cert.pem -copy_extensions copyall
```

The pit's client certificate. The vehicle's listener sets `verify: true`, so
an unauthenticated dialler is rejected at the TLS layer before it can even
attempt an account login.

```bash
openssl req -newkey rsa:4096 -nodes -keyout client-key.pem -out client.csr -subj "/CN=openlaps-pit"
```

```bash
openssl x509 -req -in client.csr -CA ca.pem -CAkey ca-key.pem -CAcreateserial -days 825 -sha256 -out client-cert.pem
```

Then, on each machine, mount the three files that side needs (see the table
above) at `/etc/nats/tls`, and keep `ca-key.pem` off both — it is only ever
needed to issue a replacement certificate.

## The leafnode password

`$NATS_LEAF_PASSWORD` is the vehicle listener's `authorization` credential.
Generate a fresh one per car — **hex, not base64**:

```bash
openssl rand -hex 32
```

The password is embedded in the pit's `NATS_LEAFNODE_URL`, and base64's
alphabet includes `/` and `+`. A password containing either makes
`nats-server` refuse the config outright with
`error parsing leafnode url [...]` — a message that names the URL and not
the character in it. Found while standing up the P4.6 parity stack; hex has
the same entropy and no reserved characters.

Put it in the **vehicle** stack's `.env` as `NATS_LEAF_PASSWORD`, and embed
the same value in the **pit** stack's `NATS_LEAFNODE_URL`:

```
NATS_LEAFNODE_URL=tls://leaf:<that-password>@192.168.12.176:7422
```

The host in that URL must match the vehicle certificate's `subjectAltName`,
or the pit's TLS handshake fails with a name mismatch. Credentials live in
the URL because a leafnode remote has no separate user/password fields —
only `url`, `account`, `credentials` and `tls`. The password is a second
factor behind mutual TLS rather than the only gate, which is why a plain
one is adequate here.

## Client credentials (`OPENLAPS_NATS_CREDS`)

The two client listeners (vehicle `:4222` for the agent, pit `:4222` for the
four pit services) are unauthenticated in the checked-in configs, because in
the deployed topology each is reachable only from its own host or compose
network. If either is exposed to a wider network, add an `authorization`
block to that server's config, issue creds files, and point
`OPENLAPS_NATS_CREDS` at the mounted file — every service already reads it
(`example.env`), so no code changes.

## Rotation

Certificates expire; the leafnode simply stops connecting and the pit's
`TELE_VEHICLE` stops advancing, with the vehicle none the wiser. Re-issue
from the same CA, replace the mounted files, and restart both `nats-server`
containers. Sourcing resumes by sequence, so no data is lost by the restart
itself — that is the same property the dropout test in
`tests/test_deploy_topology.py` exercises.

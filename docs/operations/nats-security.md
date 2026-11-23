# NATS security

The vehicle leafnode listener uses mutual TLS plus a per-vehicle password.
Generate all material outside the repository and mount only the files each host
needs.

## Files

| Container path | Vehicle | Pit |
| --- | :---: | :---: |
| `/etc/nats/tls/ca.pem` | yes | yes |
| `/etc/nats/tls/server-cert.pem` | yes | no |
| `/etc/nats/tls/server-key.pem` | yes | no |
| `/etc/nats/tls/client-cert.pem` | no | yes |
| `/etc/nats/tls/client-key.pem` | no | yes |

Keep the CA private key off both deployed hosts.

## Certificate requirements

The vehicle certificate's subject alternative name must match the host or IP in
`NATS_LEAFNODE_URL`. Generate a dedicated CA, server certificate and client
certificate with OpenSSL or your existing PKI. The checked-in NATS configs
expect the filenames above.

Generate the leaf password with a URL-safe representation:

```bash
openssl rand -hex 32
```

Set the vehicle value:

```text
NATS_LEAF_PASSWORD=<generated value>
```

Set the pit remote URL with the same value:

```text
NATS_LEAFNODE_URL=tls://leaf:<generated value>@<vehicle-host>:7422
```

## Link tunnel and live frames

The link tunnel (`openlaps-link`) authenticates both ends with the same
files: the vehicle presents the server certificate and the pit the client
certificate, each verified against the CA. On a vehicle that runs the tunnel
as a systemd service, copy the server certificate, its key and the CA to
`/etc/openlaps/tls` ([Vehicle installation](vehicle.md)). The name in
`OPENLAPS_LINK_SERVER_NAME` must be in the vehicle certificate's subject
alternative names.

Live frames sent outside the tunnel are signed, not encrypted. Set the same
`OPENLAPS_LIVE_KEY` on both hosts (`openssl rand -hex 32`). The signature
stops another host on the network from faking or redirecting the gauges; it
does not hide the values.

## Client listeners

The checked-in client listeners do not require authentication. Vehicle NATS
uses host networking and pit NATS publishes port `4222`, so these listeners are
reachable on host interfaces unless a firewall restricts them. Keep them on a
trusted network or add NATS authorization and mount credentials through
`OPENLAPS_NATS_CREDS`.

## Rotation

Reissue certificates from the same CA, replace the mounted files, and restart
both NATS servers. JetStream sourcing resumes by sequence after reconnection.

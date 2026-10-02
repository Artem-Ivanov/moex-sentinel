# TBank CA certificates for the image build

`python-base.Dockerfile` adds these CA certificates to pip's trust bundle only
while installing `t-tech-investments` from `opensource.tbank.ru`. The registry
does not send its intermediate certificate. The application's TBank SDK uses
its own bundled root for gRPC and needs no system-wide trust change.

- `tbank-root.pem` came from `t_tech/invest/certs/RussianTrustedRootCA.pem` in
  the installed official `t-tech-investments` 1.49.3 package. SHA-256 X.509
  fingerprint: `D26D2D0231B7C39F92CC738512BA54103519E4405D68B5BD703E9788CA8ECF31`.
- `tbank-sub.pem` came from the TLS chain presented by
  `sandbox-invest-public-api.tbank.ru:443` on 2026-10-01. It was verified
  against the root above. SHA-256 X.509 fingerprint:
  `2155785036C900DBB5F1BB2A1569C80C55595BD6BF94867A29BBDDC7D88A3F2`.

To verify the chain after replacing either file:

```sh
openssl verify -CAfile docker/certs/tbank-root.pem docker/certs/tbank-sub.pem
openssl x509 -in docker/certs/tbank-root.pem -noout -fingerprint -sha256
openssl x509 -in docker/certs/tbank-sub.pem -noout -fingerprint -sha256
```

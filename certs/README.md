# Extra CA certificates for the Docker build

Leave this directory empty unless `docker compose build` fails with:

```
SSLError(SSLCertVerificationError(1, '[SSL: CERTIFICATE_VERIFY_FAILED] ...'))
Could not fetch URL https://pypi.org/simple/hatchling/
```

That means something between you and PyPI is inspecting TLS and re-signing
certificates with its own root — a corporate middlebox, or consumer antivirus
such as Norton Web Shield, Kaspersky or ESET. Your machine trusts that root
because the product installed it into the OS trust store; the build container
does not, so `pip` refuses the connection.

Drop the interceptor's root certificate here as a `.crt` or `.pem` file and
rebuild. Anything in this directory is added to the trust store used for the
`pip install` step, and removed from the image afterwards.

Certificates here are **git-ignored** — do not commit them.

## Exporting the root certificate

**Windows** (PowerShell) — exports the whole machine trust store, which
includes whatever the interceptor added:

```powershell
$out = "certs\local-ca.pem"
$certs = Get-ChildItem Cert:\LocalMachine\Root, Cert:\CurrentUser\Root
$sb = New-Object System.Text.StringBuilder
foreach ($c in $certs) {
  [void]$sb.AppendLine('-----BEGIN CERTIFICATE-----')
  [void]$sb.AppendLine([Convert]::ToBase64String($c.RawData, 'InsertLineBreaks'))
  [void]$sb.AppendLine('-----END CERTIFICATE-----')
}
[IO.File]::WriteAllText($out, $sb.ToString(), [Text.Encoding]::ASCII)
```

**macOS**:

```bash
security find-certificate -a -p /Library/Keychains/System.keychain > certs/local-ca.pem
```

**Linux**: copy the root your IT department supplies, usually already in
`/usr/local/share/ca-certificates/`.

## Runtime

This only covers the *build*. The running service downloads from
opendata.swiss and priminfo.admin.ch through the same interceptor, so set the
`CA_BUNDLE` environment variable to a PEM file for that — see the README.

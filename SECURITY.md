# Security

## Secrets

This project never reads API keys from YAML. The only credential it uses is
an optional MEXC web session token, loaded from the environment:

- `MEXC_WEB_TOKEN` (preferred)
- `MEXC_AUTH_TOKEN` (alias)

Put the token in a local `.env` file. `.env` is gitignored. Do not paste
tokens into `config.yaml`, issues, pull requests, or commit messages.

The token is a browser `Authorization` header that starts with `WEB`. Treat
it like a session cookie: it can trade on your account, and it can expire or
be revoked when you log out.

## Default-safe live path

Live order placement is off until **all three** are true:

1. `live.enabled: true` in `config.yaml`
2. `live.dry_run: false` in `config.yaml`
3. `--confirm` on the CLI

`record` / `replay` never construct a live client and never send orders.

## Reporting

If you find a vulnerability, open a private GitHub security advisory on this
repository instead of a public issue.

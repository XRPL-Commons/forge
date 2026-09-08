# Security

Do not open a public issue with exploit details, credentials, private repository
content, or unredacted worker output. Use GitHub's private vulnerability reporting
for this repository when available. If it is unavailable, request a private
reporting channel from the maintainer without including sensitive details.

Forge is experimental operator software. Coding agents execute tools with the
credentials and filesystem access their deployment grants. The working tmux
supervisor is not a credential-isolation boundary. The staged isolated executor
must remain disabled for live repository work until its documented gates are met.

The dashboard exposes local operational details and has no application login.
Keep it on a trusted private network. Critical changes require qualifying human
approval; an audit PASS, a shared agent identity, or a cached summary is not human
approval. Do not weaken ownership, current-candidate, or cleanup checks to increase
throughput.

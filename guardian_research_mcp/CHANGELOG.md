# Guardian Research MCP Changelog

## 0.8.0 – Read-only Guardian Research Transport

- Adds a separate provider-neutral MCP 2.2.0 add-on using stateless Streamable HTTP at `/mcp`.
- Exposes exactly 15 read-only Research tools through `GuardianResearchClient`; Guardian Research envelopes, provenance, evidence classes, coverage and cursors pass through without interpretation.
- Adds separate Bearer authentication for MCP clients, explicit Host/Origin allowlists, bounded concurrency and responses, cancellation, metadata-only audit and operational `/health`.
- Keeps running while Guardian is unavailable and recovers without restart. It does not read Guardian files or write MQTT, RS485, Hycube, maintenance, configuration or Home Assistant state.
- Publishes no host port by default and mounts neither `/share` nor `/config`.

# Security and permissions

Public observation uses no API key. Authenticated preflight and trading are separate authorities. The example environment file names separate credentials because code must not infer that a read credential can write or that an exchange offers finer permission separation than its current key configuration actually exposes.

Any later key must omit withdrawal and transfer authority, use a supported IP allow-list, remain outside Git, and be masked in logs and exception text. The system must never print headers, signatures, passphrases, raw environment variables, or private response fields unrelated to the strategy. It does not read the repository's existing `.env` during tests or public observation.

UTA management read (`GET /api/v3/account/settings`) and UTA trade read (positions/orders/fills/strategy orders) are capability checks, not permission to call management or trade writes. Account mode, hold mode, margin mode, leverage, collateral mode, automatic margin addition, and account separation are observed only. If any required state cannot be read, live validation fails closed.

The current development deliberately contains no enabled private-write transport. A later live change must separately review exact endpoint permissions and mappings, including UTA v3 versus Classic v2, server-side protection placement/query/cancel semantics, one-way reduce-only replacement behavior, ambiguous ACK reconciliation, and IP restrictions. It must not fall back from an unsupported feature to cross margin, hedge mode, borrowing, transfers, or an invented endpoint.

Research documents and external news are untrusted data. They may be hashed, classified, and summarized but cannot alter configuration, execute code, approve an order, relax a gate, or supply instructions to an LLM with tool authority. Deterministic risk and protection logic must operate without an LLM.

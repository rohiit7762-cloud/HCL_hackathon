# Payments Failover Runbook

Owner: Platform team. Applies to payments-api and ledger-db.

## Escalation path for payments outages

1. The payments on-call engineer (rotation: payments-oncall) is paged automatically.
2. If the incident is not mitigated within 15 minutes, page the platform on-call (platform-oncall).
3. After 30 minutes, escalate to the Engineering Manager, Platform (Sam Patel), who becomes incident commander.
4. For customer-facing impact longer than 60 minutes, notify the VP Engineering and Customer Support leads.

## Ledger database failover

The ledger database (ledger-db) runs on Postgres with one synchronous standby.

- Confirm the primary is unhealthy using the ledger-db health dashboard.
- Freeze writes by setting payments-api to maintenance mode.
- Promote the standby replica with the platform failover tool: `platformctl db promote ledger-db`.
- Point payments-api at the new primary and disable maintenance mode.
- Open a follow-up ticket to rebuild the old primary as a standby.

## Recovery objectives

The recovery time objective for the ledger service is 30 minutes from detection to restored writes.
The recovery point objective is 5 minutes of transactions, recovered from the write-ahead log archive.

## Connection pool exhaustion

If payments-api logs show "remaining connection slots are reserved", the ledger-db connection pool is exhausted.
Restart the PgBouncer sidecar and reduce payments-api worker concurrency to 32 until traffic normalizes.

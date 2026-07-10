-- Verified live against a self-hosted 3-node CockroachDB (cockroachdb/cockroach:latest)
-- on 2026-07-10: VECTOR column type, CREATE VECTOR INDEX, <-> distance operator,
-- AS OF SYSTEM TIME, and CREATE CHANGEFEED (core/sinkless) all confirmed working.

SET CLUSTER SETTING kv.rangefeed.enabled = true;

CREATE TABLE IF NOT EXISTS memory_items (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    scope STRING NOT NULL,
    kind STRING NOT NULL,
    content STRING NOT NULL,
    embedding VECTOR(128) NOT NULL,
    provenance_agent STRING NOT NULL,
    version INT NOT NULL DEFAULT 1,
    valid BOOL NOT NULL DEFAULT true,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_scope_kind UNIQUE (scope, kind)
);

CREATE VECTOR INDEX IF NOT EXISTS memory_items_embedding_idx ON memory_items (embedding);

CREATE TABLE IF NOT EXISTS memory_events (
    event_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    item_id UUID NOT NULL,
    op STRING NOT NULL,               -- 'create' | 'update' | 'conflict_resolve' | 'rollback'
    prev_version INT,
    new_version INT NOT NULL,
    payload JSONB NOT NULL,
    actor_agent STRING NOT NULL,
    ts TIMESTAMPTZ NOT NULL DEFAULT now(),
    INDEX idx_events_item_ts (item_id, ts)
);

CREATE TABLE IF NOT EXISTS conflicts (
    conflict_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    item_id UUID NOT NULL,
    version_a INT NOT NULL,
    version_b INT NOT NULL,
    policy STRING NOT NULL,           -- 'merge' | 'adjudicate'
    resolution JSONB NOT NULL,
    rationale STRING,
    status STRING NOT NULL DEFAULT 'resolved',  -- 'resolved' | 'unresolved'
    ts TIMESTAMPTZ NOT NULL DEFAULT now(),
    INDEX idx_conflicts_item (item_id, ts)
);

CREATE TABLE IF NOT EXISTS audit_log (
    entry_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    actor STRING NOT NULL,
    action STRING NOT NULL,
    item_id UUID,
    detail JSONB,
    ts TIMESTAMPTZ NOT NULL DEFAULT now(),
    INDEX idx_audit_ts (ts)
);

CREATE TABLE IF NOT EXISTS agents (
    agent_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name STRING NOT NULL UNIQUE,
    role STRING,
    subscriptions JSONB NOT NULL DEFAULT '[]'
);

-- Deliberately naive comparison store for the head-to-head demo: a faithful
-- read-modify-write / last-write-wins table with NO version guard. Used only
-- by backend/sim/baseline_lww.py to demonstrate the lost-update bug that
-- memory_items' optimistic CAS (see app/memory/write.py) prevents.
CREATE TABLE IF NOT EXISTS baseline_items (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    scope STRING NOT NULL,
    kind STRING NOT NULL,
    content STRING NOT NULL,
    provenance_agent STRING NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_baseline_scope_kind UNIQUE (scope, kind)
);

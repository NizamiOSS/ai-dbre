-- =============================================================================
-- AI DBRE — Database Initialization
-- =============================================================================

-- Enable monitoring extensions
CREATE EXTENSION IF NOT EXISTS pg_stat_statements;
CREATE EXTENSION IF NOT EXISTS pgstattuple;

-- Create a read-only user for the AI agent (safety first)
CREATE USER dbre_agent WITH PASSWORD 'agent_readonly';
GRANT CONNECT ON DATABASE dbre_testdb TO dbre_agent;
GRANT USAGE ON SCHEMA public TO dbre_agent;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO dbre_agent;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO dbre_agent;
GRANT pg_read_all_stats TO dbre_agent;  -- Access to pg_stat_* views
GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA public TO dbre_agent;

-- Create a remediation user for approved write operations (Phase 3)
-- This user CAN create indexes, run vacuum/analyze, but CANNOT drop or alter data
CREATE USER dbre_remediation WITH PASSWORD 'remediation_write';
GRANT CONNECT ON DATABASE dbre_testdb TO dbre_remediation;
GRANT USAGE ON SCHEMA public TO dbre_remediation;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO dbre_remediation;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO dbre_remediation;
GRANT pg_read_all_stats TO dbre_remediation;
GRANT CREATE ON SCHEMA public TO dbre_remediation;  -- For CREATE INDEX
-- CREATE INDEX requires table ownership — grant dbre_admin role membership
-- The safety whitelist in tools/remediation.py is the real security boundary,
-- not the DB permissions. This user has the power but the agent only uses
-- whitelisted operations, and every action requires human approval.
GRANT dbre_admin TO dbre_remediation;

-- =============================================================================
-- Sample Schema — E-commerce (realistic patterns for slow query testing)
-- =============================================================================

CREATE TABLE customers (
    id SERIAL PRIMARY KEY,
    email VARCHAR(255) NOT NULL,
    full_name VARCHAR(255),
    country VARCHAR(100),
    created_at TIMESTAMP DEFAULT NOW(),
    status VARCHAR(20) DEFAULT 'active'
);

CREATE TABLE products (
    id SERIAL PRIMARY KEY,
    name VARCHAR(255) NOT NULL,
    category VARCHAR(100),
    price NUMERIC(10, 2),
    stock_quantity INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE TABLE orders (
    id SERIAL PRIMARY KEY,
    customer_id INTEGER REFERENCES customers(id),
    order_date TIMESTAMP DEFAULT NOW(),
    total_amount NUMERIC(12, 2),
    status VARCHAR(30) DEFAULT 'pending',
    shipping_country VARCHAR(100),
    notes TEXT
);

CREATE TABLE order_items (
    id SERIAL PRIMARY KEY,
    order_id INTEGER REFERENCES orders(id),
    product_id INTEGER REFERENCES products(id),
    quantity INTEGER,
    unit_price NUMERIC(10, 2)
);

-- Audit log — intentionally NO indexes except PK (agent should detect this)
CREATE TABLE audit_log (
    id SERIAL PRIMARY KEY,
    table_name VARCHAR(100),
    action VARCHAR(20),
    record_id INTEGER,
    changed_by VARCHAR(100),
    changed_at TIMESTAMP DEFAULT NOW(),
    old_values JSONB,
    new_values JSONB
);

-- =============================================================================
-- Seed Data — enough rows to make slow queries visible
-- =============================================================================

-- Insert 10,000 customers
INSERT INTO customers (email, full_name, country, created_at, status)
SELECT
    'user_' || i || '@example.com',
    'Customer ' || i,
    (ARRAY['US', 'UK', 'DE', 'FR', 'JP', 'BR', 'IN', 'AU', 'CA', 'AZ'])[1 + (i % 10)],
    NOW() - (random() * interval '730 days'),
    (ARRAY['active', 'active', 'active', 'inactive', 'suspended'])[1 + (i % 5)]
FROM generate_series(1, 10000) AS i;

-- Insert 500 products
INSERT INTO products (name, category, price, stock_quantity)
SELECT
    'Product ' || i,
    (ARRAY['Electronics', 'Books', 'Clothing', 'Food', 'Sports', 'Home', 'Toys'])[1 + (i % 7)],
    (random() * 500 + 5)::NUMERIC(10,2),
    (random() * 1000)::INTEGER
FROM generate_series(1, 500) AS i;

-- Insert 100,000 orders
INSERT INTO orders (customer_id, order_date, total_amount, status, shipping_country)
SELECT
    1 + (random() * 9999)::INTEGER,
    NOW() - (random() * interval '365 days'),
    (random() * 1000 + 10)::NUMERIC(12,2),
    (ARRAY['pending', 'shipped', 'delivered', 'cancelled', 'returned'])[1 + (i % 5)],
    (ARRAY['US', 'UK', 'DE', 'FR', 'JP', 'BR', 'IN', 'AU', 'CA', 'AZ'])[1 + (i % 10)]
FROM generate_series(1, 100000) AS i;

-- Insert 250,000 order items
INSERT INTO order_items (order_id, product_id, quantity, unit_price)
SELECT
    1 + (random() * 99999)::INTEGER,
    1 + (random() * 499)::INTEGER,
    1 + (random() * 5)::INTEGER,
    (random() * 200 + 5)::NUMERIC(10,2)
FROM generate_series(1, 250000) AS i;

-- Insert 500,000 audit log entries (large, unindexed table)
INSERT INTO audit_log (table_name, action, record_id, changed_by, changed_at, old_values, new_values)
SELECT
    (ARRAY['orders', 'customers', 'products', 'order_items'])[1 + (i % 4)],
    (ARRAY['INSERT', 'UPDATE', 'DELETE'])[1 + (i % 3)],
    1 + (random() * 100000)::INTEGER,
    'user_' || (1 + (random() * 100)::INTEGER),
    NOW() - (random() * interval '365 days'),
    ('{"status": "old_value_' || i || '"}')::JSONB,
    ('{"status": "new_value_' || i || '"}')::JSONB
FROM generate_series(1, 500000) AS i;

-- =============================================================================
-- Intentionally create SOME indexes (but leave gaps for agent to find)
-- =============================================================================

CREATE INDEX idx_orders_customer_id ON orders(customer_id);
CREATE INDEX idx_order_items_order_id ON order_items(order_id);
-- NOTE: No index on orders(status), orders(order_date), orders(shipping_country)
-- NOTE: No index on audit_log(table_name), audit_log(changed_at), audit_log(record_id)
-- NOTE: No index on customers(country), customers(status)
-- The agent should detect these missing indexes!

-- Update statistics
ANALYZE;

-- =============================================================================
-- Generate intentional bloat (Phase 2 — for bloat detection testing)
-- =============================================================================

-- Delete ~30% of audit_log rows to create dead tuples
DELETE FROM audit_log WHERE id % 3 = 0;

-- Update many orders rows to create dead tuples in orders table
UPDATE orders SET notes = 'updated_' || id WHERE id % 4 = 0;

-- Delete some order_items to create bloat there too
DELETE FROM order_items WHERE id % 5 = 0;

-- NOTE: We intentionally do NOT run VACUUM here.
-- This leaves dead tuples for the agent's bloat detection to find.
-- Autovacuum will eventually clean some of this, but the agent
-- should detect the bloat before that happens.

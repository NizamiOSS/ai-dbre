# Troubleshooting

Common issues encountered during setup and how to fix them.

---

## 1. Port conflict — `Bind for 0.0.0.0:5433 failed: port is already allocated`

**Symptom:**

```
Error response from daemon: failed to set up container networking:
Bind for 0.0.0.0:5433 failed: port is already allocated
```

**Cause:** Another PostgreSQL instance (local install, Patroni standby, or another container) is already using port 5433.

**Fix:** Change the host port in three places:

1. `docker-compose.yml` — update ports mapping:
   ```yaml
   ports:
     - "5434:5432"
   ```

2. `agent/config.py` — update default:
   ```python
   pg_port: int = 5434
   ```

3. `.env` — update the variable:
   ```
   PG_PORT=5434
   ```

Then restart:
```bash
docker compose down
docker compose up -d
```

---

## 2. `pg_stat_statements_reset()` does not exist during init

**Symptom:**

```
psql:/docker-entrypoint-initdb.d/01_init.sql:15: ERROR:
function pg_stat_statements_reset() does not exist
```

Container exits immediately after this error.

**Cause:** The `pg_stat_statements` extension is loaded via `shared_preload_libraries` at server start, but the init script runs during bootstrap before the function is fully registered. Also, a read-only agent doesn't need reset privileges anyway.

**Fix:** Remove the GRANT line from `pg_config/init.sql`:

```sql
-- Delete this line:
GRANT EXECUTE ON FUNCTION pg_stat_statements_reset() TO dbre_agent;
```

Then rebuild with a fresh volume (PG won't re-run init on existing data):

```bash
docker compose down -v
docker compose up -d
```

---

## 3. Pydantic `extra_forbidden` validation error

**Symptom:**

```
pydantic_core._pydantic_core.ValidationError: 2 validation errors for Settings
pg_admin_user
  Extra inputs are not permitted
pg_admin_password
  Extra inputs are not permitted
```

**Cause:** The `.env` file contains `PG_ADMIN_USER` and `PG_ADMIN_PASSWORD` (used by the workload generator), but the `Settings` class in `agent/config.py` doesn't define those fields. Pydantic-settings rejects unrecognized variables by default.

**Fix:** Add `"extra": "ignore"` to the model config in `agent/config.py`:

```python
model_config = {"env_file": ".env", "env_file_encoding": "utf-8", "extra": "ignore"}
```

This tells Pydantic to silently skip any `.env` variables that don't have a matching field.

---

## 4. Anthropic API returns `401 authentication_error`

**Symptom:**

```
Error code: 401 - {'type': 'error', 'error': {'type': 'authentication_error',
'message': 'invalid x-api-key'}}
```

**Cause:** The API key in `.env` is invalid — expired, revoked, copied with extra characters, or wrapped in quotes.

**Diagnosis steps:**

1. Check the key is loading at all:
   ```bash
   python -c "from agent.config import settings; print(f'Key: [{settings.anthropic_api_key[:10]}...]')"
   ```

2. Check for hidden characters:
   ```bash
   python -c "from agent.config import settings; print(repr(settings.anthropic_api_key))"
   ```

3. Test the key directly:
   ```bash
   curl https://api.anthropic.com/v1/messages \
     -H "x-api-key: $(grep ANTHROPIC_API_KEY .env | cut -d= -f2)" \
     -H "anthropic-version: 2023-06-01" \
     -H "content-type: application/json" \
     -d '{"model":"claude-sonnet-4-6","max_tokens":10,"messages":[{"role":"user","content":"hi"}]}'
   ```

**Fix:** Generate a fresh key at [console.anthropic.com/settings/keys](https://console.anthropic.com/settings/keys) and update `.env`. Make sure the format has no quotes:

```
# Correct
ANTHROPIC_API_KEY=sk-ant-api03-xxxxx...

# Wrong — quotes become part of the value
ANTHROPIC_API_KEY="sk-ant-api03-xxxxx..."
```

---

## 5. Init script doesn't re-run after fixing SQL errors

**Symptom:** You fixed an error in `pg_config/init.sql` but the container still fails or the schema is missing.

**Cause:** PostgreSQL only runs `/docker-entrypoint-initdb.d/` scripts when the data directory is empty (first boot). If the container already initialized once (even partially), it skips the scripts.

**Fix:** Destroy the volume and start fresh:

```bash
docker compose down -v    # -v removes the pgdata volume
docker compose up -d
```

---

## 6. `ModuleNotFoundError` when running scripts

**Symptom:**

```
ModuleNotFoundError: No module named 'langgraph'
```

**Cause:** Dependencies not installed, or wrong Python environment active.

**Fix:**

```bash
# Make sure you're in the venv
source .venv/bin/activate    # macOS/Linux
.venv\Scripts\activate       # Windows

# Install dependencies
pip install -r requirements.txt
```

If using Conda, make sure you're not in the `base` environment when you expect the venv:

```bash
conda deactivate
source .venv/bin/activate
```

---

## 7. Remediation tool returns `permission denied` (Phase 3)

**Symptom:**

```json
{"status": "ERROR", "error": "permission denied for table orders"}
```

**Cause:** The `dbre_remediation` database user doesn't exist or doesn't have the right privileges. This happens when you upgraded to Phase 3 without rebuilding the database volume.

**Fix:** Rebuild with the updated init.sql that creates the `dbre_remediation` user:

```bash
docker compose down -v
docker compose up -d
```

If you can't destroy the volume, create the user manually:

```bash
psql -h localhost -p 5434 -U dbre_admin -d dbre_testdb -c "
CREATE USER dbre_remediation WITH PASSWORD 'remediation_write';
GRANT CONNECT ON DATABASE dbre_testdb TO dbre_remediation;
GRANT USAGE ON SCHEMA public TO dbre_remediation;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO dbre_remediation;
GRANT pg_read_all_stats TO dbre_remediation;
GRANT CREATE ON SCHEMA public TO dbre_remediation;
"
```

---

## 8. Agent doesn't propose remediation, only recommends

**Symptom:** You ask the agent to "fix" or "create an index" but it only gives you
a recommendation without calling `execute_remediation`.

**Cause:** The agent's system prompt guides its behavior. If you only ask "what's wrong?"
it diagnoses. You need to explicitly ask it to take action.

**Fix:** Use action-oriented phrasing:

```
# These trigger remediation:
"Find slow queries on audit_log and fix them"
"Create missing indexes for the worst-performing queries"
"Vacuum the audit_log table"

# These only diagnose:
"What are the slowest queries?"
"What indexes should I create?"
```

---

## 9. Remediation blocked — statement doesn't match whitelist

**Symptom:**

```json
{"status": "BLOCKED", "error": "Statement does not match any allowed pattern."}
```

**Cause:** The agent proposed SQL that doesn't match the safety whitelist. Only
`CREATE INDEX`, `ANALYZE`, `VACUUM`, and `REINDEX CONCURRENTLY` are allowed.

**This is working as intended.** The whitelist is restrictive by design. If you need
to allow additional operations, add patterns to `ALLOWED_PATTERNS` in
`tools/remediation.py`.

# GTD Backend Alerting Rules

This document defines alerting rules for monitoring the GTD backend in production. These rules should be configured in your alert management system (Grafana, Prometheus, PagerDuty, etc.).

## Overview

The GTD backend emits structured JSON logs suitable for alerting. All rules use LogQL queries to identify critical conditions that require operator attention.

## Alert Rules

### 1. High Error Rate Alert
**Severity:** CRITICAL
**Description:** Error rate exceeds 0.1 errors/second for 5+ minutes
**Action:** Investigate logs immediately; check external service status

**LogQL Query:**
```logql
rate({job="gtd-backend"} | json | level="ERROR" | __error__="" [1m]) > 0.1
```

**Grafana Alert:**
```
for: 5m
threshold: 0.1
comparison: greater than
```

**Example Scenarios:**
- Database continuously locked
- RTM API service down
- LLM API service down
- IMAP connection issues

---

### 2. RTM Authentication Failed
**Severity:** HIGH
**Description:** RTM authentication token invalid or expired
**Action:** User must re-authenticate via `/rtm/auth/start`

**LogQL Query:**
```logql
{job="gtd-backend"} | json | component="rtm" | error_type="auth_failed"
```

**Annotation:**
```
User must re-authenticate with RTM. Visit: http://gtd-backend/approvals
Click "Re-authenticate with RTM" button and follow instructions.
```

**Detection Method:**
1. Monitor for `error_type="auth_failed"` in RTM commit logs
2. Check `commit_status="auth_failed"` in database
3. Verify `RtmAuth.valid="invalid"` in database

---

### 3. Database Locked (High Contention)
**Severity:** WARNING
**Description:** SQLite database locked for >30 seconds, retries exhausted
**Action:** Check for concurrent writes; reduce polling intervals if needed

**LogQL Query:**
```logql
{job="gtd-backend"} | json | error_type="db_locked" | level="ERROR"
```

**Escalation:**
- 1st occurrence: Log and monitor
- 2+ occurrences in 1 hour: Page operator
- Persistent: May need to switch to PostgreSQL

**Mitigation Steps:**
1. Reduce polling intervals (EMAIL_POLL_INTERVAL, CLARIFY_POLL_INTERVAL, COMMIT_POLL_INTERVAL)
2. Increase DB_LOCK_TIMEOUT (default 30 seconds)
3. Ensure only one GTD backend instance is running
4. Check for other processes accessing the database file

---

### 4. Clarification Permanent Failure
**Severity:** MEDIUM
**Description:** Clarification failed after 5 retries; manual review required
**Action:** Review capture in UI; manually clarify or fix input

**LogQL Query:**
```logql
{job="gtd-backend"} | json | component="clarification" | error_type="permanently_failed"
```

**Manual Resolution:**
1. Go to `/approvals` in web UI
2. Find capture with status "Clarification failed"
3. Manually edit clarification fields
4. Save and approve

**Common Causes:**
- Email with invalid UTF-8 encoding
- Email too large for LLM to process
- LLM service consistently unavailable
- Network issues during all 5 attempts

---

### 5. Commit Unknown State (Requires Manual Check)
**Severity:** HIGH
**Description:** RTM commit timed out; cannot determine if task was created
**Action:** Manually verify in RTM; inspect capture state

**LogQL Query:**
```logql
{job="gtd-backend"} | json | component="rtm_commit" | commit_status="unknown"
```

**Important:** Unknown state captures are NOT automatically retried (to prevent duplicates).

**Manual Resolution:**
1. Go to `/audit-log` and find the capture with `commit_status="unknown"`
2. Check RTM to see if task exists
   - If task exists in RTM: Manually update database `commit_status='committed'`
   - If task doesn't exist: Update `commit_status='pending'` to retry
3. Or delete the capture and re-send the email

**Database Update (if needed):**
```sql
UPDATE captures SET commit_status='committed'
WHERE id=<capture_id> AND commit_status='unknown';
```

---

### 6. RTM API Errors (Rate Limited/Server Error)
**Severity:** MEDIUM
**Description:** RTM API returning 429 (rate limit) or 500-503 (server error)
**Action:** Monitor; automatic retry should handle; escalate if persistent

**LogQL Query:**
```logql
{job="gtd-backend"} | json | external_service="rtm" | (http_status="429" or http_status=~"50[0-3]")
```

**Automatic Handling:**
- Retries up to 3 times with exponential backoff
- Circuit breaker opens after 5 consecutive failures
- Captures in failed state retried on next poll

**If Circuit Breaker Opens:**
```logql
{job="gtd-backend"} | json | error_type="circuit_breaker" | level="ERROR"
```
Wait 60 seconds for recovery, or restart service.

---

### 7. LLM API Errors
**Severity:** MEDIUM
**Description:** OpenRouter API errors (rate limit, server error, timeout)
**Action:** Automatic retry; manual action if persistent

**LogQL Query:**
```logql
{job="gtd-backend"} | json | external_service="llm" | level="ERROR"
```

**Specific Cases:**

**Rate Limited (429):**
```logql
{job="gtd-backend"} | json | external_service="llm" | http_status="429"
```
System will backoff and retry. If persistent, check API quota/pricing.

**Timeout:**
```logql
{job="gtd-backend"} | json | external_service="llm" | error_type="timeout"
```
Temporary network issue. Should recover on next poll.

---

### 8. Email Ingestion Issues
**Severity:** MEDIUM
**Description:** IMAP connection failures or email processing errors
**Action:** Check Gmail IMAP is enabled; verify credentials

**LogQL Query:**
```logql
{job="gtd-backend"} | json | component="email" | level="ERROR"
```

**Common Issues & Solutions:**

**IMAP Connection Timeout:**
```logql
{job="gtd-backend"} | json | component="email" | error_type="timeout"
```
- Increase IMAP_TIMEOUT
- Check network connectivity to Gmail IMAP server

**Authentication Failed:**
```logql
{job="gtd-backend"} | json | component="email" | error_type="auth_failed"
```
- Verify IMAP_USERNAME and IMAP_PASSWORD
- For Gmail: May need app-specific password (not main password)
- Enable "Less secure app access" or use OAuth

**Duplicate Email Processed:**
```logql
{job="gtd-backend"} | json | component="email" | error_type="duplicate"
```
- Normal behavior; email was already processed
- Check if gtdinput→gtdprocessed label move succeeded in Gmail

---

## Alert Configuration Examples

### Prometheus AlertManager
```yaml
groups:
  - name: gtd_backend
    rules:
      - alert: GTDHighErrorRate
        expr: rate({job="gtd-backend"} | json | level="ERROR" [1m]) > 0.1
        for: 5m
        annotations:
          summary: "GTD Backend high error rate"
          description: "Error rate > 0.1/sec for 5+ minutes"

      - alert: GTDRTMAuthFailed
        expr: count({job="gtd-backend"} | json | component="rtm" | error_type="auth_failed") > 0
        for: 1m
        annotations:
          summary: "RTM authentication failed"
          description: "User must re-authenticate with RTM"

      - alert: GTDClarificationPermanentlyFailed
        expr: count({job="gtd-backend"} | json | error_type="permanently_failed" | component="clarification") > 0
        for: 5m
        annotations:
          summary: "Email clarification permanently failed"
          description: "Manual review required in /approvals"

      - alert: GTDCommitUnknownState
        expr: count({job="gtd-backend"} | json | commit_status="unknown") > 0
        for: 5m
        annotations:
          summary: "RTM commit unknown state"
          description: "Manual verification needed in /audit-log"
```

### Grafana Loki Alerts
1. Go to Explore → Loki
2. Create alert rule with LogQL query
3. Set threshold and duration
4. Configure notification channel

---

## Dashboard Queries for Monitoring

### System Health Dashboard

**Error Rate (per minute):**
```logql
rate({job="gtd-backend"} | json | level="ERROR" [1m])
```

**Pending Work:**
```logql
sum by (status) (rate({job="gtd-backend"} | json | status=~"(pending|failed)" [5m]))
```

**Failure Types (last 24h):**
```logql
topk(5, rate({job="gtd-backend"} | json | level="ERROR" | __error__="" [24h]))
```

**Component Health:**
```logql
{job="gtd-backend"} | json | component=~"(rtm|email|clarification)" | level=~"(ERROR|WARNING)"
```

---

## Escalation Policy

1. **0-5 minutes:** Monitor logs, check component status
2. **5-15 minutes:** Page on-call operator if unresolved
3. **15+ minutes:** Page secondary on-call, prepare incident post-mortem

## Post-Incident Checklist

After resolving a critical alert:
1. ✅ Document root cause
2. ✅ Implement permanent fix (if applicable)
3. ✅ Update this alerting guide if alert rule needs tuning
4. ✅ Add test case to prevent recurrence
5. ✅ Update runbooks with discovered solutions

---

## Dashboard Setup

### For Teams Using Grafana

1. Import the Grafana dashboard: `docs/grafana-dashboard.json`
2. Configure Loki datasource to use structured logs
3. Set notification channels (Slack, PagerDuty, email)
4. Test alerts with:
   ```bash
   # Simulate error by stopping clarification service
   # Monitor for alert firing within 5 minutes
   ```

### For Teams Using Prometheus + AlertManager

1. Configure Prometheus to scrape `/metrics` endpoint
2. Load AlertManager rules from configuration examples above
3. Configure routing to appropriate channels
4. Test with alert testing tools

---

## Tuning Alerts

### Too Many False Positives?
- Increase threshold (e.g., 0.2 errors/sec instead of 0.1)
- Increase duration (e.g., 10 minutes instead of 5)
- Add additional conditions (e.g., exclude known maintenance windows)

### Missing Real Issues?
- Decrease threshold (e.g., 0.05 errors/sec)
- Decrease duration (e.g., 2 minutes)
- Add more specific error types

### Consulting Logs
Always check structured logs for context:
```logql
{job="gtd-backend"} | json | error_type="<error_type>" | __error__=""
```

Examine the full error message to understand severity and needed action.

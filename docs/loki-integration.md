# Loki Integration Guide

This guide explains how to configure Promtail to ingest structured JSON logs from the GTD backend and make them searchable in Loki/Grafana.

## Overview

The GTD backend emits structured JSON logs with the following fields:
- `timestamp`: ISO 8601 timestamp (UTC)
- `level`: Log level (DEBUG, INFO, WARNING, ERROR, CRITICAL)
- `logger`: Logger name (e.g., "app.main", "app.clarification")
- `message`: Log message text
- `component`: System component (e.g., "ui", "email", "rtm", "llm")
- `operation`: Operation being performed (e.g., "create_capture", "commit_task")
- `capture_id`: ID of associated capture (if applicable)
- `error_type`: Type of error (e.g., "json_decode_error", "http_timeout", "db_locked")
- `external_service`: External service name (e.g., "rtm", "openrouter", "gmail_imap")
- `http_status`: HTTP status code (if applicable)
- `retry_count`: Number of retry attempts
- `attempt`: Current attempt number
- `exception`: Stack trace (if exc_info=True)

## Configuration

### Environment Variables

Set these in `.env` or `docker-compose.yml`:

```bash
# Logging format: "json" for production (Loki), "text" for local development
LOG_FORMAT=json

# Log level: DEBUG, INFO, WARNING, ERROR, CRITICAL
LOG_LEVEL=INFO
```

### Docker Compose Setup

Ensure the GTD backend container writes logs to stdout:

```yaml
services:
  gtd-backend:
    build: .
    environment:
      LOG_FORMAT: json
      LOG_LEVEL: INFO
    # Logs automatically sent to stdout (Docker standard)
```

## Promtail Configuration

Add the following to your `promtail-config.yaml`:

```yaml
scrape_configs:
  - job_name: gtd-backend
    # Scrape Docker container logs
    docker_sd_configs:
      - host: unix:///var/run/docker.sock
    relabel_configs:
      # Only scrape gtd-backend container
      - source_labels: [__meta_docker_container_name]
        regex: gtd-backend
        action: keep
      # Use container name as job label
      - source_labels: [__meta_docker_container_name]
        target_label: container
      - target_label: job
        replacement: gtd-backend

    # Parse JSON logs and extract structured fields
    pipeline_stages:
      - json:
          expressions:
            timestamp: timestamp
            level: level
            logger: logger
            message: message
            component: component
            operation: operation
            capture_id: capture_id
            error_type: error_type
            external_service: external_service
            http_status: http_status
            retry_count: retry_count
            attempt: attempt
            exception: exception

      # Optional: add container label to logs
      - labels:
          container:
          level:
          component:
          error_type:
          external_service:

      # Optional: parse timestamp to Promtail's internal format
      - timestamp:
          format: "2006-01-02T15:04:05Z"
          source: timestamp
```

## Loki Configuration

In your `loki-config.yaml`, ensure you have appropriate retention and indexing:

```yaml
schema_config:
  configs:
    - from: 2020-10-24
      store: boltdb-shipper
      object_store: filesystem
      schema: v11
      index:
        prefix: index_
        period: 24h

# Retention policy: keep logs for 7 days
limits_config:
  retention_period: 168h
  enforce_metric_name: false

# Enable chunk cache for performance
cache_config:
  enable_fifocache: true
```

## LogQL Queries

Common queries for monitoring GTD backend:

### All errors in last 1 hour
```logql
{job="gtd-backend"} | json | level="ERROR" | __error__="" [1h]
```

### RTM API errors
```logql
{job="gtd-backend"} | json | external_service="rtm" | level="ERROR"
```

### LLM/OpenRouter clarification failures
```logql
{job="gtd-backend"} | json | external_service="llm" | error_type!=""
```

### Database lock contentions
```logql
{job="gtd-backend"} | json | error_type="db_locked"
```

### HTTP retries by service
```logql
{job="gtd-backend"} | json | retry_count > 0 | group_without(timestamp, message, exception)
```

### Failed clarifications (permanent failures)
```logql
{job="gtd-backend"} | json | component="clarification" | error_type="permanently_failed"
```

### Captures by status (metric query)
```logql
rate({job="gtd-backend"} | json | component="capture" [5m])
```

## Grafana Dashboard Setup

### Add Loki Data Source

1. In Grafana, go to Configuration → Data Sources
2. Click "Add data source"
3. Select "Loki"
4. Set URL to `http://loki:3100` (or appropriate Loki URL)
5. Save & Test

### Sample Dashboard Panels

See `grafana-dashboard.json` for a complete sample dashboard with:
- Error rate gauge
- Failed operations table
- Component status breakdown
- Retry distribution
- Pending work counts

## Alerting Rules

See `loki-alerts.md` for Loki alerting rules that can be used to notify operators of critical failures.

## Troubleshooting

### Logs not appearing in Loki

1. Check that Promtail is scraping the container:
   ```bash
   docker logs promtail | grep gtd-backend
   ```

2. Verify JSON format is valid in container logs:
   ```bash
   docker logs gtd-backend | head -1 | python -m json.tool
   ```

3. Check Promtail configuration for parsing errors:
   ```bash
   docker logs promtail | grep "json" | head -10
   ```

### Query returns no results

1. Verify the time range includes logs
2. Check filter labels match your log data:
   ```logql
   {job="gtd-backend"} | json
   ```
3. Use `| __error__=""` to exclude parse errors from results

### Performance issues

- Reduce label cardinality (avoid using high-cardinality fields like `capture_id` as labels)
- Use appropriate retention periods (default 7 days recommended)
- Enable chunk cache in Loki for frequently accessed data

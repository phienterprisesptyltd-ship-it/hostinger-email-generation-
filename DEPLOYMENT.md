# Deployment Guide

## Development Environment

### Using Docker Compose (Recommended)

```bash
# Set environment variables
export HOSTINGER_API_TOKEN=your_token
export HOSTINGER_MAILBOX_RESOURCE_ID=your_resource_id
export HOSTINGER_SENDER_ADDRESS=info@synchrobuild.com.au
export ANTHROPIC_API_KEY=your_anthropic_key

# Start services
docker-compose up

# In another terminal, initialize database
docker-compose exec app python setup.py

# Visit http://localhost:8000
```

### Manual Setup

```bash
# Install PostgreSQL 12+
# (macOS): brew install postgresql
# (Ubuntu): apt-get install postgresql

# Start PostgreSQL
psql postgres

# Create database
CREATE DATABASE synchrobuild;
CREATE USER synchrobuild WITH PASSWORD 'development_password';
GRANT ALL PRIVILEGES ON DATABASE synchrobuild TO synchrobuild;
```

Then follow the Quick Start guide in QUICKSTART.md.

## Production Deployment

### Architecture

```
Internet Traffic
    ↓
Load Balancer (HTTPS)
    ↓
Application Instances (FastAPI/Uvicorn)
    ↓
PostgreSQL Database (Managed Service)
    ↓
Hostinger Email API (External)
Anthropic Claude API (External)
```

### Prerequisites

- PostgreSQL 12+ (managed service recommended)
- Python 3.9+ runtime
- Hostinger API credentials
- Anthropic API key
- HTTPS certificate
- Load balancer (optional but recommended)

### Option 1: Docker on AWS ECS

#### Build and Push Image

```bash
# Build image
docker build -t synchrobuild-leads:latest .

# Tag for ECR
aws ecr get-login-password | docker login --username AWS --password-stdin <account>.dkr.ecr.us-east-1.amazonaws.com
docker tag synchrobuild-leads:latest <account>.dkr.ecr.us-east-1.amazonaws.com/synchrobuild-leads:latest
docker push <account>.dkr.ecr.us-east-1.amazonaws.com/synchrobuild-leads:latest
```

#### Create ECS Task Definition

```json
{
  "family": "synchrobuild-leads",
  "networkMode": "awsvpc",
  "requiresCompatibilities": ["FARGATE"],
  "cpu": "512",
  "memory": "1024",
  "containerDefinitions": [
    {
      "name": "synchrobuild-leads",
      "image": "<account>.dkr.ecr.us-east-1.amazonaws.com/synchrobuild-leads:latest",
      "portMappings": [
        {
          "containerPort": 8000,
          "hostPort": 8000
        }
      ],
      "environment": [
        {
          "name": "ENVIRONMENT",
          "value": "production"
        },
        {
          "name": "DEBUG",
          "value": "false"
        }
      ],
      "secrets": [
        {
          "name": "DATABASE_URL",
          "valueFrom": "arn:aws:secretsmanager:us-east-1:<account>:secret:synchrobuild-db-url"
        },
        {
          "name": "HOSTINGER_API_TOKEN",
          "valueFrom": "arn:aws:secretsmanager:us-east-1:<account>:secret:hostinger-token"
        },
        {
          "name": "ANTHROPIC_API_KEY",
          "valueFrom": "arn:aws:secretsmanager:us-east-1:<account>:secret:anthropic-key"
        }
      ],
      "logConfiguration": {
        "logDriver": "awslogs",
        "options": {
          "awslogs-group": "/ecs/synchrobuild-leads",
          "awslogs-region": "us-east-1",
          "awslogs-stream-prefix": "ecs"
        }
      }
    }
  ]
}
```

### Option 2: Heroku

```bash
# Login to Heroku
heroku login

# Create app
heroku create synchrobuild-leads

# Add PostgreSQL addon
heroku addons:create heroku-postgresql:standard-0 -a synchrobuild-leads

# Set environment variables
heroku config:set ENVIRONMENT=production -a synchrobuild-leads
heroku config:set DEBUG=false -a synchrobuild-leads
heroku config:set HOSTINGER_API_TOKEN=<token> -a synchrobuild-leads
heroku config:set HOSTINGER_MAILBOX_RESOURCE_ID=<id> -a synchrobuild-leads
heroku config:set HOSTINGER_SENDER_ADDRESS=<email> -a synchrobuild-leads
heroku config:set ANTHROPIC_API_KEY=<key> -a synchrobuild-leads
heroku config:set BUSINESS_TIMEZONE=Australia/Sydney -a synchrobuild-leads

# Deploy
git push heroku main

# Run migrations
heroku run python setup.py -a synchrobuild-leads

# View logs
heroku logs --tail -a synchrobuild-leads
```

### Option 3: Traditional Linux Server

```bash
# On your Linux server (Ubuntu 20.04+)

# Install dependencies
sudo apt update
sudo apt install -y python3.11 python3.11-venv postgresql postgresql-contrib nginx

# Clone repository
git clone https://github.com/phienterprisesptyltd-ship-it/hostinger-email-generation-.git
cd hostinger-email-generation-

# Create virtual environment
python3.11 -m venv venv
source venv/bin/activate

# Install Python dependencies
pip install -r requirements.txt

# Create .env file
cat > .env << EOF
DATABASE_URL=postgresql://synchrobuild:$(openssl rand -base64 32)@localhost:5432/synchrobuild
HOSTINGER_API_TOKEN=<paste-token>
HOSTINGER_MAILBOX_RESOURCE_ID=<paste-id>
HOSTINGER_SENDER_ADDRESS=info@synchrobuild.com.au
ANTHROPIC_API_KEY=<paste-key>
ENVIRONMENT=production
DEBUG=false
AUTONOMOUS_MODE=true
BUSINESS_TIMEZONE=Australia/Sydney
EOF

# Setup database
sudo -u postgres createdb synchrobuild
sudo -u postgres createuser synchrobuild
# Set password interactively when prompted

# Initialize
python setup.py

# Create systemd service
sudo tee /etc/systemd/system/synchrobuild-leads.service > /dev/null << EOF
[Unit]
Description=Synchrobuild Lead Follow-up
After=network.target postgresql.service

[Service]
Type=notify
User=www-data
WorkingDirectory=/home/ubuntu/hostinger-email-generation-
Environment="PATH=/home/ubuntu/hostinger-email-generation-/venv/bin"
ExecStart=/home/ubuntu/hostinger-email-generation-/venv/bin/python -m uvicorn main:app --host 127.0.0.1 --port 8000
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

# Enable and start service
sudo systemctl enable synchrobuild-leads.service
sudo systemctl start synchrobuild-leads.service

# Check status
sudo systemctl status synchrobuild-leads.service
```

### Nginx Configuration

```nginx
upstream synchrobuild {
    server 127.0.0.1:8000;
}

server {
    listen 80;
    server_name api.synchrobuild.com;
    
    # Redirect HTTP to HTTPS
    return 301 https://$server_name$request_uri;
}

server {
    listen 443 ssl http2;
    server_name api.synchrobuild.com;
    
    # SSL certificates (use Let's Encrypt)
    ssl_certificate /etc/letsencrypt/live/api.synchrobuild.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/api.synchrobuild.com/privkey.pem;
    
    # Security headers
    add_header Strict-Transport-Security "max-age=31536000; includeSubDomains" always;
    add_header X-Content-Type-Options "nosniff" always;
    add_header X-Frame-Options "DENY" always;
    add_header X-XSS-Protection "1; mode=block" always;
    
    # Logging
    access_log /var/log/nginx/synchrobuild_access.log;
    error_log /var/log/nginx/synchrobuild_error.log;
    
    # Proxy
    location / {
        proxy_pass http://synchrobuild;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_buffering off;
    }
    
    # Health check (not logged)
    location /health {
        access_log off;
        proxy_pass http://synchrobuild;
    }
}
```

## Database Management

### PostgreSQL Backups

```bash
# Daily backup
0 2 * * * pg_dump synchrobuild > /backups/synchrobuild-$(date +\%Y\%m\%d).sql

# Upload to S3
aws s3 cp /backups/synchrobuild-*.sql s3://backups-bucket/synchrobuild/
```

### Database Monitoring

```bash
# Connect to database
psql postgresql://synchrobuild:password@db-host:5432/synchrobuild

# Check table sizes
SELECT 
    schemaname,
    tablename,
    pg_size_pretty(pg_total_relation_size(schemaname||'.'||tablename)) 
FROM pg_tables 
WHERE schemaname = 'public'
ORDER BY pg_total_relation_size(schemaname||'.'||tablename) DESC;

# Check index usage
SELECT schemaname, tablename, indexname, idx_scan 
FROM pg_stat_user_indexes 
ORDER BY idx_scan DESC;
```

## Monitoring

### Application Monitoring

```bash
# View logs
docker logs -f <container-id>

# Health check
curl https://api.synchrobuild.com/health

# Metrics
curl https://api.synchrobuild.com/

# Dashboard
curl https://api.synchrobuild.com/api/admin/dashboard
```

### Error Tracking (Sentry)

```bash
pip install sentry-sdk

# In main.py, add:
import sentry_sdk
sentry_sdk.init(
    dsn="your-sentry-dsn",
    environment="production",
    traces_sample_rate=0.1
)
```

### Performance Monitoring

```python
# In main.py, add timing middleware
from fastapi import Request
import time

@app.middleware("http")
async def add_process_time_header(request: Request, call_next):
    start_time = time.time()
    response = await call_next(request)
    process_time = time.time() - start_time
    response.headers["X-Process-Time"] = str(process_time)
    return response
```

## Production Checklist

### Before Launch

- [ ] PostgreSQL database created and backed up
- [ ] Environment variables set securely
- [ ] HTTPS certificate installed
- [ ] Hostinger API token verified
- [ ] Anthropic API key working
- [ ] Load testing completed (100+ concurrent users)
- [ ] Database performance tested
- [ ] Backup strategy implemented
- [ ] Monitoring configured
- [ ] Error tracking enabled
- [ ] Logging configured
- [ ] Rate limiting configured
- [ ] CORS origins restricted

### During Launch

- [ ] Start with `AUTONOMOUS_MODE=false`
- [ ] Monitor logs closely
- [ ] Test webhook receiving
- [ ] Verify email sending
- [ ] Check admin dashboard
- [ ] Monitor database connections

### After Launch

- [ ] Keep checking logs for errors
- [ ] Monitor API response times
- [ ] Track AI decision quality
- [ ] Monitor email delivery rates
- [ ] Review escalation reasons
- [ ] Track follow-up success rate
- [ ] Plan capacity increases if needed
- [ ] Set up automated backups
- [ ] Schedule regular security updates

## Scaling

### Horizontal Scaling

```bash
# Run multiple application instances behind load balancer
for i in {1..3}; do
    docker run -d \
        --name synchrobuild-leads-$i \
        -e DATABASE_URL=postgresql://... \
        synchrobuild-leads:latest
done

# Use Nginx as load balancer (see configuration above)
```

### Database Scaling

```bash
# Read replicas (PostgreSQL)
# Create standby server for read operations
# Route read-only queries to replica

# Connection pooling
# Use PgBouncer for connection pooling
# Reduce connection overhead

# Partitioning
# Partition messages table by date
# Improves query performance for large datasets
```

## Troubleshooting Production Issues

### High Memory Usage

```bash
# Check process memory
ps aux | grep uvicorn

# Restart if needed
systemctl restart synchrobuild-leads

# Increase container memory limit
```

### Database Connection Errors

```bash
# Check connection pool
SELECT count(*) FROM pg_stat_activity;

# Kill idle connections
SELECT pg_terminate_backend(pid) 
FROM pg_stat_activity 
WHERE datname = 'synchrobuild' AND state = 'idle' 
AND state_change < now() - interval '5 minutes';
```

### Scheduler Not Running

```bash
# Check scheduler logs
docker logs <container-id> | grep scheduler

# Verify timezone setting
echo "BUSINESS_TIMEZONE=$BUSINESS_TIMEZONE"

# Restart application
systemctl restart synchrobuild-leads
```

### Emails Not Sending

```bash
# Check Hostinger API token
curl -H "Authorization: Bearer $HOSTINGER_API_TOKEN" \
     https://api.mail.hostinger.com/api/v1/me

# Check mailbox resource ID
curl -H "Authorization: Bearer $HOSTINGER_API_TOKEN" \
     https://api.mail.hostinger.com/api/v1/mailboxes/$HOSTINGER_MAILBOX_RESOURCE_ID

# Review application logs
docker logs <container-id> | grep -i "email"
```

## Security Considerations

- **Secrets Management**: Use cloud provider's secrets manager
- **Database**: Enable SSL/TLS connections
- **API**: Use rate limiting on webhook endpoint
- **Backups**: Encrypt backups at rest
- **Logs**: Don't log sensitive information
- **Updates**: Keep Python and dependencies updated
- **Access**: Restrict admin API to internal network only
- **Tokens**: Rotate API tokens quarterly

## Support

For deployment issues:
1. Check application logs
2. Verify all environment variables
3. Test external API connections
4. Monitor database performance
5. Check firewall/security groups

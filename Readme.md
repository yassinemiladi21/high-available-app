# High Availability Web Application - Complete Documentation

##This project was created by: Azza Mechken, Moataz Khabbouchi & Yassine Miladi 

## 📋 Table of Contents

1. [Project Overview](#project-overview)
2. [Architecture](#architecture)
3. [Components](#components)
4. [Networking Configuration](#networking-configuration)
5. [Database Replication](#database-replication)
6. [Load Balancing](#load-balancing)
7. [Shared Storage (NFS)](#shared-storage-nfs)
8. [Application Logic](#application-logic)
9. [Deployment Guide](#deployment-guide)
10. [Testing & Monitoring](#testing--monitoring)
11. [Troubleshooting](#troubleshooting)

---

## 🎯 Project Overview

This project implements a **high-availability web application** without using containers or cloud services. The system uses only virtual machines (VirtualBox) to create a fault-tolerant architecture that can handle component failures gracefully.

### Key Features

- ✅ **Zero Single Point of Failure** (except load balancer and NFS)
- ✅ **Automatic Database Failover** (Primary → Replica)
- ✅ **Load Balanced Application Servers**
- ✅ **Shared Image Storage** via NFS
- ✅ **PostgreSQL Streaming Replication**
- ✅ **Health Monitoring & Auto-Recovery**

### Technology Stack

- **Operating System**: Ubuntu 24.04 LTS
- **Web Framework**: Flask (Python)
- **Database**: PostgreSQL 16
- **Load Balancer**: HAProxy
- **Shared Storage**: NFS (Network File System)
- **Hypervisor**: VirtualBox

---

## 🏗️ Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                         Internet                            │
└──────────────────────────┬──────────────────────────────────┘
                           │
                  ┌────────▼────────┐
                  │  Load Balancer  │
                  │  192.168.104.10 │
                  │    (HAProxy)    │
                  │   NFS Server    │
                  └────────┬────────┘
                           │
              ┌────────────┴────────────┐
              │                         │
     ┌────────▼────────┐       ┌───────▼─────────┐
     │   App Server 1  │       │  App Server 2   │
     │ 192.168.104.21  │       │ 192.168.104.22  │
     │    (Flask)      │       │    (Flask)      │
     └────────┬────────┘       └────────┬────────┘
              │                         │
              └──────────┬──────────────┘
                         │
                    NFS Mount
                 /mnt/shared/images
                         │
              ┌──────────┴──────────┐
              │                     │
     ┌────────▼────────┐   ┌───────▼─────────┐
     │  DB Primary     │   │   DB Replica    │
     │ 192.168.104.31  │◄──┤ 192.168.104.32  │
     │  PostgreSQL 16  │   │  PostgreSQL 16  │
     │    (R/W)        │   │   (Read-only)   │
     └─────────────────┘   └─────────────────┘
           Primary              Streaming
                            ◄─ Replication ─►
```

### Component Roles

| VM Name | IP Address | Role | Services |
|---------|------------|------|----------|
| **Load Balancer** | 192.168.104.10 | Reverse Proxy + NFS Server | HAProxy, NFS |
| **App VM 1** | 192.168.104.21 | Application Server | Flask, NFS Client |
| **App VM 2** | 192.168.104.22 | Application Server | Flask, NFS Client |
| **DB Primary** | 192.168.104.31 | Database Master | PostgreSQL 16 (R/W) |
| **DB Replica** | 192.168.104.32 | Database Standby | PostgreSQL 16 (R/O) |

---

## 🔧 Components

### 1. Load Balancer (HAProxy)

**Purpose**: Distributes incoming HTTP traffic across multiple application servers.

**Configuration** (`/etc/haproxy/haproxy.cfg`):

```haproxy
frontend http_front
    bind *:80
    default_backend app_servers

backend app_servers
    balance roundrobin                    # Distribution algorithm
    option httpchk GET /api/health        # Health check endpoint
    http-check expect status 200          # Expected response
    server app1 192.168.104.21:5000 check inter 3000 rise 2 fall 3
    server app2 192.168.104.22:5000 check inter 3000 rise 2 fall 3

listen stats
    bind *:8080
    stats enable
    stats uri /stats
    stats refresh 30s
```

**Key Parameters**:
- `balance roundrobin`: Distributes requests evenly
- `check inter 3000`: Health check every 3 seconds
- `rise 2`: Server considered UP after 2 successful checks
- `fall 3`: Server considered DOWN after 3 failed checks

**Access**:
- Application: `http://192.168.104.10`
- Statistics: `http://192.168.104.10:8080/stats`

---

### 2. Application Servers (Flask)

**Purpose**: Serve the web application with automatic database failover.

**Key Features**:

#### Database Connection Logic

```python
def get_db(readonly=False):
    """
    Smart database connection with failover:
    - For reads: Connect to any available DB (primary or replica)
    - For writes: Ensure connection to writable node (not in recovery)
    - Auto-rotates through DB_CONFIGS on failure
    """
    global current_db_index
    
    nodes_count = len(DB_CONFIGS)
    start_index = current_db_index
    
    for attempt in range(nodes_count):
        idx = (start_index + attempt) % nodes_count
        try:
            conn = _connect_to_node(idx)
            
            # Check if writable when needed
            if not readonly:
                if is_read_only(conn):
                    conn.close()
                    continue  # Skip read-only nodes
            
            current_db_index = idx
            return conn
        except Exception:
            continue
    
    raise Exception("All database connections failed")
```

**How It Works**:

1. **Read Operations** (GET `/api/content`):
   - Tries current DB (primary or replica)
   - Accepts any node (readonly=True)
   - Faster because replicas can handle reads

2. **Write Operations** (POST `/api/content`, DELETE):
   - Requires writable node (readonly=False)
   - Checks `pg_is_in_recovery()` to verify not a replica
   - Automatically fails over to primary if connected to replica

3. **Connection Retry**:
   - Cycles through all configured nodes
   - Updates `current_db_index` to successful node
   - Logs each attempt for monitoring

#### File Upload Handling

```python
@app.route('/api/content', methods=['POST'])
def add_content():
    # 1. Validate file upload
    if 'image' not in request.files:
        return jsonify({'error': 'No image'}), 400
    
    # 2. Save to NFS shared storage
    unique_filename = f"{uuid.uuid4()}.{ext}"
    filepath = os.path.join(UPLOAD_FOLDER, unique_filename)
    file.save(filepath)
    
    # 3. Insert to writable database
    conn = get_db(readonly=False)
    cur.execute('INSERT INTO content (quote, image_filename) VALUES (%s, %s)',
                (quote, unique_filename))
    
    # 4. Return success with image URL
    return jsonify({'id': new_id, 'image_url': f"/images/{unique_filename}"})
```

**Image Storage Flow**:
```
User Upload → Flask App → /mnt/shared/images (NFS) → Accessible by both App VMs
                       ↓
                   Database Entry (filename only)
```

---

### 3. Database Servers (PostgreSQL 16)

#### Primary Database (192.168.104.31)

**Configuration** (`/etc/postgresql/16/main/postgresql.conf`):

```ini
listen_addresses = '*'              # Listen on all interfaces
port = 5432
wal_level = replica                 # Enable WAL for replication
max_wal_senders = 3                 # Max concurrent replica connections
wal_keep_size = 64                  # Keep 64MB of WAL files
```

**Access Control** (`/etc/postgresql/16/main/pg_hba.conf`):

```
# Local connections
local   all             postgres                                peer
local   all             all                                     peer

# Remote app connections
host    all             all             192.168.104.21/32       md5
host    all             all             192.168.104.22/32       md5
host    all             all             192.168.104.0/24        md5

# Replication from replica
host    replication     replicator      192.168.104.32/32       md5
```

**Database Schema**:

```sql
CREATE TABLE content (
    id SERIAL PRIMARY KEY,
    quote TEXT NOT NULL,
    image_filename TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

#### Replica Database (192.168.104.32)

**Setup Process**:

```bash
# 1. Stop PostgreSQL
sudo systemctl stop postgresql

# 2. Clone data from primary
sudo -u postgres pg_basebackup \
    -h 192.168.104.31 \
    -U replicator \
    -D /var/lib/postgresql/16/main \
    -P -R --wal-method=stream

# 3. Start replica
sudo systemctl start postgresql
```

**Replication Mechanism**:

```
Primary (192.168.104.31)
    ↓ Writes to WAL (Write-Ahead Log)
    ↓ Streaming via TCP
Replica (192.168.104.32)
    ↓ Applies WAL entries
    ↓ Data synchronized
```

**Key File**: `/var/lib/postgresql/16/main/standby.signal`
- Presence indicates replica mode
- Automatically created by `pg_basebackup -R`

**Verification Commands**:

```bash
# On Primary: Check replication status
sudo -u postgres psql -c "SELECT * FROM pg_stat_replication;"

# On Replica: Confirm in recovery mode
sudo -u postgres psql -c "SELECT pg_is_in_recovery();"  # Should return 't'
```

---

## 🌐 Networking Configuration

### VirtualBox Network Setup

Each VM has **two network adapters**:

1. **Adapter 1: NAT**
   - Provides internet access
   - Interface: `enp0s17` (DHCP)

2. **Adapter 2: Host-Only Network**
   - VM-to-VM communication
   - Interface: `enp0s8` (Static IP)
   - Network: `192.168.104.0/24`

### Static IP Configuration

**Netplan Configuration** (`/etc/netplan/01-netcfg.yaml`):

```yaml
network:
  version: 2
  ethernets:
    lo:
      addresses:
        - 127.0.0.1/8
    enp0s8:                          # Host-only adapter
      addresses:
        - 192.168.104.XX/24          # Static IP
      dhcp4: no
    enp0s17:                         # NAT adapter
      dhcp4: yes                     # Internet access
```

**Apply Configuration**:

```bash
sudo netplan apply
ip addr show enp0s8  # Verify IP
```

### IP Address Assignment

| VM | Host-Only IP | Purpose |
|----|--------------|---------|
| Load Balancer | 192.168.104.10 | Entry point, HAProxy, NFS |
| App VM 1 | 192.168.104.21 | Flask application |
| App VM 2 | 192.168.104.22 | Flask application |
| DB Primary | 192.168.104.31 | PostgreSQL master |
| DB Replica | 192.168.104.32 | PostgreSQL standby |


## 💾 Database Replication

### How PostgreSQL Streaming Replication Works

```
┌─────────────────────────────────────────────────────────┐
│                   Primary Server                        │
│  1. Client writes data                                  │
│  2. PostgreSQL writes to WAL (Write-Ahead Log)          │
│  3. WAL sender process streams changes to replica       │
└────────────────────┬────────────────────────────────────┘
                     │ Streaming (TCP)
                     ▼
┌─────────────────────────────────────────────────────────┐
│                   Replica Server                        │
│  1. WAL receiver process receives changes               │
│  2. Applies changes to data files                       │
│  3. Data synchronized in near real-time                 │
└─────────────────────────────────────────────────────────┘
```

### Replication Types

**Asynchronous Replication** (Used in this project):
- Primary doesn't wait for replica confirmation
- Faster writes on primary
- Small chance of data loss if primary fails before replica receives WAL

**Synchronous Replication** (Alternative):
- Primary waits for replica confirmation
- Slower writes but zero data loss
- Enable by setting `synchronous_standby_names` in postgresql.conf




### Failover Process

**Scenario**: Primary database fails

1. **Detection**: App detects connection failure to 192.168.104.31
2. **Failover**: App automatically connects to 192.168.104.32
3. **Read-Only Mode**: Replica can serve reads but not writes
4. **Promotion** (Manual): Promote replica to primary


**Update App Configuration**:

```python
# Switch primary and replica in DB_CONFIGS
DB_CONFIGS = [
    {'host': '192.168.104.32', ...},  # Now primary
    {'host': '192.168.104.31', ...},  # Now replica (after re-sync)
]
```

---

## ⚖️ Load Balancing

### HAProxy Algorithm: Round Robin

**How It Works**:

```
Request 1 → App VM 1
Request 2 → App VM 2
Request 3 → App VM 1
Request 4 → App VM 2
...
```

**Advantages**:
- Simple and fair distribution
- No server overload
- Predictable behavior

**Alternative Algorithms**:

```haproxy
balance leastconn     # Send to server with fewest connections
balance source        # Same client always goes to same server (sticky)
balance uri           # Route based on URL path
```

### Health Checks

**Configuration**:

```haproxy
option httpchk GET /api/health
http-check expect status 200
server app1 192.168.104.21:5000 check inter 3000 rise 2 fall 3
```

**Health Check Endpoint** (`/api/health`):

```python
@app.route('/api/health')
def health_check():
    try:
        conn = get_db(readonly=True)
        cur.execute('SELECT COUNT(*) FROM content')
        count = cur.fetchone()[0]
        return jsonify({
            'status': 'healthy',
            'hostname': hostname,
            'database': current_db_index + 1,
            'content_count': count
        })
    except:
        return jsonify({'status': 'degraded'}), 500
```

**Health Check States**:

```
Healthy Server (Green)
    ↓ 3 consecutive failures
Down Server (Red) → Removed from pool
    ↓ 2 consecutive successes
Healthy Server (Green) → Added back to pool
```


---

## 📂 Shared Storage (NFS)

### Why NFS?

**Problem**: Two app servers need to access the same uploaded images.

**Solution**: Network File System (NFS) provides shared storage.

```
┌──────────────┐       ┌──────────────┐
│  App VM 1    │       │  App VM 2    │
│   /mnt/      │       │   /mnt/      │
│   shared/    │       │   shared/    │
│   images/    │       │   images/    │
└──────┬───────┘       └──────┬───────┘
       │                      │
       └──────────┬───────────┘
                  │ NFS Mount
         ┌────────▼────────┐
         │  Load Balancer  │
         │  /srv/nfs/      │
         │  images/        │
         │  (NFS Server)   │
         └─────────────────┘
```

### NFS Server Setup (Load Balancer)

**1. Install NFS Server**:

```bash
sudo apt update
sudo apt install nfs-kernel-server -y
```

**2. Create Shared Directory**:

```bash
sudo mkdir -p /srv/nfs/images
sudo chown nobody:nogroup /srv/nfs/images
sudo chmod 777 /srv/nfs/images
```

**3. Configure Exports** (`/etc/exports`):

```
/srv/nfs/images 192.168.104.21(rw,sync,no_subtree_check,no_root_squash)
/srv/nfs/images 192.168.104.22(rw,sync,no_subtree_check,no_root_squash)
```

**Export Options Explained**:
- `rw`: Read-write access
- `sync`: Write changes immediately (safer but slower)
- `no_subtree_check`: Improves reliability
- `no_root_squash`: Allow root on client to write as root

**4. Apply and Start**:

```bash
sudo exportfs -ra                   # Apply exports
sudo systemctl restart nfs-kernel-server
sudo systemctl enable nfs-kernel-server
showmount -e localhost              # Verify
```

### NFS Client Setup (App VMs)

**1. Install NFS Client**:

```bash
sudo apt install nfs-common -y
```

**2. Create Mount Point**:

```bash
sudo mkdir -p /mnt/shared/images
```

**3. Mount NFS Share**:

```bash
sudo mount 192.168.104.10:/srv/nfs/images /mnt/shared/images
```

**4. Verify Mount**:

```bash
df -h | grep images
mount | grep images

# Test write access
sudo touch /mnt/shared/images/test.txt
ls -la /mnt/shared/images/
```

**5. Make Mount Persistent** (`/etc/fstab`):

```bash
echo "192.168.104.10:/srv/nfs/images /mnt/shared/images nfs defaults 0 0" | sudo tee -a /etc/fstab

# Test auto-mount
sudo umount /mnt/shared/images
sudo mount -a
df -h | grep images
```

---

## 🐍 Application Logic

### Application Structure

```
welcome_app/
├── app.py                 # Main Flask application
├── templates/
│   ├── welcome.html       # Main page (displays quotes)
│   └── admin.html         # Admin page (upload content)
```

### Key Application Functions

#### 1. Database Connection with Failover

```python
def get_db(readonly=False):
    """
    Intelligent DB connection:
    - Tries current DB first
    - On failure, rotates through all configured DBs
    - For writes, ensures connected to writable node
    - Returns psycopg2 connection object
    """
```

**Usage**:

```python
# For reads (can use replica)
conn = get_db(readonly=True)

# For writes (must be primary)
conn = get_db(readonly=False)
```

#### 2. Read-Only Check

```python
def is_read_only(conn):
    """
    Checks if connected DB is in recovery (replica).
    Returns True if read-only, False if writable.
    """
    cur = conn.cursor()
    cur.execute("SELECT pg_is_in_recovery();")
    return bool(cur.fetchone()[0])
```

#### 3. File Upload with Validation

```python
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'webp'}
MAX_FILE_SIZE = 5 * 1024 * 1024  # 5MB

def allowed_file(filename):
    return '.' in filename and \
           filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS
```

#### 4. Atomic Operations

**Adding Content** (ensures file + DB are in sync):

```python
def add_content():
    # 1. Save file first
    file.save(filepath)
    
    try:
        # 2. Insert to database
        conn = get_db(readonly=False)
        cur.execute('INSERT INTO content ...')
        conn.commit()
    except Exception:
        # 3. If DB fails, delete orphaned file
        if os.path.exists(filepath):
            os.remove(filepath)
        raise
```

### Application Endpoints

| Method | Endpoint | Purpose | DB Access |
|--------|----------|---------|-----------|
| GET | `/` | Main page | None |
| GET | `/admin` | Admin page | None |
| GET | `/images/<filename>` | Serve image | None (filesystem) |
| GET | `/api/content` | Get all content | Read-only |
| POST | `/api/content` | Add new content | Write |
| DELETE | `/api/content/<id>` | Delete content | Write |
| GET | `/api/time` | Get current time | None |
| GET | `/api/health` | Health check | Read-only |

### Configuration Management

```python
# Multiple database configs for failover
DB_CONFIGS = [
    {'host': '192.168.104.31', ...},  # Primary (preferred)
    {'host': '192.168.104.32', ...},  # Replica (fallback)
]

# Runtime state
current_db_index = 0                  # Tracks active DB

# Timeouts and retry logic
CONNECT_TIMEOUT = 3                   # seconds
FAILOVER_RETRY_DELAY = 0.5           # seconds
MAX_FAILOVER_ATTEMPTS = 3
```

### Logging

**Structured Logging**:

```python
def log(msg):
    print(f"[{datetime.utcnow().isoformat()}] {msg}")

# Usage
log("✓ Connected to DB1 (192.168.104.31)")
log("✗ Cannot connect to DB2: Connection refused")
```

**View Logs**:

```bash
# If running as systemd service
sudo journalctl -u welcome_app -f

# If running directly
python3 app.py
```

---

## 🚀 Deployment Guide

### Prerequisites

- 5 Ubuntu 24.04 VMs in VirtualBox
- All VMs connected to same Host-Only network
- Static IPs configured
- Internet access via NAT

### Step-by-Step Deployment

#### 1. Setup Load Balancer

```bash
# Install HAProxy
sudo apt update
sudo apt install haproxy -y

# Configure HAProxy
sudo nano /etc/haproxy/haproxy.cfg
# (paste configuration from Load Balancing section)

# Restart HAProxy
sudo systemctl restart haproxy
sudo systemctl enable haproxy

# Verify
curl http://localhost:8080/stats
```

#### 2. Setup NFS Server (on Load Balancer)

```bash
# Install NFS
sudo apt install nfs-kernel-server -y

# Create and configure directory
sudo mkdir -p /srv/nfs/images
sudo chown nobody:nogroup /srv/nfs/images
sudo chmod 777 /srv/nfs/images

# Configure exports
sudo tee /etc/exports << 'EOF'
/srv/nfs/images 192.168.104.21(rw,sync,no_subtree_check,no_root_squash)
/srv/nfs/images 192.168.104.22(rw,sync,no_subtree_check,no_root_squash)
EOF

# Start NFS
sudo exportfs -ra
sudo systemctl restart nfs-kernel-server
sudo systemctl enable nfs-kernel-server
```

#### 3. Setup Database Primary

```bash
# Install PostgreSQL
sudo apt install postgresql postgresql-contrib -y

# Configure
sudo nano /etc/postgresql/16/main/postgresql.conf
# Set: listen_addresses = '*'
#      wal_level = replica
#      max_wal_senders = 3
#      wal_keep_size = 64

sudo nano /etc/postgresql/16/main/pg_hba.conf
# Add connection rules (see Database section)

# Restart
sudo systemctl restart postgresql

# Create database and user
sudo -u postgres psql << 'EOF'
CREATE USER replicator WITH REPLICATION ENCRYPTED PASSWORD 'reppass123';
CREATE DATABASE welcome_app;
\c welcome_app
CREATE TABLE content (
    id SERIAL PRIMARY KEY,
    quote TEXT NOT NULL,
    image_filename TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
EOF
```

#### 4. Setup Database Replica

```bash
# Install PostgreSQL
sudo apt install postgresql postgresql-contrib -y
sudo systemctl stop postgresql

# Clone from primary
sudo rm -rf /var/lib/postgresql/16/main/*
sudo -u postgres pg_basebackup \
    -h 192.168.104.31 \
    -U replicator \
    -D /var/lib/postgresql/16/main \
    -P -R --wal-method=stream

# Start replica
sudo systemctl start postgresql

# Verify
sudo -u postgres psql -c "SELECT pg_is_in_recovery();"  # Should be 't'
```

#### 5. Setup Application Servers (Both VMs)

```bash
# Install dependencies
sudo apt update
sudo apt install python3 python3-pip python3-venv nfs-common -y

# Mount NFS
sudo mkdir -p /mnt/shared/images
sudo mount 192.168.104.10:/srv/nfs/images /mnt/shared/images
echo "192.168.104.10:/srv/nfs/images /mnt/shared/images nfs defaults 0 0" | sudo tee -a /etc/fstab

# Create systemd service
sudo nano /etc/systemd/system/welcome_app.service
```

**Service File**:

```ini
[Unit]
Description=Flask Quotes App
After=network.target

[Service]
User=root
WorkingDirectory=/home/user/welcome_app
Environment="PATH=/home/user/welcome_app"
ExecStart=/usr/bin/python3 /home/user/welcome_app/app.py
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
```

**Start Service**:

```bash
sudo systemctl daemon-reload
sudo systemctl start welcome_app
sudo systemctl enable welcome_app
sudo systemctl status welcome_app
```

---

## 🧪 Testing & Monitoring

### Functional Testing

#### Test 1: Image Upload

```bash
# 1. Access admin page
Open: http://192.168.104.10/admin

# 2. Upload image + quote
- Select an image (< 5MB)
- Enter a quote
- Click "Add Content"

# 3. Verify on main page
Open: http://192.168.104.10
# Image should appear

# 4. Check file exists on both app VMs
ssh user@192.168.104.21 "ls /mnt/shared/images/"
ssh user@192.168.104.22 "ls /mnt/shared/images/"
# Both should show same files
```

#### Test 2: Load Balancing

```bash
# Watch HAProxy distribute requests
watch -n 1 'curl -s http://192.168.104.10/api/health | jq .hostname'

# Output alternates:
# "app-01"
# "app-02"
# "app-01"
# "app-02"
```

#### Test 3: Database Replication

```bash
# On DB Primary
sudo -u postgres psql -d welcome_app << 'EOF'
INSERT INTO content (quote, image_filename) 
VALUES ('Test replication', 'test.jpg');
SELECT COUNT(*) FROM content;
EOF

# On DB Replica (wait 1 second)
sudo -u postgres psql -d welcome_app -c "SELECT COUNT(*) FROM content;"
# Count should match
```

#### Test 4: Database Failover

```bash
# 1. Stop primary database
ssh user@192.168.104.31 "sudo systemctl stop postgresql"

# 2. Check app logs (should show failover)
sudo journalctl -u welcome_app -f
# Output: "✗ Cannot connect to DB1"
#         "✓ Failover successful to DB2"

# 3. App should still serve reads
curl http://192.168.104.10/api/content

# 4. Writes will fail (replica is read-only)
# Upload new content → Error

# 5. Restore primary
ssh user@192.168.104.31 "sudo systemctl start postgresql"
```

---



## 📊 Architecture Decisions

### Why This Architecture?

| Component | Choice | Reason |
|-----------|--------|--------|
| **Load Balancer** | HAProxy | Lightweight, reliable, industry standard |
| **App Servers** | Flask | Simple, Python-based, easy to modify |
| **Database** | PostgreSQL | Excellent replication, ACID compliance |
| **Replication** | Streaming | Near real-time, built-in PostgreSQL feature |
| **Storage** | NFS | Simple shared storage, works across VMs |
| **OS** | Ubuntu 24.04 | Stable, well-documented, LTS support |

### Trade-offs

#### Advantages ✅

- **Simple Setup**: No complex orchestration tools
- **Cost Effective**: Runs on local VMs, no cloud costs
- **Educational**: Teaches fundamental HA concepts
- **Transparent**: Full control over every component
- **Portable**: Can move to bare metal or cloud

#### Limitations ⚠️

- **Single Points of Failure**:
  - Load Balancer (can be mitigated with Keepalived/VRRP)
  - NFS Server (can use distributed storage like GlusterFS)
    
- **Scalability**: Limited by physical resources
  
- **No Auto-Scaling**: Must manually add/remove app servers

### Production Improvements

To make this production-ready:

1. **Add Load Balancer Redundancy**:
   ```
   Keepalived + VRRP → Floating IP between 2 HAProxy nodes
   ```

2. **Automate DB Failover**:
   ```
   Patroni + etcd → Automatic primary election
   ```

3. **Replace NFS with Distributed Storage**:
   ```
   GlusterFS or Ceph → Redundant storage
   ```

4. **Add Monitoring**:
   ```
   Prometheus + Grafana → Metrics and alerting
   ```

5. **Implement Backups**:
   ```
   pg_dump + cron → Scheduled database backups
   rsync → NFS content backups
   ```

6. **SSL/TLS**:
   ```
   Let's Encrypt + HAProxy → HTTPS encryption
   ```

---

## 📚 References

### Documentation

- [PostgreSQL Replication](https://www.postgresql.org/docs/16/high-availability.html)
- [HAProxy Configuration](http://www.haproxy.org/download/2.8/doc/configuration.txt)
- [NFS Setup Guide](https://ubuntu.com/server/docs/service-nfs)
- [Flask Documentation](https://flask.palletsprojects.com/)
- [VirtualBox Networking](https://www.virtualbox.org/manual/ch06.html)

### Useful Commands Reference

```bash
# PostgreSQL
sudo -u postgres psql                                    # Connect to PostgreSQL
pg_lsclusters                                            # List clusters
sudo systemctl restart postgresql                        # Restart PostgreSQL

# HAProxy
sudo systemctl restart haproxy                           # Restart HAProxy
echo "show stat" | socat stdio /var/run/haproxy/admin.sock  # Stats via socket

# NFS
showmount -e [server]                                    # Show exports
sudo exportfs -ra                                        # Reload exports
mount | grep nfs                                         # Show NFS mounts

# Networking
ip addr show                                             # Show IP addresses
ss -tlnp | grep [port]                                   # Show listening ports
ping -c 3 [ip]                                           # Test connectivity

# Systemd
sudo systemctl status [service]                          # Check service status
sudo journalctl -u [service] -f                          # Follow service logs
sudo systemctl restart [service]                         # Restart service
```

---

## 🎓 Learning Outcomes

After completing this project, you understand:

1. **High Availability Principles**
   - Redundancy and fault tolerance
   - Failover mechanisms
   - Health checking

2. **Database Replication**
   - Streaming replication
   - Primary-replica architecture
   - Read/write splitting

3. **Load Balancing**
   - Distribution algorithms
   - Health checks
   - Session persistence

4. **Shared Storage**
   - NFS architecture
   - Network file systems
   - Concurrent access handling

5. **System Administration**
   - VirtualBox networking
   - Linux networking (netplan)
   - Systemd services
   - Log analysis

---

## 🚀 Next Steps

### Enhancements

1. **Add HTTPS Support**
2. **Implement Automated DB Failover** (Patroni)
3. **Add Load Balancer Redundancy** (Keepalived)
4. **Set Up Monitoring** (Prometheus + Grafana)
5. **Implement Backup Strategy**
6. **Add Caching Layer** (Redis)
7. **Deploy Distributed Storage** (GlusterFS)

### Migration to Production

1. Replace VirtualBox VMs with physical servers or cloud VMs
2. Use proper DNS instead of IP addresses
3. Implement SSL certificates
4. Set up automated monitoring and alerting
5. Create disaster recovery procedures
6. Document runbooks for common scenarios

---

## 📝 License

This project is for educational purposes. Feel free to modify and extend it for your needs.

---

## 👥 Contributors

Created as a learning project for understanding high availability systems without containers or cloud dependencies.

---

**Last Updated**: November 2025  
**Version**: 1.0  
**Status**: Production-Ready Architecture (Educational Use)

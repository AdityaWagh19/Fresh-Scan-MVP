# Fresh-Scan MVP: Maintenance & Setup Scripts
![Type](https://img.shields.io/badge/type-devops-blue) ![Language](https://img.shields.io/badge/python-3.12-green) ![Status](https://img.shields.io/badge/status-active-success)

This directory contains the essential DevOps and database migration utilities for the Fresh-Scan MVP ecosystem. These scripts are critical for initializing the environment, managing security credentials, and ensuring database performance.

---

## Script Catalog

### 1. `setup_auth.py` (Security Bootstrap)
**Role:** Primary Security Initialization  
**Criticality:** High (Required for first run)

**Functionality:**
*   **Cryptographic Key Gen**: Uses `secrets` module to generate a cryptographically strong 64-byte `JWT_SECRET_KEY`.
*   **Environment Configuration**: Safely writes to `.env`, preserving existing keys while updating auth secrets.
*   **Dependency Check**: Verifies that the required cryptography libraries are installed.

**Usage:**
```bash
python scripts/setup_auth.py
```

**Output Example:**
```text
[+] Generating strong JWT secret key...
[+] Updating .env file...
[+] Auth secret configured successfully.
```

---

### 2. `create_auth_indexes.py` (Database Optimization)
**Role:** Database Schema & Performance Tuning  
**Criticality:** High (Production Requirement)

**Functionality:**
*   **Unique Constraints**: Enforces unique emails and usernames at the database level to prevent duplicate accounts.
*   **Compound Indexes**: Optimizes queries like "Find user by email AND active status".
*   **TTL (Time-To-Live) Indexes**: Automatically purges expired sessions (`auth_sessions`) after their expiry time (default: 30 days), preventing database bloat.

**Target Collections:**
| Collection | Index Type | Purpose |
| :--- | :--- | :--- |
| `users` | Unique | Prevent duplicate registrations |
| `auth_sessions` | TTL (expiry) | Auto-delete old session tokens |
| `auth_audit_log` | Descending | Faster "Last login" queries |

**Usage:**
```bash
python scripts/create_auth_indexes.py
```

---

## Standard Deployment Workflow

For a new deployment (Development or Production), execute strictly in this order:

1.  **Configure Environment**:
    ```bash
    cp .env.auth.template .env
    # Edit .env with your Google Client IDs and MongoDB URI
    ```

2.  **Bootstrap Security**:
    ```bash
    python scripts/setup_auth.py
    ```

3.  **Optimize Database**:
    ```bash
    python scripts/create_auth_indexes.py
    ```

4.  **Verify Status**:
    Check the output for `[SUCCESS]` messages. If MongoDB is not running, Step 3 will fail.

---

## Troubleshooting

**Error: `ModuleNotFoundError: No module named 'src'`**
*   **Cause**: Running scripts from inside the `scripts/` folder instead of root.
*   **Fix**: Always run from project root:
    ```bash
    # WRONG
    cd scripts
    python setup_auth.py
    
    # RIGHT
    cd fresh-scan-mvp
    python scripts/setup_auth.py
    ```

**Error: `ServerSelectionTimeoutError`**
*   **Cause**: The script cannot connect to MongoDB.
*   **Fix**: Ensure `mongod` is running and `MONGO_URI` in `.env` is correct.

---
**Maintained by Team FreshScan**
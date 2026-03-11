"""Jira connectivity test — verifies credentials, lists projects, and creates a test issue.

Usage:
    python test_jira.py               # check env + list projects
    python test_jira.py --create      # also create a test issue (then delete it)
"""

import os
import sys

from dotenv import load_dotenv, dotenv_values

# ── 1. Load and show exactly what dotenv reads ────────────────────────────────

print("=" * 60)
print("STEP 1 — Reading .env file directly")
print("=" * 60)

raw = dotenv_values(".env")  # reads file without touching os.environ
jira_keys = {k: v for k, v in raw.items() if "JIRA" in k}
if jira_keys:
    for k, v in jira_keys.items():
        display = v[:20] + "..." if v and len(v) > 20 else v
        print(f"  {k} = {display!r}")
else:
    print("  ❌ No JIRA_* keys found in .env file")
    print("  Make sure your .env file contains:")
    print("    JIRA_BASE_URL=https://yourcompany.atlassian.net")
    print("    JIRA_EMAIL=you@company.com")
    print("    JIRA_API_TOKEN=ATATT3...")
    print("    JIRA_PROJECT_KEY=OPS")

load_dotenv(override=True)  # now load into os.environ

# Check for common formatting mistakes
base_url = os.environ.get("JIRA_BASE_URL", "")
email    = os.environ.get("JIRA_EMAIL", "")
token    = os.environ.get("JIRA_API_TOKEN", "")

issues = []
if not base_url:
    issues.append("JIRA_BASE_URL is not set")
elif base_url.startswith('"') or base_url.startswith("'"):
    issues.append(f"JIRA_BASE_URL has surrounding quotes — remove them: {base_url}")
elif " " in base_url:
    issues.append(f"JIRA_BASE_URL has spaces: {base_url!r}")

if not email:
    issues.append("JIRA_EMAIL is not set")
if not token:
    issues.append("JIRA_API_TOKEN is not set")

if issues:
    print()
    for issue in issues:
        print(f"  ❌ {issue}")
    sys.exit(1)

print(f"  JIRA_BASE_URL   = {base_url}")
print(f"  JIRA_EMAIL      = {email}")
print(f"  JIRA_API_TOKEN  = {token[:10]}... ({len(token)} chars)")
print(f"  JIRA_PROJECT_KEY= {os.environ.get('JIRA_PROJECT_KEY', 'NOT SET — will list below')}")

# ── 2. Test authentication ────────────────────────────────────────────────────

import httpx

base = base_url.rstrip("/")
auth = (email, token)
headers = {"Accept": "application/json", "Content-Type": "application/json"}

print()
print("=" * 60)
print("STEP 2 — Test authentication (GET /rest/api/3/myself)")
print("=" * 60)

try:
    r = httpx.get(f"{base}/rest/api/3/myself", auth=auth, headers=headers, timeout=10)
    if r.status_code == 200:
        me = r.json()
        print(f"  ✅ Authenticated as: {me.get('displayName')} ({me.get('emailAddress')})")
        print(f"     Account ID: {me.get('accountId')}")
    elif r.status_code == 401:
        print("  ❌ 401 Unauthorized — wrong email or API token")
        print("     Get a new token: https://id.atlassian.com/manage-profile/security/api-tokens")
        sys.exit(1)
    elif r.status_code == 403:
        print("  ❌ 403 Forbidden — token valid but no permission")
        sys.exit(1)
    else:
        print(f"  ❌ HTTP {r.status_code}: {r.text[:200]}")
        sys.exit(1)
except httpx.ConnectError:
    print(f"  ❌ Cannot connect to {base}")
    print("     Check JIRA_BASE_URL — should be https://yourcompany.atlassian.net")
    sys.exit(1)

# ── 3. List all projects + find project key ───────────────────────────────────

print()
print("=" * 60)
print("STEP 3 — List Jira projects (find your JIRA_PROJECT_KEY)")
print("=" * 60)

r = httpx.get(
    f"{base}/rest/api/3/project/search",
    auth=auth,
    headers=headers,
    params={"maxResults": 50, "orderBy": "name"},
    timeout=10,
)

if r.status_code != 200:
    print(f"  ❌ Failed to list projects: {r.status_code} {r.text[:200]}")
    sys.exit(1)

projects = r.json().get("values", [])
if not projects:
    print("  ❌ No projects found — check permissions")
    sys.exit(1)

print(f"  Found {len(projects)} project(s):\n")
print(f"  {'KEY':<12} {'NAME':<40} {'TYPE'}")
print(f"  {'-'*12} {'-'*40} {'-'*15}")
for p in projects:
    print(f"  {p['key']:<12} {p['name']:<40} {p.get('projectTypeKey', '')}")

# ── 4. Validate configured project key ────────────────────────────────────────

project_key = os.environ.get("JIRA_PROJECT_KEY", "")
if project_key:
    print()
    valid_keys = [p["key"] for p in projects]
    if project_key in valid_keys:
        print(f"  ✅ JIRA_PROJECT_KEY={project_key!r} is valid")
    else:
        print(f"  ❌ JIRA_PROJECT_KEY={project_key!r} not found in your projects")
        print(f"     Valid keys: {valid_keys}")
        sys.exit(1)
else:
    print(f"\n  ➜ Add to .env: JIRA_PROJECT_KEY=<key from list above>")
    sys.exit(0)

# ── 5. Optional: create + delete a test issue ─────────────────────────────────

if "--create" not in sys.argv:
    print()
    print("  Run with --create to also test issue creation")
    print("=" * 60)
    print("✅ Jira credentials are valid — ready to use")
    print("=" * 60)
    sys.exit(0)

print()
print("=" * 60)
print("STEP 4 — Create test issue")
print("=" * 60)

payload = {
    "fields": {
        "project": {"key": project_key},
        "summary": "[ARGOS TEST] Credential verification — safe to delete",
        "description": {
            "type": "doc", "version": 1,
            "content": [{"type": "paragraph", "content": [
                {"type": "text", "text": "Auto-created by test_jira.py — safe to delete."}
            ]}]
        },
        "issuetype": {"name": "Task"},
    }
}

r = httpx.post(f"{base}/rest/api/3/issue", auth=auth, headers=headers, json=payload, timeout=15)

if r.status_code in (200, 201):
    issue = r.json()
    issue_key = issue["key"]
    issue_url = f"{base}/browse/{issue_key}"
    print(f"  ✅ Issue created: {issue_key}")
    print(f"     URL: {issue_url}")

    # Delete it right away
    d = httpx.delete(f"{base}/rest/api/3/issue/{issue_key}", auth=auth, headers=headers, timeout=10)
    if d.status_code == 204:
        print(f"  ✅ Test issue {issue_key} deleted — cleanup done")
    else:
        print(f"  ℹ️  Could not delete {issue_key} (HTTP {d.status_code}) — delete it manually")
else:
    print(f"  ❌ Failed to create issue: HTTP {r.status_code}")
    print(f"     {r.text[:500]}")
    sys.exit(1)

print()
print("=" * 60)
print("✅ ALL JIRA CHECKS PASSED")
print("=" * 60)

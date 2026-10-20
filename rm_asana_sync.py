#!/usr/bin/env python3
"""
Rent Manager -> Asana follow-up sync.

- Pulls open follow-ups assigned to YOUR Rent Manager user.
- Creates an Asana task for each new one (due date = follow-up date).
- Keeps existing tasks in sync: due-date / description changes, and marks the
  Asana task complete when the follow-up is completed (or disappears) in RM.

Stateless: each Asana task carries a marker "[RM-FU:<id>]" in its notes, so the
script can run from a fresh machine every time (ideal for scheduled runs).

Config comes from environment variables (see README.md). Run:
    python rm_asana_sync.py --discover   # find the follow-up endpoint/fields in your RM
    python rm_asana_sync.py --dry-run    # show what would change
    python rm_asana_sync.py              # do it
"""
import argparse, json, os, re, sys
from datetime import date, datetime
import urllib.request, urllib.parse, urllib.error

# ---------------------------------------------------------------- config
def env(name, default=None, required=False):
    v = os.environ.get(name, default)
    if required and not v:
        sys.exit(f"Missing required environment variable: {name}")
    return v

RM_CORP      = env("RM_CORP_ID")             # e.g. "guardeum" -> https://guardeum.api.rentmanager.com
RM_USER      = env("RM_USERNAME")
RM_PASS      = env("RM_PASSWORD")
RM_LOCATION  = env("RM_LOCATION_ID", "1")
RM_MY_UID    = env("RM_MY_USER_ID")          # RM UserID whose follow-ups to sync (Martin = 1)
RM_MY_UNAME  = env("RM_MY_USERNAME", "")     # alternative: look up the UserID by username

# Endpoint + field names. Defaults match the RM12 /Tasks resource (verified against
# guardeum.api.rentmanager.com/Help/Resource/Tasks). A task is "done" when CloseDate is set;
# it's "mine" when I'm in TaskUsers (assigned).
RM_FU_PATH       = env("RM_FOLLOWUP_PATH", "/Tasks/Search")
F_ID             = env("RM_F_ID", "TaskID")
F_DUE            = env("RM_F_DUE", "DueDate")
F_DONE           = env("RM_F_DONE", "IsClosed")
F_TEXT           = env("RM_F_TEXT", "Description")  # falls back to the linked history note
F_SUBJECT        = env("RM_F_SUBJECT", "Subject")
RM_TASK_TYPE     = env("RM_TASK_TYPE", "")       # optional: only sync this TaskType number
RM_EMBEDS        = env("RM_EMBEDS", "TaskUsers,History")
RM_SINCE         = env("RM_SINCE", "")          # only follow-ups created on/after this date (YYYY-MM-DD)

ASANA_PAT     = env("ASANA_PAT")
ASANA_PROJECT = env("ASANA_PROJECT_GID")
ASANA_ASSIGNEE = env("ASANA_ASSIGNEE", "me")     # "me" or a user gid / email

MARKER_RE = re.compile(r"\[RM-FU:(\d+)\]")

# ---------------------------------------------------------------- http
def http(method, url, headers=None, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            raw = r.read().decode() or "null"
            return r.status, dict(r.headers), json.loads(raw)
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:500]
        raise RuntimeError(f"{method} {url} -> HTTP {e.code}: {detail}") from None

# ---------------------------------------------------------------- Rent Manager
class RM:
    def __init__(self):
        for k in ("RM_CORP_ID", "RM_USERNAME", "RM_PASSWORD"):
            env(k, required=True)
        self.base = f"https://{RM_CORP}.api.rentmanager.com"
        _, _, tok = http("POST", self.base + "/Authentication/AuthorizeUser", body={
            "Username": RM_USER, "Password": RM_PASS, "LocationID": int(RM_LOCATION)})
        self.h = {"X-RM12Api-ApiToken": tok if isinstance(tok, str) else str(tok),
                  "Accept": "application/json"}
        import atexit; atexit.register(self.logout)  # RM caps concurrent logins per user

    def logout(self):
        try: http("POST", self.base + "/Authentication/DeAuthorize", self.h)
        except Exception: pass

    def get_all(self, path, params=None):
        params = dict(params or {}); params["pagesize"] = 1000; page = 1; out = []
        while True:
            params["pagenumber"] = page
            url = self.base + path + "?" + urllib.parse.urlencode(params, safe=",;")
            status, hdr, data = http("GET", url, self.h)
            if isinstance(data, list): out.extend(data)
            elif data: out.append(data)
            if status != 206 or 'rel="next"' not in hdr.get("Link", ""):
                return out
            page += 1

    def my_user_id(self):
        if RM_MY_UID:
            return str(RM_MY_UID)
        users = self.get_all("/Users", {"filters": f"Username,eq,{RM_MY_UNAME or RM_USER}"})
        if not users:
            sys.exit("Couldn't auto-detect your RM UserID. Set RM_MY_USER_ID.")
        u = users[0]
        return str(u.get("UserID") or u.get("ID"))

    def my_followups(self, uid):
        params = {}
        if RM_TASK_TYPE:
            params["filters"] = f"TaskType,eq,{RM_TASK_TYPE}"
        if RM_EMBEDS:
            params["embeds"] = RM_EMBEDS
        rows = self.get_all(RM_FU_PATH, params)
        # RM can't filter Tasks by assignee server-side, so filter here.
        def assigned_ids(t):
            ids = set()
            for tu in t.get("TaskUsers") or []:
                for k in ("UserID", "AssignedUserID"):
                    if tu.get(k) is not None: ids.add(str(tu[k]))
                if isinstance(tu.get("User"), dict) and tu["User"].get("UserID") is not None:
                    ids.add(str(tu["User"]["UserID"]))
            return ids
        if RM_SINCE:
            rows = [t for t in rows if str(t.get("CreateDate") or "")[:10] >= RM_SINCE]
        return [t for t in rows if uid in assigned_ids(t)
                or (not t.get("TaskUsers") and str(t.get("CreateUserID")) == uid)]

    def discover(self):
        print(f"Authenticated to {self.base}\n")
        for p in ["/Tasks"]:
            try:
                rows = self.get_all(p, {"embeds": "TaskUsers"})[:3]
                print(f"FOUND {p}")
                if rows:
                    print("  sample fields:", ", ".join(sorted(rows[0].keys())))
            except RuntimeError as e:
                print(f"  -  {p}: {str(e).split(':',1)[-1].strip()[:80]}")
        try:
            print("\nYour user record:", self.get_all("/Users", {"filters": f"Username,eq,{RM_USER}"})[:1])
        except RuntimeError as e:
            print("\n/Users lookup failed:", e)
        print(f"\nFull reference for your instance: {self.base}/Help")

# ---------------------------------------------------------------- Asana
class Asana:
    base = "https://app.asana.com/api/1.0"
    def __init__(self):
        env("ASANA_PAT", required=True); env("ASANA_PROJECT_GID", required=True)
        self.h = {"Authorization": f"Bearer {ASANA_PAT}", "Accept": "application/json"}

    def project_tasks(self):
        tasks, offset = [], None
        while True:
            q = {"project": ASANA_PROJECT, "limit": 100,
                 "opt_fields": "name,notes,due_on,completed"}
            if offset: q["offset"] = offset
            _, _, d = http("GET", f"{self.base}/tasks?" + urllib.parse.urlencode(q), self.h)
            tasks += d["data"]
            offset = (d.get("next_page") or {}).get("offset")
            if not offset: return tasks

    def create(self, fields):
        fields = {**fields, "projects": [ASANA_PROJECT], "assignee": ASANA_ASSIGNEE}
        return http("POST", f"{self.base}/tasks", self.h, {"data": fields})[2]["data"]

    def update(self, gid, fields):
        return http("PUT", f"{self.base}/tasks/{gid}", self.h, {"data": fields})[2]["data"]

# ---------------------------------------------------------------- mapping
def as_date(v):
    if not v: return None
    s = str(v)[:10]
    try: return datetime.strptime(s, "%Y-%m-%d").date().isoformat()
    except ValueError: return None

def is_done(fu):
    v = fu.get(F_DONE)
    return bool(v) and str(v).lower() not in ("false", "0", "none")

def context_label(fu):
    # Try to name the tenant / property / unit from embedded objects, if present.
    for path in (("History", "Tenant", "Name"), ("Tenant", "Name"), ("Prospect", "Name"),
                 ("History", "Property", "Name"), ("Property", "Name"), ("Unit", "Name")):
        cur = fu
        for k in path:
            cur = cur.get(k) if isinstance(cur, dict) else None
        if cur: return str(cur)
    return None

def task_fields(fu):
    fid = fu[F_ID]
    text = (fu.get(F_TEXT) or (fu.get("History") or {}).get("Note") or "").strip()
    subj = " ".join(str(fu.get(F_SUBJECT) or "").split()) if F_SUBJECT else ""
    title = subj or (text.splitlines()[0][:90] if text else f"Rent Manager follow-up {fid}")
    ctx = context_label(fu)
    if ctx: title = f"{ctx}: {title}"
    notes = f"{text}\n\n— Synced from Rent Manager [RM-FU:{fid}]"
    return {"name": title, "notes": notes, "due_on": as_date(fu.get(F_DUE))}

# ---------------------------------------------------------------- sync
def sync(dry):
    rm, asana = RM(), Asana()
    uid = rm.my_user_id()
    fus = rm.my_followups(uid)
    open_fus = {str(f[F_ID]): f for f in fus if not is_done(f)}
    done_ids = {str(f[F_ID]) for f in fus if is_done(f)}

    existing = {}
    for t in asana.project_tasks():
        m = MARKER_RE.search(t.get("notes") or "")
        if m: existing[m.group(1)] = t

    created = updated = completed = 0
    for fid, fu in open_fus.items():
        want = task_fields(fu)
        t = existing.get(fid)
        if not t:
            print(f"CREATE  {want['name']}  (due {want['due_on']})")
            if not dry: asana.create(want)
            created += 1
            continue
        diff = {k: v for k, v in want.items() if (t.get(k) or None) != (v or None)}
        if t.get("completed"):
            diff["completed"] = False  # reopened in RM
        if diff:
            print(f"UPDATE  {t['name']}  {sorted(diff)}")
            if not dry: asana.update(t["gid"], diff)
            updated += 1

    # Close tasks whose follow-up is completed (or no longer open/assigned to you) in RM.
    for fid, t in existing.items():
        if fid not in open_fus and not t.get("completed"):
            why = "completed in RM" if fid in done_ids else "no longer open/assigned in RM"
            print(f"COMPLETE {t['name']}  ({why})")
            if not dry: asana.update(t["gid"], {"completed": True})
            completed += 1

    print(f"\n{'[dry run] ' if dry else ''}{len(open_fus)} open follow-ups | "
          f"created {created}, updated {updated}, completed {completed}")

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--discover", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    if a.discover: RM().discover()
    else: sync(a.dry_run)

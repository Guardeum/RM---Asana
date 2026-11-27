#!/usr/bin/env python3
"""
Rent Manager <-> Asana follow-up sync, for several workers.

For every worker listed in workers.json:
- Each open RM history-note follow-up assigned to them becomes a task in THEIR Asana
  project, assigned to them, due on the follow-up date.
- Follow-up closed in RM  -> Asana task completed.
- Asana task completed    -> follow-up closed in RM (logged in as that worker, because RM
  only lets a user close their own tasks through the API).

Stateless: each Asana task carries "[RM-FU:<id>]" in its notes.

Settings (environment / GitHub secrets):
  RM_CORP_ID, RM_LOCATION_ID          Rent Manager API company + location
  RM_USERNAME, RM_PASSWORD            read-only reader login (GPT) used to find follow-ups
  ASANA_PAT                           Asana token (creates projects/tasks)
  ASANA_WORKSPACE_GID, ASANA_TEAM_GID where new worker projects are created
  ALL_SECRETS                         ${{ toJSON(secrets) }} — holds each worker's
                                      RM password as RM_PW_<USERNAME>, e.g. RM_PW_MJ
  WORKERS_FILE                        default: workers.json next to this script

Run:  python rm_asana_sync.py [--dry-run] [--only <rm_username>]
"""
import argparse, json, os, re, sys, atexit
from datetime import datetime
import urllib.request, urllib.parse, urllib.error

def env(name, default=None, required=False):
    v = os.environ.get(name, default)
    if required and not v:
        sys.exit(f"Missing required setting: {name}")
    return v

RM_CORP     = env("RM_CORP_ID")
RM_LOCATION = env("RM_LOCATION_ID", "1")
ASANA_PAT   = env("ASANA_PAT")
WORKSPACE   = env("ASANA_WORKSPACE_GID", "1210702695314279")
TEAM        = env("ASANA_TEAM_GID", "1210702695314281")
TODAY       = datetime.now().strftime("%Y-%m-%d")
try:
    SECRETS = json.loads(env("ALL_SECRETS", "{}") or "{}")
except ValueError:
    SECRETS = {}

MARKER_RE = re.compile(r"\[RM-FU:(\d+)\]")

def worker_password(username):
    key = "RM_PW_" + re.sub(r"[^A-Z0-9]", "_", username.upper())
    pw = SECRETS.get(key) or os.environ.get(key)
    # backwards compatibility with the original single-user setup
    if not pw and username.lower() == (os.environ.get("RM_CLOSE_USERNAME") or "").lower():
        pw = os.environ.get("RM_CLOSE_PASSWORD")
    return key, pw

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
        env("RM_CORP_ID", required=True)
        self.base = f"https://{RM_CORP}.api.rentmanager.com"
        self.h = self.login(env("RM_USERNAME", required=True), env("RM_PASSWORD", required=True))
        self.sessions = {}          # worker username -> headers

    def login(self, user, pw):
        _, _, tok = http("POST", self.base + "/Authentication/AuthorizeUser", body={
            "Username": user, "Password": pw, "LocationID": int(RM_LOCATION)})
        h = {"X-RM12Api-ApiToken": str(tok), "Accept": "application/json"}
        atexit.register(self._logout, h)   # RM caps concurrent logins per user
        return h

    def _logout(self, h):
        try: http("POST", self.base + "/Authentication/DeAuthorize", h)
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

    def user_id(self, username):
        users = self.get_all("/Users", {"filters": f"Username,eq,{username}",
                                        "fields": "UserID,Username,Name"})
        return str(users[0]["UserID"]) if users else None

    def all_followups(self):
        return self.get_all("/Tasks/Search", {"embeds": "TaskUsers,History"})

    def close_task(self, task_id, username, uid):
        if username not in self.sessions:
            key, pw = worker_password(username)
            if not pw:
                raise RuntimeError(f"no password secret {key} for {username}")
            self.sessions[username] = self.login(username, pw)
        h = self.sessions[username]
        try:   # send back the full record with the close fields set
            _, _, task = http("GET", f"{self.base}/Tasks/{int(task_id)}", h)
            task = task[0] if isinstance(task, list) else task
        except RuntimeError:
            task = {"TaskID": int(task_id)}
        task.update({"IsClosed": True, "CloseUserID": int(uid),
                     "CloseDate": datetime.now().strftime("%Y-%m-%dT%H:%M:%S")})
        http("POST", self.base + "/Tasks", h, [task])

def assigned_ids(t):
    ids = set()
    for tu in t.get("TaskUsers") or []:
        if tu.get("UserID") is not None: ids.add(str(tu["UserID"]))
    if not ids and t.get("CreateUserID") is not None:
        ids.add(str(t["CreateUserID"]))
    return ids

def is_done(t):
    v = t.get("IsClosed")
    return bool(v) and str(v).lower() not in ("false", "0", "none")

# ---------------------------------------------------------------- Asana
class Asana:
    base = "https://app.asana.com/api/1.0"
    def __init__(self):
        env("ASANA_PAT", required=True)
        self.h = {"Authorization": f"Bearer {ASANA_PAT}", "Accept": "application/json"}
        self._projects = None

    def call(self, method, path, body=None):
        return http(method, self.base + path, self.h, body)[2]

    def team_projects(self):
        if self._projects is None:
            self._projects, offset = {}, None
            while True:
                q = {"limit": 100, "archived": "false", "opt_fields": "name"}
                if offset: q["offset"] = offset
                d = self.call("GET", f"/teams/{TEAM}/projects?" + urllib.parse.urlencode(q))
                for p in d["data"]: self._projects[p["name"]] = p["gid"]
                offset = (d.get("next_page") or {}).get("offset")
                if not offset: break
        return self._projects

    def ensure_project(self, worker, dry):
        if worker.get("asana_project_gid"):
            return worker["asana_project_gid"]
        name = worker.get("asana_project_name") or f"RM Follow-ups - {worker['name']}"
        gid = self.team_projects().get(name)
        if gid or dry:
            return gid
        print(f"  creating Asana project '{name}'")
        gid = self.call("POST", "/projects", {"data": {
            "name": name, "workspace": WORKSPACE, "team": TEAM, "privacy_setting": "private",
            "default_view": "list",
            "notes": "Auto-synced from Rent Manager follow-ups. Keep the [RM-FU:id] marker in task descriptions."}})["data"]["gid"]
        self.call("POST", f"/projects/{gid}/addMembers", {"data": {"members": worker["asana_email"]}})
        self._projects[name] = gid
        return gid

    def project_tasks(self, project):
        tasks, offset = [], None
        while True:
            q = {"project": project, "limit": 100, "opt_fields": "name,notes,due_on,completed"}
            if offset: q["offset"] = offset
            d = self.call("GET", "/tasks?" + urllib.parse.urlencode(q))
            tasks += d["data"]
            offset = (d.get("next_page") or {}).get("offset")
            if not offset: return tasks

    def create(self, project, assignee, fields):
        return self.call("POST", "/tasks", {"data": {**fields, "projects": [project], "assignee": assignee}})

    def update(self, gid, fields):
        return self.call("PUT", f"/tasks/{gid}", {"data": fields})

# ---------------------------------------------------------------- mapping
def as_date(v):
    try: return datetime.strptime(str(v)[:10], "%Y-%m-%d").date().isoformat() if v else None
    except ValueError: return None

def task_fields(fu):
    fid = fu["TaskID"]
    text = (fu.get("Description") or (fu.get("History") or {}).get("Note") or "").strip()
    subj = " ".join(str(fu.get("Subject") or "").split())
    title = subj or (text.splitlines()[0][:90] if text else f"Rent Manager follow-up {fid}")
    notes = f"{text}\n\n— Synced from Rent Manager [RM-FU:{fid}]"
    return {"name": title, "notes": notes, "due_on": as_date(fu.get("DueDate"))}

# ---------------------------------------------------------------- sync
def sync_worker(rm, asana, worker, all_fus, defaults, dry):
    uname = worker["rm_username"]
    uid = str(worker.get("rm_user_id") or rm.user_id(uname) or "")
    if not uid:
        print(f"  ! Rent Manager user '{uname}' not found — skipped"); return
    since = worker.get("since") or defaults.get("since") or TODAY
    fus = [t for t in all_fus if uid in assigned_ids(t)
           and str(t.get("CreateDate") or "")[:10] >= since]
    open_fus = {str(f["TaskID"]): f for f in fus if not is_done(f)}
    done_ids = {str(f["TaskID"]) for f in fus if is_done(f)}

    project = asana.ensure_project(worker, dry)
    existing = {}
    if project:
        for t in asana.project_tasks(project):
            m = MARKER_RE.search(t.get("notes") or "")
            if m: existing[m.group(1)] = t

    c = dict(created=0, updated=0, completed=0, closed_in_rm=0)
    for fid, fu in list(open_fus.items()):
        want = task_fields(fu)
        t = existing.get(fid)
        if not t:
            print(f"  CREATE  {want['name']}  (due {want['due_on']})")
            if not dry: asana.create(project, worker["asana_email"], want)
            c["created"] += 1
        elif t.get("completed"):
            print(f"  CLOSE IN RM  {t['name']}  (completed in Asana)")
            if not dry:
                try:
                    rm.close_task(fid, uname, uid)
                except RuntimeError as e:
                    print(f"    ! could not close RM follow-up {fid}: {e}"); continue
            c["closed_in_rm"] += 1
            del open_fus[fid]
        else:
            diff = {k: v for k, v in want.items() if (t.get(k) or None) != (v or None)}
            if diff:
                print(f"  UPDATE  {t['name']}  {sorted(diff)}")
                if not dry: asana.update(t["gid"], diff)
                c["updated"] += 1

    for fid, t in existing.items():
        if fid not in open_fus and not t.get("completed"):
            print(f"  COMPLETE {t['name']}  ({'closed in RM' if fid in done_ids else 'no longer open in RM'})")
            if not dry: asana.update(t["gid"], {"completed": True})
            c["completed"] += 1

    print(f"  {len(open_fus)} open | " + ", ".join(f"{k.replace('_', ' ')} {v}" for k, v in c.items()))

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--only", help="sync just this RM username")
    a = ap.parse_args()

    path = env("WORKERS_FILE", os.path.join(os.path.dirname(os.path.abspath(__file__)), "workers.json"))
    with open(path) as f:
        cfg = json.load(f)
    workers = [w for w in cfg["workers"] if not a.only or w["rm_username"].lower() == a.only.lower()]

    rm, asana = RM(), Asana()
    all_fus = rm.all_followups()
    failed = 0
    for w in workers:
        print(f"\n== {w['name']} ({w['rm_username']})")
        try:
            sync_worker(rm, asana, w, all_fus, cfg, a.dry_run)
        except Exception as e:      # one worker's problem never blocks the others
            failed += 1
            print(f"  ! error: {e}")
    if failed:
        sys.exit(f"\n{failed} worker(s) had errors")

if __name__ == "__main__":
    main()

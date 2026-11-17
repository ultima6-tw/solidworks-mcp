"""
Update check: is there a newer version of this server on GitHub?

There is no way to push updates to people who cloned the repository, so the
stdio server asks GitHub once at start-up (in a background thread, at most a
few seconds, never blocking) whether the branch this copy is on has commits
this copy does not have. If so, the first tool result of the session carries
a one-time notice telling the AI to point the user at CHANGELOG.md and
`git pull`.

What is sent: one unauthenticated HTTPS GET to the public GitHub API,
`/repos/<owner>/<repo>/compare/<local commit>...<branch>`, with a generic
User-Agent. Nothing about the user, the machine or the documents is sent.
Turn it off with SOLIDWORKS_MCP_UPDATE_CHECK=0.

Only git checkouts can be checked (the local commit comes from .git). A copy
with local commits that are not on GitHub gets a 404 from GitHub and is
treated as "nothing to report".
"""

import json
import logging
import os
import re
import threading
import urllib.error
import urllib.request

logger = logging.getLogger(__name__)

DEFAULT_REPO = "ultima6-tw/solidworks-mcp"
TIMEOUT_S = 4.0
USER_AGENT = "solidworks-mcp-update-check"

_state = {"notice": None, "shown": False}
_lock = threading.Lock()


def _git_dir(repo_root):
    """The git directory of a checkout: .git as a directory, or the target
    of a `gitdir: <path>` file (e.g. a separate git dir). None if neither."""
    dot = os.path.join(repo_root, ".git")
    if os.path.isdir(dot):
        return dot
    if os.path.isfile(dot):
        with open(dot, encoding="utf-8") as f:
            line = f.read().strip()
        if line.startswith("gitdir:"):
            path = line[len("gitdir:"):].strip()
            if not os.path.isabs(path):
                path = os.path.join(repo_root, path)
            return path if os.path.isdir(path) else None
    return None


def local_head(repo_root):
    """(commit sha, branch name) of the checkout, or (None, None)."""
    gd = _git_dir(repo_root)
    if not gd:
        return None, None
    try:
        with open(os.path.join(gd, "HEAD"), encoding="utf-8") as f:
            head = f.read().strip()
    except OSError:
        return None, None
    if not head.startswith("ref:"):
        return (head if re.fullmatch(r"[0-9a-f]{40}", head) else None), None   # detached
    ref = head[len("ref:"):].strip()
    branch = ref[len("refs/heads/"):] if ref.startswith("refs/heads/") else None
    try:
        with open(os.path.join(gd, *ref.split("/")), encoding="utf-8") as f:
            return f.read().strip(), branch
    except OSError:
        pass
    try:   # ref only in packed-refs
        with open(os.path.join(gd, "packed-refs"), encoding="utf-8") as f:
            for line in f:
                parts = line.strip().split(" ")
                if len(parts) == 2 and parts[1] == ref:
                    return parts[0], branch
    except OSError:
        pass
    return None, branch


def github_repo(repo_root):
    """'owner/repo' from the origin remote if it is on github.com, else the
    default. SOLIDWORKS_MCP_UPDATE_REPO overrides both."""
    env = os.environ.get("SOLIDWORKS_MCP_UPDATE_REPO")
    if env:
        return env.strip()
    gd = _git_dir(repo_root)
    if gd:
        try:
            with open(os.path.join(gd, "config"), encoding="utf-8") as f:
                cfg = f.read()
            m = re.search(r'\[remote "origin"\][^\[]*?url\s*=\s*(\S+)', cfg)
            if m:
                g = re.search(r"github\.com[:/]([^/\s]+)/([^/\s]+?)(?:\.git)?/?$", m.group(1))
                if g:
                    return f"{g.group(1)}/{g.group(2)}"
                return None   # origin is not on GitHub: nothing to compare against
        except OSError:
            pass
    return DEFAULT_REPO


def _fetch_json(url):
    req = urllib.request.Request(url, headers={
        "Accept": "application/vnd.github+json",
        "User-Agent": USER_AGENT,
    })
    with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
        return json.loads(r.read().decode("utf-8"))


def check_for_update(repo_root, fetch=_fetch_json):
    """The notice text if GitHub has commits this checkout does not, else
    None. Never raises."""
    try:
        if os.environ.get("SOLIDWORKS_MCP_UPDATE_CHECK", "1").strip() == "0":
            logger.info("Update check disabled (SOLIDWORKS_MCP_UPDATE_CHECK=0)")
            return None
        sha, branch = local_head(repo_root)
        if not sha:
            logger.info("Update check skipped: not a git checkout")
            return None
        repo = github_repo(repo_root)
        if not repo:
            logger.info("Update check skipped: origin is not on GitHub")
            return None
        branch = branch or "main"
        data = fetch(f"https://api.github.com/repos/{repo}/compare/{sha}...{branch}")
        status, new = data.get("status"), int(data.get("ahead_by") or 0)
        if status in ("ahead", "diverged") and new > 0:
            notice = (f"Update available for the SolidWorks MCP server: GitHub ({repo}, branch "
                      f"{branch}) has {new} commit(s) this copy ({sha[:7]}) does not. Tell the user; "
                      f"what changed is in CHANGELOG.md on GitHub. To update: `git pull` in "
                      f"{repo_root}, then restart the MCP server (and the desktop bridge host if "
                      f"one is used). Set SOLIDWORKS_MCP_UPDATE_CHECK=0 to stop this check.")
            logger.info(f"Update check: {new} new commit(s) on {repo}/{branch}")
            return notice
        logger.info(f"Update check: up to date ({status}, {sha[:7]} vs {repo}/{branch})")
        return None
    except urllib.error.HTTPError as e:
        # 404: GitHub does not know this commit (local commits not pushed)
        logger.info(f"Update check: nothing to report (HTTP {e.code})")
        return None
    except Exception as e:
        logger.info(f"Update check: could not reach GitHub ({e})")
        return None


def start(repo_root):
    """Run the check in a background thread."""
    def run():
        notice = check_for_update(repo_root)
        with _lock:
            _state["notice"] = notice
    threading.Thread(target=run, name="update-check", daemon=True).start()


def take_notice():
    """The notice, once per process; None until the check finished or when
    there is nothing to report."""
    with _lock:
        if _state["notice"] and not _state["shown"]:
            _state["shown"] = True
            return _state["notice"]
    return None

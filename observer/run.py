import argparse, os, subprocess, sys, time, fcntl
from pathlib import Path
from observer import config, gitwatch as _gitwatch, heuristics as _heuristics
from observer import judge as _judge, report as _report
from observer import heartbeat as _heartbeat
from observer import coverage as _coverage
from observer.isolation import check_runtime, IsolationError

FAIL_THRESHOLD = 5

def _default_deps():
    return {"gitwatch": _gitwatch, "heuristics": _heuristics,
            "judge": _judge, "report": _report, "coverage": _coverage}

def _failcount_path(name, sd):
    return os.path.join(sd, f"{name}.failcount")

def _bump_fail(name, sd):
    os.makedirs(sd, exist_ok=True)
    n = 0
    p = _failcount_path(name, sd)
    if os.path.exists(p):
        n = int(Path(p).read_text().strip() or "0")
    n += 1
    Path(p).write_text(str(n))
    return n

def _alerted_path(name, sd):
    return os.path.join(sd, f"{name}.failalerted")

def _reset_fail(name, sd):
    p = _failcount_path(name, sd)
    if os.path.exists(p):
        os.remove(p)
    ap = _alerted_path(name, sd)
    if os.path.exists(ap):
        os.remove(ap)

def _scan_commits(d, repo, commits, state_dir, claude_bin, notify_sh, dry_run, old):
    git, heur, jdg, rep = d["gitwatch"], d["heuristics"], d["judge"], d["report"]
    last_good = old
    alerts = 0
    scanned = 0
    for c in commits:
        try:
            diff = git.commit_diff(repo.observer_mirror, c.sha)
            paths = git.changed_paths(repo.observer_mirror, c.sha)
            flags = heur.scan_diff(diff)
            for p in paths:
                flags += heur.scan_diff("+" + p)
            verdict = jdg.judge(repo.repo, c.sha, c.subject, flags, diff, claude_bin)
            if dry_run:
                print(f"{repo.repo}@{c.sha[:8]} {verdict.level} "
                      f"{verdict.reason} ({verdict.confidence})")
            else:
                d["coverage"].record_scan(repo, c, diff, verdict, state_dir)
                rep.report(repo.repo, c.sha, c.author, c.subject, verdict,
                           state_dir, notify_sh)
            if verdict.level != "benign":
                alerts += 1
            if not verdict.complete or verdict.level == "unknown":
                return {"alerts": alerts, "scanned": scanned, "last_good": last_good, "error": "incomplete coverage: " + c.sha}
            scanned += 1
            last_good = c.sha
        except Exception as e:
            return {"alerts": alerts, "scanned": scanned, "last_good": last_good,
                    "error": f"{c.sha[:8]}: {str(e)[:80]}"}
    return {"alerts": alerts, "scanned": scanned, "last_good": last_good, "error": None}

def scan_repo(repo, state_dir, claude_bin, notify_sh, dry_run=False, deps=None):
    d = deps or _default_deps()
    git, heur, jdg, rep = d["gitwatch"], d["heuristics"], d["judge"], d["report"]
    name = repo.name
    if not repo.observer_mirror or os.path.realpath(repo.observer_mirror) == os.path.realpath(repo.clone_build):
        return {"repo": name, "status": "mirror_required", "scanned": 0, "alerts": 0, "note": "configure a separately protected OBSERVER_MIRROR"}
    try:
        git.verify_mirror(repo.observer_mirror, repo.repo)
    except Exception as exc:
        return {"repo": name, "status": "mirror_unverified", "scanned": 0, "alerts": 0, "note": str(exc)[:120]}
    try:
        if not dry_run:
            git.fetch(repo.observer_mirror)
    except Exception as e:
        n = _bump_fail(name, state_dir)
        ap = _alerted_path(name, state_dir)
        if n >= FAIL_THRESHOLD and not os.path.exists(ap) and not dry_run:
            rep.report(repo.repo, "-", "-", "fetch failure",
                       _judge.Verdict("suspicious",
                                      f"observer cannot reach repo ({n} fails)",
                                      "n/a"),
                       state_dir, notify_sh)
            open(ap, "w").close()
        return {"repo": name, "status": "fetch_failed", "scanned": 0,
                "alerts": 0, "note": str(e)[:120]}
    if not dry_run:
        _reset_fail(name, state_dir)

    new = git.head_sha(repo.observer_mirror)
    old = git.read_sha(name, state_dir)

    if old is None:
        return {"repo": name, "status": "baseline_required", "scanned": 0,
                "alerts": 0, "note": new[:8]}
    try:
        d["coverage"].verify_checkpoint(repo, old, state_dir)
    except Exception as exc:
        return {"repo": name, "status": "checkpoint_unverified", "scanned": 0, "alerts": 0, "note": str(exc)[:120]}
    if old == new:
        return {"repo": name, "status": "up_to_date", "scanned": 0,
                "alerts": 0, "note": ""}

    if not git.is_ancestor(repo.observer_mirror, old, new):
        v = _judge.Verdict("malicious",
                           "history rewrite / force-push on main", "n/a")
        if not dry_run:
            rep.report(repo.repo, new, "-", "force-push", v, state_dir, notify_sh)
        else:
            print(f"{repo.repo}@{new[:8]} malicious force-push")
        try:
            commits = git.new_commits(repo.observer_mirror, old, new)
        except Exception as exc:
            return {"repo": name, "status": "coverage_gap", "scanned": 0, "alerts": 1, "note": str(exc)[:120]}
        r = _scan_commits(d, repo, commits, state_dir, claude_bin,
                          notify_sh, dry_run, old)
        # History rewrites require explicit operator re-baselining even after scanning.
        # Retain the previous coverage boundary; never silently bless rewritten history.
        return {"repo": name, "status": "force_push",
                "scanned": r["scanned"], "alerts": 1 + r["alerts"],
                "note": r["error"] or ""}

    commits = git.new_commits(repo.observer_mirror, old, new)
    r = _scan_commits(d, repo, commits, state_dir, claude_bin, notify_sh,
                      dry_run, old)
    if r["error"] is not None:
        if not dry_run:
            git.write_sha(name, r["last_good"], state_dir)
        return {"repo": name, "status": "scan_error", "scanned": r["scanned"],
                "alerts": r["alerts"], "note": r["error"]}
    if not commits or r["last_good"] != new:
        return {"repo": name, "status": "coverage_gap", "scanned": r["scanned"], "alerts": r["alerts"], "note": "enumeration did not reach expected head"}
    if not dry_run:
        git.write_sha(name, new, state_dir)
    return {"repo": name, "status": "scanned", "scanned": len(commits),
            "alerts": r["alerts"], "note": ""}

def main(argv=None):
    ap = argparse.ArgumentParser(prog="observer")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--baseline-repo", help="explicitly declare coverage begins at current main")
    ap.add_argument("--baseline-sha", help="expected current main SHA for baseline declaration")
    ap.add_argument("--baseline-reason", help="why coverage begins at this SHA; required for baseline")
    ap.add_argument("--replace-baseline", action="store_true", help="explicitly replace existing coverage after investigating a gap")
    args = ap.parse_args(argv)
    if bool(args.baseline_repo) != bool(args.baseline_sha):
        ap.error("baseline repo and expected SHA must be supplied together")
    if args.baseline_repo and not (args.baseline_reason or "").strip():
        ap.error("baseline declaration requires --baseline-reason")
    if (args.baseline_reason or args.replace_baseline) and not args.baseline_repo:
        ap.error("baseline options require --baseline-repo")

    repos_d = os.path.join(config.WD, "repos.d")
    state_dir = config.STATE_DIR
    check_runtime(state_dir)
    if not args.dry_run:
        os.makedirs(state_dir, exist_ok=True)
    lock_path = os.path.join(state_dir, "scan.lock")
    lock = open(lock_path, "r" if args.dry_run else "a") if not args.dry_run or os.path.exists(lock_path) else open(os.devnull, "r")
    try:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0
        notify_sh = os.path.join(config.WD, "notify.sh")
        claude_bin = config.resolve_claude()

        repos = config.load_repos(repos_d)
        if args.baseline_repo:
            selected = [r for r in repos if r.repo == args.baseline_repo]
            if len(selected) != 1 or not selected[0].observer_mirror:
                ap.error("baseline requires one enabled repo with a trusted mirror")
            repo = selected[0]
            if os.path.realpath(repo.observer_mirror) == os.path.realpath(repo.clone_build):
                ap.error("observer mirror must be independent of worker clone")
            _gitwatch.verify_mirror(repo.observer_mirror, repo.repo)
            if not args.dry_run:
                _gitwatch.fetch(repo.observer_mirror)
            actual = _gitwatch.head_sha(repo.observer_mirror)
            if actual != args.baseline_sha:
                ap.error("expected baseline SHA differs from fetched main")
            existing = _coverage.baseline_path(repo, state_dir).exists() or os.path.exists(os.path.join(state_dir, repo.name + ".sha"))
            if existing and not args.replace_baseline:
                ap.error("existing checkpoint/baseline requires explicit --replace-baseline after investigation")
            if not args.dry_run:
                _coverage.declare_baseline(repo, actual, state_dir, args.baseline_reason)
                _gitwatch.write_sha(repo.name, actual, state_dir)
            return 0
        summaries = []
        for r in repos:
            try:
                s = scan_repo(r, state_dir, claude_bin, notify_sh, dry_run=args.dry_run)
            except Exception as exc:
                s = {"repo": r.name, "status": "scan_error", "scanned": 0, "alerts": 0, "note": type(exc).__name__}
            summaries.append(s)

        if not args.dry_run:
            from forge.notifications import atomic_json
            atomic_json(os.path.join(state_dir, "coverage.json"), {"checked_at": time.time(), "repositories": summaries})
            _heartbeat.record_scan(state_dir, sum(s["scanned"] for s in summaries))
            if sum(s["alerts"] for s in summaries) > 0:
                _heartbeat.record_alert(state_dir)
            if all(s["status"] in ("scanned", "up_to_date") for s in summaries) and repos:
                _heartbeat.maybe_heartbeat(state_dir, len(repos), notify_sh)

        line = time.strftime("%F %T") + " " + " | ".join(
            f"{s['repo']}:{s['status']}"
            + (f"(scan {s['scanned']},alert {s['alerts']})"
               if s['status'] == 'scanned' else "")
            for s in summaries)
        if not args.dry_run:
            with open(os.path.join(state_dir, "run.log"), "a") as f:
                f.write(line + "\n")
        else:
            print(line)
        return 0 if repos and all(s["status"] in ("scanned", "up_to_date") for s in summaries) else 1
    finally:
        lock.close()

if __name__ == "__main__":
    sys.exit(main())

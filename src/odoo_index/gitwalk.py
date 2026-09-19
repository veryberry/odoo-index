"""Stream commits from a git range and yield curated documents."""
import re
import subprocess

from .filters import commit_tag, curate_commit

RS = "\x1e"  # record separator (between commits)
US = "\x1f"  # field separator
GS = "\x1d"  # marks end of header fields / start of patch


def _file_exclude_pathspecs(filters):
    """Pathspecs for files `Filters.file_skipped` would drop anyway.

    Excluding them at the git level means git never generates (nor we parse)
    the diff at all — for Odoo that is most of the bytes, since .pot/.po
    regeneration commits touch every module with enormous hunks.
    """
    out = []
    if filters.skip_pot:
        out.append("*.pot")
    if filters.skip_i18n_po:
        out.append("**/i18n/*.po")
    if filters.skip_static_lib:
        out.append("**/static/lib/**")
    if filters.skip_minified:
        out.append("*.min.*")
    out.extend(sorted(filters.lockfiles))
    return [f":(exclude,glob){p}" for p in out]


def _rev_endpoints(rev_range):
    return [r.lstrip("^") for r in re.split(r"\.{2,3}", rev_range or "") if r.strip()]


def _module_exclude_pathspecs(repo, rev_range, include_roots, filters):
    """Pathspecs for whole modules `Filters.module_excluded` would drop.

    Enumerates the real directory names at both ends of the range and reuses
    Filters.module_excluded, so this is exactly the set Python would discard —
    just discarded before git spends time diffing it.
    """
    dirs = set()
    for rev in _rev_endpoints(rev_range):
        for root in include_roots:
            cmd = ["git", "-C", repo, "ls-tree", "-d", "--name-only", rev]
            if root not in (".", "", "./"):
                cmd.append(root)
            p = subprocess.run(cmd, capture_output=True, text=True)
            if p.returncode != 0:
                return []  # unknown rev / odd layout: skip the optimisation
            dirs.update(ln for ln in p.stdout.splitlines() if ln.strip())
    return [
        f":(exclude){d}"
        for d in sorted(dirs)
        if filters.module_excluded(filters.module_of(d))
    ]


def _log_cmd(repo, rev_range, include_roots, no_merges, exclude_pathspecs=()):
    fmt = RS + US.join(["%H", "%an", "%ae", "%aI", "%cI", "%s", "%b"]) + GS
    cmd = [
        "git", "-C", repo, "log", rev_range,
        "--no-color", "-M", "--find-renames",
        f"--format={fmt}", "-p",
    ]
    if no_merges:
        cmd.insert(4, "--no-merges")
    cmd.append("--")
    cmd.extend(include_roots)
    cmd.extend(exclude_pathspecs)
    return cmd


def _records(stream):
    """Split an RS-delimited stream into records without rescanning the buffer.

    The naive `buf += line; buf.split(RS)` is quadratic in the record size: a
    commit with a 100k-line patch re-scans a growing multi-megabyte string once
    per line. Here each line is looked at exactly once.
    """
    cur = []
    for line in stream:
        if RS not in line:
            cur.append(line)
            continue
        parts = line.split(RS)
        cur.append(parts[0])
        yield "".join(cur)
        for mid in parts[1:-1]:
            yield mid
        cur = [parts[-1]]
    if cur:
        yield "".join(cur)


def iter_commits(cfg, filters):
    """Yield dicts for each commit that survives filtering."""
    f = cfg["filters"]
    excludes = []
    if f.get("git_pathspec_prefilter", True):
        excludes = _file_exclude_pathspecs(filters) + _module_exclude_pathspecs(
            cfg["repo"], cfg["rev_range"], cfg["include_roots"], filters
        )
    cmd = _log_cmd(
        cfg["repo"],
        cfg["rev_range"],
        cfg["include_roots"],
        f.get("skip_merges", True),
        excludes,
    )
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, bufsize=1024 * 1024, text=True, errors="replace"
    )
    try:
        for record in _records(proc.stdout):
            doc = _parse_record(record, filters)
            if doc:
                yield doc
    finally:
        proc.stdout.close()
        proc.wait()


def _parse_record(record, filters):
    record = record.lstrip(RS)
    if not record.strip():
        return None
    head, _, patch = record.partition(GS)
    fields = head.split(US)
    if len(fields) < 7:
        return None
    sha, an, ae, aiso, ciso, subject, body = fields[:7]
    if filters.subject_tag_skipped(subject):
        return None
    diff_text, files, modules = curate_commit(patch, filters)
    if diff_text is None:
        return None
    header = f"modules: {', '.join(modules)}\n"
    doc_text = f"{header}{subject}\n\n{body}\n\n{diff_text}".strip()
    return {
        "sha": sha,
        "author_name": an,
        "author_email": ae,
        "authored_at": aiso or None,
        "committed_at": ciso or None,
        "tag": commit_tag(subject),
        "subject": subject,
        "body": body,
        "modules": modules,
        "files": files,
        "n_files": len(files),
        "diff_text": diff_text,
        "doc_text": doc_text,
    }

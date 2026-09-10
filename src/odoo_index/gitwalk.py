"""Stream commits from a git range and yield curated documents."""
import subprocess

from .filters import commit_tag, curate_commit

RS = "\x1e"  # record separator (between commits)
US = "\x1f"  # field separator
GS = "\x1d"  # marks end of header fields / start of patch


def _log_cmd(repo, rev_range, include_roots, no_merges):
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
    return cmd


def iter_commits(cfg, filters):
    """Yield dicts for each commit that survives filtering."""
    cmd = _log_cmd(
        cfg["repo"],
        cfg["rev_range"],
        cfg["include_roots"],
        cfg["filters"].get("skip_merges", True),
    )
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, bufsize=1024 * 1024, text=True, errors="replace"
    )
    buf = ""
    try:
        for chunk in proc.stdout:
            buf += chunk
            # Records are RS-delimited. Everything before the LAST RS is complete;
            # keep the trailing partial record in the buffer.
            parts = buf.split(RS)
            buf = parts.pop()  # incomplete tail
            for record in parts:
                doc = _parse_record(record, filters)
                if doc:
                    yield doc
        doc = _parse_record(buf, filters)  # final record
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

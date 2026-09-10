"""Pure, testable filtering logic. No git, no DB, no network."""
import fnmatch
import os
import re

_TAG_RE = re.compile(r"\s*\[([A-Z0-9_]+)\]")


def commit_tag(subject: str):
    m = _TAG_RE.match(subject or "")
    return m.group(1) if m else None


class Filters:
    def __init__(self, cfg):
        f = cfg["filters"]
        self.module_exclude = f["module_exclude_globs"]
        self.module_allow = f["module_allow_globs"]
        self.lockfiles = set(f.get("lockfiles", []))
        self.skip_pot = f.get("skip_pot", True)
        self.skip_i18n_po = f.get("skip_i18n_po", True)
        self.skip_static_lib = f.get("skip_static_lib", True)
        self.skip_minified = f.get("skip_minified", True)
        self.asset_exts = set(f.get("asset_extensions", []))
        self.asset_max = int(f.get("asset_diff_max_chars", 2000))
        self.commit_max = int(f.get("commit_diff_max_chars", 24000))
        self.skip_subject_tags = set(f.get("skip_subject_tags", []))
        self.only_installed = bool(f.get("only_installed", False))
        self.installed = set()
        if self.only_installed:
            mf = f.get("installed_modules_file")
            if mf and os.path.exists(mf):
                self.installed = {
                    ln.strip() for ln in open(mf) if ln.strip() and not ln.startswith("#")
                }

    # ---- module scope -----------------------------------------------------
    @staticmethod
    def module_of(path: str) -> str:
        if path.startswith("addons/"):
            parts = path.split("/", 2)
            return parts[1] if len(parts) > 1 else "addons"
        if path.startswith("odoo/"):
            return "odoo"
        return path.split("/", 1)[0]

    def module_excluded(self, module: str) -> bool:
        # framework core is always kept
        if module == "odoo":
            return False
        for g in self.module_allow:
            if fnmatch.fnmatch(module, g):
                return False
        for g in self.module_exclude:
            if fnmatch.fnmatch(module, g):
                return True
        if self.only_installed and module not in self.installed and module != "odoo":
            return True
        return False

    # ---- file skips -------------------------------------------------------
    def file_skipped(self, path: str) -> bool:
        base = os.path.basename(path)
        if self.skip_pot and path.endswith(".pot"):
            return True
        if self.skip_i18n_po and "/i18n/" in path and path.endswith(".po"):
            return True
        if self.skip_static_lib and "/static/lib/" in path:
            return True
        if self.skip_minified and ".min." in base:
            return True
        if base in self.lockfiles:
            return True
        return False

    @staticmethod
    def _ext(path: str) -> str:
        base = os.path.basename(path)
        return base.rsplit(".", 1)[1].lower() if "." in base else ""

    def cap_asset_block(self, path: str, block: str) -> str:
        """If an asset file's diff is oversized, replace body with a stub."""
        if self._ext(path) in self.asset_exts and len(block) > self.asset_max:
            header = block.split("\n", 1)[0]
            return (
                f"{header}\n"
                f"[diff omitted: {len(block)} chars > {self.asset_max} "
                f"({self._ext(path)} asset)]\n"
            )
        return block

    def subject_tag_skipped(self, subject: str) -> bool:
        tag = commit_tag(subject)
        return tag in self.skip_subject_tags


_DIFF_HEADER_RE = re.compile(r"(?m)^diff --git a/(.+?) b/(\S+)")


def split_file_blocks(patch: str):
    """Split a unified-diff patch into (new_path, block_text) per file."""
    starts = [m.start() for m in re.finditer(r"(?m)^diff --git ", patch)]
    out = []
    for i, s in enumerate(starts):
        e = starts[i + 1] if i + 1 < len(starts) else len(patch)
        block = patch[s:e]
        m = _DIFF_HEADER_RE.match(block)
        if not m:
            continue
        a_path, b_path = m.group(1), m.group(2)
        path = b_path if b_path != "dev/null" else a_path
        out.append((path, block))
    return out


def curate_commit(patch: str, filters: "Filters"):
    """Apply module/file/asset filters to a commit's patch.

    Returns (diff_text, files, modules) or (None, [], set()) if the commit has
    no content left after filtering (e.g. it only touched excluded modules).
    """
    kept, files, modules = [], [], set()
    for path, block in split_file_blocks(patch):
        module = filters.module_of(path)
        if filters.module_excluded(module):
            continue
        if filters.file_skipped(path):
            continue
        kept.append(filters.cap_asset_block(path, block))
        files.append(path)
        modules.add(module)
    if not kept:
        return None, [], set()
    diff_text = "".join(kept)
    if len(diff_text) > filters.commit_max:
        diff_text = (
            diff_text[: filters.commit_max]
            + f"\n[commit diff truncated at {filters.commit_max} chars]\n"
        )
    return diff_text, files, sorted(modules)

"""Lightweight self-tests for the filtering rules. Run: python -m tests.test_filters"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from odoo_index.config import Config  # noqa: E402
from odoo_index.filters import Filters, curate_commit, commit_tag  # noqa: E402

cfg = Config(os.path.join(os.path.dirname(__file__), "..", "config.yaml"))
f = Filters(cfg)


def check(name, cond):
    print(("PASS" if cond else "FAIL"), name)
    assert cond, name


# module scope
check("l10n_fr excluded", f.module_excluded("l10n_fr"))
check("l10n_de kept", not f.module_excluded("l10n_de"))
check("l10n_de_reports kept", not f.module_excluded("l10n_de_reports"))
check("l10n_ch kept", not f.module_excluded("l10n_ch"))
check("point_of_sale excluded", f.module_excluded("point_of_sale"))
check("pos_restaurant excluded", f.module_excluded("pos_restaurant"))
check("postmark NOT excluded by pos rule", not f.module_excluded("postmark_mail_status"))
check("spreadsheet excluded", f.module_excluded("spreadsheet"))
check("spreadsheet_dashboard excluded", f.module_excluded("spreadsheet_dashboard"))
check("payment_stripe excluded", f.module_excluded("payment_stripe"))
check("payment_paypal kept (installed)", not f.module_excluded("payment_paypal"))
check("payment (base) kept", not f.module_excluded("payment"))
check("sale kept", not f.module_excluded("sale"))
check("odoo core kept", not f.module_excluded("odoo"))

# module_of
check("module_of addons", f.module_of("addons/sale/models/sale.py") == "sale")
check("module_of odoo", f.module_of("odoo/models.py") == "odoo")

# file skips
check("pot skipped", f.file_skipped("addons/sale/i18n/sale.pot"))
check("i18n po skipped", f.file_skipped("addons/sale/i18n/de.po"))
check("static/lib skipped", f.file_skipped("addons/web/static/lib/jquery/jquery.js"))
check("min.js skipped", f.file_skipped("addons/web/static/src/foo.min.js"))
check("lockfile skipped", f.file_skipped("addons/web/package-lock.json"))
check("normal py kept", not f.file_skipped("addons/sale/models/sale_order.py"))

# tag
check("tag parse", commit_tag("[REF] sale: rework") == "REF")

# asset cap
big_js = "diff --git a/x.js b/x.js\n" + ("+a\n" * 2000)
capped = f.cap_asset_block("x.js", big_js)
check("asset capped", "diff omitted" in capped and len(capped) < len(big_js))
small_py = "diff --git a/x.py b/x.py\n" + ("+a\n" * 2000)
check("py not asset-capped", f.cap_asset_block("x.py", small_py) == small_py)

# curate: commit touching only excluded modules -> dropped
patch_excluded = (
    "diff --git a/addons/l10n_fr/x.py b/addons/l10n_fr/x.py\n@@\n+x\n"
    "diff --git a/addons/point_of_sale/y.py b/addons/point_of_sale/y.py\n@@\n+y\n"
)
d, files, mods = curate_commit(patch_excluded, f)
check("excluded-only commit dropped", d is None)

# curate: mixed commit keeps only allowed hunks
patch_mixed = (
    "diff --git a/addons/sale/a.py b/addons/sale/a.py\n@@\n+keep\n"
    "diff --git a/addons/l10n_fr/b.py b/addons/l10n_fr/b.py\n@@\n+drop\n"
    "diff --git a/addons/sale/i18n/de.po b/addons/sale/i18n/de.po\n@@\n+dropo\n"
)
d, files, mods = curate_commit(patch_mixed, f)
check("mixed keeps sale only", d is not None and "keep" in d and "drop" not in d)
check("mixed module set == {sale}", mods == ["sale"])
check("po file dropped from files", files == ["addons/sale/a.py"])

print("\nAll filter self-tests passed.")

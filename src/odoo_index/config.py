import os
import yaml


class Config:
    def __init__(self, path="config.yaml"):
        with open(path) as fh:
            self._d = yaml.safe_load(fh)
        # env overrides
        self._d["database_url"] = os.environ.get(
            "DATABASE_URL", self._d.get("database_url")
        )
        if os.environ.get("ODOO_REPO"):
            self._d["repo"] = os.environ["ODOO_REPO"]
        if os.environ.get("REV_RANGE"):
            self._d["rev_range"] = os.environ["REV_RANGE"]

    def __getitem__(self, k):
        return self._d[k]

    def get(self, k, default=None):
        return self._d.get(k, default)

    @property
    def voyage_api_key(self):
        key = os.environ.get("VOYAGE_API_KEY")
        if not key:
            raise SystemExit("VOYAGE_API_KEY is not set in the environment")
        return key

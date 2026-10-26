"""补齐空配置卷后交给前台服务；已有管理员、模型和编队配置均保留。"""

import os
import sys
from pathlib import Path


def initialize(configs: Path) -> None:
    configs.mkdir(parents=True, exist_ok=True)
    models = configs / "models.yaml"
    if not models.exists():
        with models.open("x", encoding="utf-8") as output:
            output.write("providers: []\nmodels: []\n")
        models.chmod(0o600)


if __name__ == "__main__":
    initialize(Path("/app/configs"))
    os.execvpe(sys.argv[1], sys.argv[1:], os.environ)

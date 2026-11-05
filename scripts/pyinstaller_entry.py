"""PyInstaller 打包入口：让 spiritscout 作为普通包被收集，再交给正式 CLI。"""

from spiritscout.__main__ import main

if __name__ == "__main__":
    raise SystemExit(main())

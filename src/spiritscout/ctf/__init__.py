"""CTF 专项能力（设计书 4.4）：五方向专项子 Agent + Flag 统一提交模块。

CTF 模式不启用端口扫描（硬约束 C7 / 5.2）；所有专项直接面向赛题入口
（题目 URL、附件、源码包）定向突破。
"""

from spiritscout.ctf.flags import FlagManager, SubmitResult, extract_flags

__all__ = ["FlagManager", "SubmitResult", "extract_flags"]

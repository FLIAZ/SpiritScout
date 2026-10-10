"""模式装配器（设计书 4.4/4.5/5.1~5.3）。

按运行模式装配子 Agent 集群、战术映射、模式专属模块：
- SRC：侦察 + Web 漏洞（链路）；
- CTF：五方向专项子 Agent + Flag 统一提交模块（不启用端口扫描）；
- 渗透赛：按赛制类型混编（Jeopardy=CTF 池+调度器 / AWD=攻防双线 / 靶场=SRC 池）。
"""

from spiritscout.modes.assembler import ModeAssembler, ModeAssembly

__all__ = ["ModeAssembler", "ModeAssembly"]

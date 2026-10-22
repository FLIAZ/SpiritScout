# 灵枢（SpiritScout）容器镜像：Python 3.11 + bubblewrap 沙箱
#
# 设计要点：
#   - 智能体的 shell/python 沙箱（sandbox_exec）硬依赖 bubblewrap，且代码
#     明确「bwrap 不可用时报错、不静默降级」，所以镜像内必须装 bwrap；
#   - 依赖装进 /opt/venv，项目以可编辑方式安装到 /app；docker-compose 会把
#     宿主机源码挂到 /app，因此改代码无需重建镜像；
#   - CTF 工具箱（pwntools/pycryptodome/z3/scapy/binwalk/sqlmap 等）体积大、
#     构建慢，默认不装，需要时 --build-arg INSTALL_TOOLKIT=1。
FROM node:22-bookworm-slim AS chrome-mcp
RUN npm install --prefix /opt/chrome-mcp --ignore-scripts @playwright/mcp@0.0.83

FROM python:3.11-slim
COPY --from=chrome-mcp /usr/local/bin/node /usr/local/bin/node
COPY --from=chrome-mcp /opt/chrome-mcp /opt/chrome-mcp

# 依赖源：默认官方 PyPI；国内可覆盖为镜像源，例如
#   docker compose build --build-arg PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
ARG PIP_INDEX_URL=https://pypi.org/simple

# 0 = 只装核心依赖（默认）；1 = 额外安装 .[toolkit] 工具箱
ARG INSTALL_TOOLKIT=0

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    VIRTUAL_ENV=/opt/venv \
    PATH=/opt/venv/bin:$PATH

# bubblewrap：沙箱运行时；git/curl/procps：MCP 接入与排障常用
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      bubblewrap ca-certificates curl git procps libstdc++6 \
 && rm -rf /var/lib/apt/lists/*

RUN python -m venv /opt/venv \
 && pip install --upgrade pip --index-url "$PIP_INDEX_URL"

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
# 可编辑安装：指向 /app/src，运行时被宿主机源码挂载覆盖
RUN pip install -e . --index-url "$PIP_INDEX_URL"

# 可选工具箱：部分包需要编译（gmpy2 等），装完保留编译链以免后续 pip 不可用
RUN if [ "$INSTALL_TOOLKIT" = "1" ]; then \
      apt-get update \
      && apt-get install -y --no-install-recommends build-essential \
      && pip install -e '.[toolkit]' --index-url "$PIP_INDEX_URL" \
      && rm -rf /var/lib/apt/lists/*; \
    fi

EXPOSE 10010
# 容器内必须绑定非回环地址才能被端口映射访问；程序此时强制要求管理员凭据
# （SPIRITSCOUT_ADMIN_USERNAME / SPIRITSCOUT_ADMIN_PASSWORD，旧版
#   SPIRITSCOUT_SERVER_PASSWORD 仍兼容），由 docker-compose 从 .env 注入
CMD ["python", "-m", "spiritscout", "--config", "configs/system.yaml", "--host", "0.0.0.0", "--port", "10010"]

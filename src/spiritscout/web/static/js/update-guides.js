"use strict";
/* 更新教程只展示操作说明；不向后端提交这些命令，不猜测不存在的发布产物。 */
const UpdateGuides = (() => {
  function currentGuide(installation, reason = "") {
    if (/ASGI|进程管理器|托管/.test(reason)) return "managed";
    if (installation.kind !== "system_package") return ["source", "package", "container", "binary", "appimage"].includes(installation.kind) ? installation.kind : "unknown";
    const label = (installation.label || "").toLowerCase();
    return label.includes("deb") || label.includes("apt") ? "deb" : label.includes("rpm") ? "rpm" : label.includes("pacman") || label.includes("arch") ? "arch" : "unknown";
  }
  function guides(release) {
    const tag = /^v?\d+\.\d+\.\d+$/.test(release?.tag || "") ? release.tag : I18n.t("目标版本标签");
    const archive = `https://github.com/FLIAZ/SpiritScout/archive/refs/tags/${tag}.zip`;
    const sync = `git status --short\ngit fetch origin tag "${tag}"\ngit merge --ff-only --no-overwrite-ignore "${tag}"`;
    const code = (title, text) => `<div class="update-guide-command"><div>${esc(title)}</div><pre><code>${esc(text)}</code></pre></div>`;
    const p = text => `<p>${esc(text)}</p>`;
    return [
      {id:"source", title:I18n.t("Git 源码 · pip / uv 环境"), body:
        p(I18n.t("先在原启动终端正常停止服务，进入原仓库目录。备份实际使用的配置、.env 和工作区，确认 origin 指向官方仓库。")) +
        p(I18n.t("先单独运行 git status --short；输出不为空时先自行备份和处理本地改动，再继续。提交本地修改也可能造成分支分叉，不能靠强制覆盖解决。")) +
        code(I18n.t("检查后同步目标标签；遇到冲突或无法快进时停止处理"), sync) +
        p(I18n.t("同步成功后，按原安装方式更新依赖。以下是基础安装示例；原来启用的 extras / groups 需保留。若此前使用网页更新，请先查看更新状态目录 active.json 的 python 字段，使用其中的解释器安装依赖，不要只更新旧 .venv。")) +
        code(I18n.t("Linux · pip 虚拟环境"), '.venv/bin/python -m pip install -e .\n./launch.sh') +
        code(I18n.t("Windows PowerShell · pip 虚拟环境"), '.\\.venv\\Scripts\\python.exe -m pip install -e .\n.\\launch.ps1') +
        code(I18n.t("uv 环境 · 增量安装基础依赖"), 'uv pip install --python .venv/bin/python -e .\n./launch.sh') +
        p(I18n.t("uv sync 用户：有 uv.lock 时沿用原有 groups / extras 参数并添加 --frozen；sync 会按选择的依赖组同步环境，不要忽略原来的可选依赖。重启时沿用原 --config、端口和环境变量。") )},
      {id:"package", title:I18n.t("Python 包 · pip / uv / wheel"), body:
        p(I18n.t("结束任务、备份数据并正常停止服务。先激活原运行环境；下面的 python 必须是运行灵枢的解释器。若曾网页更新过，以 active.json 记录的解释器为准。")) +
        p(I18n.t("不确定是否发布到 PyPI 时，使用官方目标标签的源码包；不要从名称相似的第三方项目安装。下面的标签与上方检测结果一致。")) +
        code(I18n.t("pip · 从正式发布的源码包升级"), `python -m pip install --upgrade "${archive}"`) +
        code(I18n.t("uv · 指定实际运行环境升级"), I18n.t`uv pip install --python "运行灵枢的Python路径" --upgrade "${archive}"`) +
        p(I18n.t("若最初使用 uv tool 安装，应继续交由 uv tool 管理；下面将安装来源固定到该正式标签。")) +
        code(I18n.t("仅适用于 uv tool 安装"), `uv tool install --force "${archive}"`) +
        p(I18n.t("若使用官方 wheel，先从对应 Release 下载并校验文件，再替换下面的文件名。按原方式安装额外依赖，并沿用原命令和配置启动。")) +
        code(I18n.t("pip · 本地 wheel"), I18n.t('python -m pip install --upgrade "./下载的新版本.whl"'))},
      {id:"container", title:I18n.t("Docker · 宿主助手 / 手动更新"), body:
        p(I18n.t("所有命令都在宿主机原部署目录执行。先结束任务，备份 .env、部署配置及持久化数据卷；不要删除数据卷。使用自定义服务名、-f 或 profile 时，沿用原来的部署参数。")) +
        p(I18n.t("方式一：启动宿主更新助手后回到网页点击“更新并重启”。默认 Docker 控制台端口是 10010；自定义端口、HTTPS 或远程部署时替换 --url。")) +
        code(I18n.t("在宿主启动助手并保持终端运行"), 'python3 scripts/update-container.py --url "http://127.0.0.1:10010" --project-dir "."') +
        p(I18n.t("助手不会自动读取 .env。控制台启用登录时，先通过 SPIRITSCOUT_UPDATE_USERNAME、SPIRITSCOUT_UPDATE_PASSWORD 环境变量提供管理员凭据。Windows 宿主可将 python3 换为 py -3。助手仅支持当前仓库构建、工作区已持久化且未挂载实时源码的 Compose 服务。")) +
        p(I18n.t("方式二：源码构建部署可手动同步并重建。先单独检查 git status --short，有本地改动或不能快进时停止；依照原部署方式停止服务后，再执行同步与重建。")) +
        code(I18n.t("源码构建示例 · 默认服务名 spiritscout"), `${sync}\ndocker compose build spiritscout\ndocker compose up -d --no-deps --wait spiritscout`) +
        p(I18n.t("方式三：纯镜像拉取部署，先在原 Compose 文件中将 image 改为原镜像源实际提供的目标标签，再执行下面命令。Git 发布标签不一定就是镜像标签，不要猜测镜像仓库地址。")) +
        code(I18n.t("仅适用于镜像拉取部署"), 'docker compose pull spiritscout\ndocker compose up -d --no-deps --wait spiritscout')},
      {id:"binary", title:I18n.t("独立程序 · ELF / EXE"), body:
        p(I18n.t("网页更新要求 Release 有匹配当前系统、架构和 SHA-256 的单文件程序。缺少附件时，等待对应平台正式产物；不要用源码 ZIP 替换 EXE / ELF。")) +
        p(I18n.t("手动更新：结束任务并完全退出程序 → 从官方 Release 下载相同系统、相同架构的新程序并核对 SHA-256 → 备份原程序及实际配置、数据 → 将新程序放回原安装目录，沿用原程序名和启动参数 → 启动后核对版本。Linux 下载后按需添加执行权限。")) +
        p(I18n.t("amd64 与 x86_64 是同一架构命名；arm64 与 aarch64 对应。单文件可只替换程序；包含 DLL 或资源目录的安装包应整体更新程序文件，保留用户配置和数据。正在运行的 EXE 不能直接覆盖。"))},
      {id:"appimage", title:"AppImage", body:
        p(I18n.t("已提供 AppImage 构建流程，应用内替换尚未接入。是否有当前版本的下载附件，以官方 Release 为准；没有对应附件时先等待该架构产物。")) +
        p(I18n.t("退出旧程序，备份实际配置和数据；下载匹配 x86_64 / arm64 的官方 AppImage 并校验 SHA-256。添加执行权限后，按该发布包说明启动。确认新版正常后再移除旧程序；桌面快捷方式也应指向新文件。")) +
        code(I18n.t("将文件名替换为实际下载的 AppImage"), I18n.t('chmod +x "./下载的新版本.AppImage"'))},
      {id:"deb", title:"Debian / Ubuntu · deb", body:
        p(I18n.t("已支持构建 deb 包，尚未配置官方 apt 软件源。以下仅适用于取得正式发布、匹配系统和架构的 deb 包之后，不代表当前版本已经发布下载附件。")) +
        p(I18n.t("停止灵枢服务并备份配置和数据，从官方 Release 下载目标 deb 并校验，然后使用 apt 安装本地包。按原方式启动服务并核对版本。不要手动替换 /usr 下由包管理器管理的文件。")) +
        code(I18n.t("替换成实际文件名，在下载目录执行"), I18n.t('sudo apt install "./下载的新版本.deb"'))},
      {id:"rpm", title:I18n.t("Fedora / RHEL 系 · rpm"), body:
        p(I18n.t("已支持构建 rpm 包，尚未配置官方软件源。取得正式发布且适配当前发行版、架构的 rpm 后，停止服务、备份数据并校验文件，再用原包管理器更新。是否已发布以 Release 为准，不要跨发行版强装。")) +
        code(I18n.t("使用 DNF 的系统 · 替换成实际文件名"), I18n.t('sudo dnf install "./下载的新版本.rpm"')) +
        p(I18n.t("使用 Zypper 等其他工具的发行版，继续沿用其包管理器和发行说明；重启方式按原服务配置执行。"))},
      {id:"arch", title:"Arch / Arch Linux ARM · pacman", body:
        p(I18n.t("已支持构建 .pkg.tar.zst，尚未提供 AUR 发布。取得正式发布且匹配架构的包后，停止服务、备份并校验文件，再通过 pacman 更新。aarch64 包面向 Arch Linux ARM，不能安装 x86_64 包。")) +
        code(I18n.t("本地软件包 · 替换成实际文件名"), I18n.t('sudo pacman -U "./下载的新版本.pkg.tar.zst"')) +
        p(I18n.t("如果将来通过 AUR / PKGBUILD 安装，应审阅更新后的 PKGBUILD，按原构建与安装方式更新；这里不假设任何 AUR 包已经存在。"))},
      {id:"managed", title:I18n.t("systemd / Supervisor / 外部 ASGI"), body:
        p(I18n.t("托管部署请使用原管理器停止服务，再按上面的真实安装方式更新；完成后由同一管理器启动，避免手动再启动一份进程占用端口。")) +
        p(I18n.t("systemd：检查实际单元的 WorkingDirectory、ExecStart 和环境配置，沿用该解释器、配置和单元名。Supervisor：先查看 supervisorctl status，使用实际进程名停止和启动。外部 ASGI：沿用原服务脚本与工作进程配置。")) +
        p(I18n.t("本项目没有假定统一的 systemd 单元名或 Supervisor 进程名；不要直接照抄不存在的服务名称。"))},
      {id:"unknown", title:I18n.t("无法识别 / 没有 Git 的源码目录"), body:
        p(I18n.t("先确认最初用哪个解释器、安装包、容器或启动脚本部署。没有 Git 的解压源码不能执行 git pull；从官方 Release 下载目标源码，在新目录准备依赖，再迁移自己实际使用的配置和数据。")) +
        p(I18n.t("迁移时保留原目录备份，不要把新包中的示例配置覆盖到自己的密钥、模型清单或会话数据上。无法确认安装方式时先联系部署维护者。"))},
      {id:"recovery", title:I18n.t("更新失败时怎么处理"), body:
        p(I18n.t("网页准备阶段失败通常不会停止原服务。先查看本机更新状态目录 jobs/运行标识/ 下的 worker.log、service.log 与 backup/；Docker 助手日志位于宿主仓库 .spiritscout-updates/host-运行标识.log。")) +
        p(I18n.t("源码状态目录在仓库 .spiritscout-updates，独立程序在程序旁；Python 包在当前用户数据目录的 spiritscout/updates 下，Windows 通常位于 LOCALAPPDATA，Linux 通常位于 XDG_DATA_HOME 或 ~/.local/share；容器在持久化工作区 .spiritscout-updates 下。")) +
        p(I18n.t("新版启动后失败可能涉及数据迁移。先停止相关服务，对照 backup/manifest.json 统一恢复程序、环境和数据；不要只换回旧 EXE 或删除更新锁反复重试。保留日志和备份交给维护者排查。"))}
    ];
  }
  function render(state) {
    const root = $("#update-guides");
    const matched = currentGuide(state.installation, state.reason);
    const key = `${state.release?.tag || "none"}|${matched}`;
    if (root.dataset.guideKey === key) return; // 轮询不重置用户展开的条目。
    const previous = root.dataset.guideKind === matched ? new Set(Array.from(root.querySelectorAll("details[open]")).map(el => el.dataset.guide)) : null;
    root.innerHTML = guides(state.release).map(item => `<details data-guide="${item.id}"${previous ? previous.has(item.id) ? " open" : "" : item.id === matched ? " open" : ""}><summary>${esc(item.title)}${item.id === matched ? I18n.html('<span class="update-guide-match">当前安装方式</span>') : ""}</summary><div class="update-guide-content">${item.body}</div></details>`).join("");
    root.dataset.guideKey = key;
    root.dataset.guideKind = matched;
  }
  return {render, currentGuide};
})();

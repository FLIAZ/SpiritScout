# Linux 构建与安装包

统一入口：

```bash
python3 scripts/build-linux-packages.py
```

需要 Docker、Python 3.11+、Go 1.26+、`dpkg-deb`、`rpmbuild`/`rpm`、`zstd`、`desktop-file-validate` 和网络。
不会向宿主安装灵枢，也不修改运行环境、配置或会话。构建只将 `src/`、`packaging/`、
项目元数据、许可证和示例配置复制进隔离环境，不挂载 Docker socket、用户主目录或真实工作区。
注意：打包的是当前工作区源代码，未提交的代码改动也会进入产物；它不是已发布正式 Release。

默认构建本机架构的五个产物，位于 `dist/linux/`：

| 类型 | 文件名模式 | 安装 / 使用方式 |
| --- | --- | --- |
| 独立 ELF | `spiritscout-linux-x86_64` / `spiritscout-linux-arm64` | 添加执行权限，直接运行 |
| AppImage | `SpiritScout-版本-架构.AppImage` | 添加执行权限，直接运行；无 FUSE 时可用 `--appimage-extract-and-run` |
| Debian / Ubuntu | `spiritscout_版本-1_amd64.deb` / `arm64.deb` | `sudo apt install ./实际文件.deb` |
| Fedora / RHEL | `spiritscout-版本-1.x86_64.rpm` / `aarch64.rpm` | `sudo dnf install ./实际文件.rpm` |
| Arch / Arch Linux ARM | `spiritscout-版本-1-x86_64.pkg.tar.zst` / `aarch64.pkg.tar.zst` | `sudo pacman -U ./实际文件.pkg.tar.zst` |

amd64 / x86_64 为同一架构，arm64 / aarch64 为同一架构。Arch 包按 ALPM 的原生元数据和
Zstandard 压缩 tar 格式生成，并非改扩展名的 deb/rpm。包本身未签名，不修改系统的签名校验策略。
校验下载的 `SHA256SUMS-架构`，该文件用于检测文件完整性，不替代发布者签名。

## 构建基线与外部工具

PyInstaller 在官方 `python:3.11-slim-bullseye` 的共享 Python 3.11 环境构建，目标最低 glibc 为 2.31；
不适用于 musl/Alpine，也不保证旧于该基线的发行版可运行。AppImage 是通用封装格式，不表示
任意年代的 Linux 或不同 CPU 架构都可运行。AppImage 使用本机 squashfs-tools 与官方 type-2 runtime 封装；runtime 从官方仓库下载，
核对 GitHub 提供的 SHA-256，实际工具摘要写入 `appimage-tools-架构.json`。

程序内置控制台、Python 运行时、核心依赖、前端、知识种子、报告模板与分词缓存。
AppImage 和系统包也包含静态 Go 会话录制代理及其许可证；裸 ELF 旁另附同架构代理附件，
使用裸 ELF 时可用 `SPIRITSCOUT_TRAFFIC_PROXY` 指向该附件，或把代理以 `spiritscout-traffic-proxy` 名称放入 PATH。
外部安全工具没有全部捆绑：完整沙箱仍需 bubblewrap、系统 Python 3.11+；浏览器工具需
Node.js 22+、Chromium/Chrome 和相应 MCP；流量包解析需 tshark。CTF 扩展工具按原部署说明安装。
程序内包含隔离环境第三方依赖的许可证；精确 Python 依赖清单另存为 `build-requirements-架构.txt`。

可以复用已有、架构匹配的 ELF，只重新封装：

```bash
python3 scripts/build-linux-packages.py --binary dist/linux/spiritscout-linux-x86_64
```

用 `--formats deb rpm` 只构建指定格式，`--output` 指定产物目录。架构从 ELF 头校验，
不允许将本机程序重命名为其他架构。

## ARM64 构建

脚本支持 `--arch aarch64`，需要在真正的 ARM64 环境执行。本机没有 ARM64 执行环境时，
不会伪造 ARM64 产物，也不会自动在宿主注册特权 QEMU/binfmt 处理器。

仓库提供手动触发的 `.github/workflows/linux-packages.yml`，分别在 GitHub 原生 x86_64 和
ARM64 runner 构建，通过 Actions artifacts 下载产物。工作流不会创建标签、上传 Release 或
推送源码；提交/推送和触发远端构建需要维护者自行授权执行。

## 首次启动、数据与卸载

安装 deb/rpm/Arch 后运行 `spiritscout`，也可以从应用菜单启动终端控制台；默认访问
`http://127.0.0.1:8000`。不会安装或自动启用系统服务，也不会自动打开公网监听。

发行包和 AppImage 将配置与工作区放在 `${XDG_DATA_HOME:-~/.local/share}/spiritscout/`：

- `configs/system.yaml`：首次从示例复制，之后保留用户修改。
- `configs/models.yaml`：首次创建空清单，随后在控制台配置模型。
- `.spiritscout/`：会话、证据、审计和密钥库。
- `.spiritscout-updates/`：版本检查缓存和更新状态。

可用 `SPIRITSCOUT_DATA_HOME` 指定独立数据目录；多个版本不要同时共用一份运行工作区。
裸单文件默认在程序旁初始化，指定该变量后使用指定目录。自定义 `--config` 建议传绝对路径。
卸载系统包不删除上述用户数据；升级也不覆盖用户配置。

系统包由原包管理器更新，AppImage 手动换包；内置单文件替换器不会覆盖包管理器所属文件。
不要把源码 ZIP 或另一架构产物替换到现有安装中。详细操作见 [在线更新](在线更新.md)。

构建依赖下载较慢时，可通过 `--index-url https://pypi.tuna.tsinghua.edu.cn/simple` 指定公开 HTTPS 镜像；
不接受带凭据的索引 URL。`--build-image` 可复用已缓存的构建环境，但必须含共享 Python 且 glibc 不高于 2.31，脚本会验证。

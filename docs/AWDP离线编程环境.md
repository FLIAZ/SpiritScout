# AWDP 离线编程环境

代码扫描器不能替代编译器、运行时和项目依赖。准备阶段联网安装工具，比赛前在灵枢实际
`sandbox_exec` 中切断网络验收。宿主终端的版本输出不足以证明 Agent 能构建源码。

## 当前源码部署的准备入口

Fedora 44 使用发行版包补齐 C/C++、Python、Node.js、Java/Maven、Go、PHP/Composer、
Rust/Cargo、.NET/Mono、Ruby/Bundler、Perl、Lua/LuaRocks 和 SQLite CLI：

```bash
bash scripts/install_awdp_toolchains.sh --plan
sudo bash scripts/install_awdp_toolchains.sh --install
.venv/bin/python scripts/prepare_awdp_portable.py --install
```

第一条只显示 DNF 事务，`--assumeno` 拒绝事务的退出码不表示发生安装。脚本仅支持 Fedora 44，
其他发行版应按仓库实际包名准备依赖；它不添加第三方仓库或升级全系统。必要的依赖更新会列在事务中。
第二条需要在本机终端输入 sudo 密码。第三条由普通用户执行，安装固定版本的 TypeScript、Gradle，
并缓存 Maven 的基础编译/资源插件；不改变现有用户 Maven 设置，不缓存私有仓库凭据。

TypeScript 使用 npm 包锁及完整性校验；Gradle 使用官方发行包并校验固定 SHA-256。
工具装入 `.venv/share/awdp-toolchains/`，入口在 `.venv/bin/`，复用现有沙箱的只读 venv 挂载。
不依赖 `~/.local`、fnm/nvm、`~/.cargo` 或 `/usr/local`，也不开放整个主目录。
官方说明：[TypeScript 安装](https://www.typescriptlang.org/download/)、
[Gradle 与 JDK 的兼容矩阵](https://docs.gradle.org/current/userguide/compatibility.html)。

这套系统包安装只作用于当前源码部署机；Docker/WSL 必须在各自执行环境安装、验收。
现有轻量 Docker 的 `INSTALL_AUDIT=1` 主要安装扫描器，不代表包含上述全部工具链。

## 真实断网验收

```bash
.venv/bin/python scripts/verify_awdp_environment.py --output /tmp/awdp-environment.json
# 单独复测补齐的组件；--workspace 必须是新空目录
.venv/bin/python scripts/verify_awdp_environment.py --only php maven \
  --workspace /tmp/awdp-build-check --output /tmp/awdp-build-check.json
```

检查使用真实轻量 AWDP 沙箱和独立网络命名空间，验证与宿主网络命名空间不同。
依次编译或运行 18 组微型项目，断网使用 npm、Composer、Cargo、Gradle、Maven、NuGet 等；
检查退出码及最终程序输出，不仅检查命令存在。报告包含命令路径、版本、构建步骤、错误和命名空间，
任一缺失/失败会返回非零；不调用本地或云端模型。Python 检查还包含 venv、pip 及基础标准库，
PHP 检查包括 mbstring、DOM、PDO SQLite/MySQL、GD、ZIP、OpenSSL 和 fileinfo。

Maven 基础缓存位于 `.venv/share/awdp-toolchains/maven-repository/`。验收先将其复制到本题可写目录，
再用 `mvn --offline -Dmaven.repo.local=...` 构建。准备和验收都使用同一份无凭据 settings，
避免宿主镜像仓库 ID 与沙箱的仓库 ID 不同，导致“文件已经下载但离线仍无法解析”。
此缓存只覆盖指定版本的编译/资源插件；它不是任意 Maven 项目的完整仓库。

## 每道题还需准备的内容

| 项目 | 比赛前随项目准备并实际断网验证 |
| --- | --- |
| Python | 对应 Python/系统架构的 wheels、固定依赖清单；在题目 venv 用 `pip --no-index --find-links` 安装，不能改灵枢控制面的只读 venv |
| Node/TypeScript | package lock、完整 npm/pnpm/yarn 缓存或适配目标平台的 node_modules；`npm ci --offline` 仍要求依赖已缓存 |
| PHP | composer.lock、vendor、所需 PHP 扩展；Laravel 等框架不随 Composer 自动离线可用 |
| Java | 项目使用的 JDK、Gradle wrapper 分发包、插件和 Maven/Gradle 仓库缓存；仅下载 JAR 不一定足够 |
| Go/Rust | go.mod/go.sum 与 vendor 或模块缓存；Cargo.lock、vendor 和相应配置，禁止自动在线切换工具链 |
| .NET | global.json 指定 SDK、目标框架 targeting pack、NuGet 包缓存；新 SDK 不等于安装了所有旧运行时 |
| Ruby/Perl/Lua | Bundler 的 vendor/cache、CPAN/LuaRocks 依赖和本地 C 扩展构建依赖 |
| C/C++ | 对应架构、libc、头文件、链接库和 CMake/Make 依赖；当前 x86_64 本机构建不能证明 i386/ARM 或其他发行版兼容 |

数据库、Redis、Web 服务配置及题目指定旧版 Python/PHP/JDK 也须匹配。不要为获得“通过”而改题目
依赖版本、替换业务逻辑或跳过回归。缓存应作为题目材料放入沙箱可见目录，只带构建所需文件，
不携带 npm token、settings.xml 密码、SSH 密钥等宿主凭据。

工具链验收通过仅说明这些微型项目可以离线运行。最终仍需对实际附件执行原构建命令、合法功能测试和
补丁回归；提交给赛事的 tar.gz/patch.sh 应依据赛题运行环境构建，不能假定评测机也有本机全部工具。

#!/usr/bin/env python3
"""在真实轻量 AWDP 沙箱断网编译微型项目，不调用模型或执行附件。"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

MARKER = "AWDP_TOOLCHAIN_OK"
VERSIONS = {
    "python3": ["--version"], "node": ["--version"], "npm": ["--version"],
    "tsc": ["--version"], "php": ["--version"], "composer": ["--version"],
    "java": ["-version"], "javac": ["-version"], "jar": ["--version"],
    "mvn": ["--version"], "gradle": ["--version"], "go": ["version"],
    "gcc": ["--version"], "g++": ["--version"], "make": ["--version"],
    "cmake": ["--version"], "ninja": ["--version"], "pkg-config": ["--version"],
    "rustc": ["--version"], "cargo": ["--version"], "ruby": ["--version"],
    "bundle": ["--version"], "dotnet": ["--list-sdks"], "mono": ["--version"],
    "mcs": ["--version"], "perl": ["-e", "print $^V"], "lua": ["-v"],
    "luarocks": ["--version"], "sqlite3": ["--version"],
}
JAVA = f'public class Main {{ public static void main(String[] a) {{ System.out.println("{MARKER}"); }} }}\n'
C_SOURCE = f'#include <stdio.h>\nint main(void) {{ puts("{MARKER}"); return 0; }}\n'


def suites() -> dict[str, dict]:
    def suite(tools, files, commands, **extra):
        return {"tools": tools.split(), "files": files, "commands": commands, **extra}

    return {
        "python": suite("python3", {"main.py": f'import ssl, sqlite3, zlib\nprint("{MARKER}")\n'}, [
            ["python3", "-m", "pip", "--version"],
            ["python3", "-m", "venv", "--without-pip", "app-venv"],
            ["./app-venv/bin/python", "main.py"],
        ]),
        "javascript": suite("node npm", {
            "main.js": f'console.log("{MARKER}");\n',
            "package.json": '{"name":"awdp-probe","version":"1.0.0","private":true}',
            "package-lock.json": '{"name":"awdp-probe","version":"1.0.0","lockfileVersion":3,"packages":{"":{"name":"awdp-probe","version":"1.0.0"}}}',
        }, [["npm", "ci", "--offline", "--ignore-scripts", "--no-audit", "--no-fund"],
            ["node", "--check", "main.js"], ["node", "main.js"]]),
        "typescript": suite("node tsc", {"main.ts": f'const value: string = "{MARKER}"; console.log(value);\n'}, [
            ["tsc", "--strict", "--target", "ES2020", "--outDir", "dist", "main.ts"],
            ["node", "dist/main.js"],
        ]),
        "c": suite("gcc", {"main.c": C_SOURCE}, [
            ["gcc", "-std=c11", "-Wall", "-Wextra", "main.c", "-o", "app"], ["./app"],
        ]),
        "cpp": suite("g++", {"main.cpp": f'#include <iostream>\n#include <vector>\nint main() {{ std::vector<int> v{{1}}; if(v.size()!=1) return 1; std::cout << "{MARKER}\\n"; }}\n'}, [
            ["g++", "-std=c++17", "-Wall", "-Wextra", "main.cpp", "-o", "app"], ["./app"],
        ]),
        "native_build": suite("make cmake ninja pkg-config", {
            "main.c": C_SOURCE,
            "CMakeLists.txt": "cmake_minimum_required(VERSION 3.16)\nproject(awdp_probe C)\nadd_executable(app main.c)\n",
            "Makefile": "app_make: main.c\n\t$(CC) main.c -o app_make\n",
        }, [["make"], ["./app_make"], ["cmake", "-S", ".", "-B", "build", "-G", "Ninja"],
            ["cmake", "--build", "build", "--parallel", "1"], ["./build/app"]]),
        "java": suite("java javac jar", {"Main.java": JAVA}, [
            ["javac", "--release", "17", "Main.java"],
            ["jar", "--create", "--file", "app.jar", "--main-class", "Main", "Main.class"],
            ["java", "-jar", "app.jar"],
        ]),
        "maven": suite("mvn java", {
            "src/main/java/Main.java": JAVA,
            "settings.xml": '<settings xmlns="http://maven.apache.org/SETTINGS/1.2.0"/>',
            "pom.xml": '<project xmlns="http://maven.apache.org/POM/4.0.0"><modelVersion>4.0.0</modelVersion><groupId>local.awdp</groupId><artifactId>probe</artifactId><version>1.0</version><properties><maven.compiler.release>17</maven.compiler.release></properties><build><plugins><plugin><groupId>org.apache.maven.plugins</groupId><artifactId>maven-compiler-plugin</artifactId><version>3.14.1</version></plugin><plugin><groupId>org.apache.maven.plugins</groupId><artifactId>maven-resources-plugin</artifactId><version>3.3.1</version></plugin></plugins></build></project>',
        }, [["mvn", "--offline", "--batch-mode", "--settings", "settings.xml", "--global-settings", "settings.xml", "-Dmaven.repo.local=maven-repository", "compile"],
            ["java", "-cp", "target/classes", "Main"]], cache="maven-repository"),
        "gradle": suite("gradle java", {
            "src/main/java/Main.java": JAVA,
            "settings.gradle": "rootProject.name = 'awdp-probe'\n",
            "build.gradle": "plugins { id 'application' }\napplication { mainClass = 'Main' }\n",
        }, [["gradle", "--offline", "--no-daemon", "--max-workers=1", "--console=plain", "-q", "run"]]),
        "go": suite("go", {
            "main.go": f'package main\nimport "fmt"\nfunc main() {{ fmt.Println("{MARKER}") }}\n',
            "go.mod": "module example.invalid/awdp-probe\n\ngo 1.20\n",
        }, [["go", "build", "-p=1", "-o", "app", "."], ["./app"]]),
        "php": suite("php composer", {
            "composer.json": '{"name":"local/awdp-probe","require":{},"autoload":{"psr-4":{"Probe\\\\":"src/"}}}',
            "src/Message.php": f'<?php namespace Probe; class Message {{ public static function text() {{ return "{MARKER}"; }} }}\n',
            "main.php": '<?php\nrequire __DIR__ . "/vendor/autoload.php";\nforeach (["mbstring", "dom", "pdo_sqlite", "pdo_mysql", "gd", "zip", "openssl", "fileinfo"] as $ext) { if (!extension_loaded($ext)) { throw new Exception("Missing extension: " . $ext); } }\necho Probe\\Message::text(), "\\n";\n',
        }, [["php", "-l", "main.php"], ["composer", "dump-autoload", "--no-plugins", "--no-scripts", "--no-interaction"],
            ["php", "main.php"]]),
        "rust": suite("rustc cargo", {
            "Cargo.toml": '[package]\nname="awdp_probe"\nversion="0.1.0"\nedition="2021"\n',
            "src/main.rs": f'fn main() {{ println!("{MARKER}"); }}\n',
        }, [["cargo", "run", "--offline", "--quiet", "--jobs", "1"]]),
        "dotnet": suite("dotnet", {
            "probe.csproj": '<Project Sdk="Microsoft.NET.Sdk"><PropertyGroup><OutputType>Exe</OutputType><TargetFramework>net__SDK_MAJOR__.0</TargetFramework></PropertyGroup></Project>',
            "Program.cs": f'System.Console.WriteLine("{MARKER}");\n',
            "NuGet.Config": '<configuration><packageSources><clear /></packageSources></configuration>',
        }, [["dotnet", "restore", "--configfile", "NuGet.Config", "--nologo"],
            ["dotnet", "build", "--no-restore", "--nologo", "-m:1", "-p:UseSharedCompilation=false"],
            ["dotnet", "run", "--no-build", "--no-restore"]]),
        "mono": suite("mono mcs", {"Main.cs": f'class MainClass {{ static void Main() {{ System.Console.WriteLine("{MARKER}"); }} }}\n'}, [
            ["mcs", "-out:app.exe", "Main.cs"], ["mono", "app.exe"],
        ]),
        "ruby": suite("ruby bundle", {"Gemfile": "# No external gems.\n", "main.rb": f'require "json"\nputs JSON.parse(\'{{"value":"{MARKER}"}}\')["value"]\n'}, [
            ["bundle", "lock", "--local"], ["bundle", "exec", "ruby", "main.rb"],
        ]),
        "perl": suite("perl", {"main.pl": f'use strict; use warnings; use JSON::PP; print decode_json(\'{{"value":"{MARKER}"}}\')->{{value}}, "\\n";\n'}, [
            ["perl", "-c", "main.pl"], ["perl", "main.pl"],
        ]),
        "lua": suite("lua luarocks", {"main.lua": f'assert(utf8.len("中文") == 2)\nprint("{MARKER}")\n'}, [["lua", "main.lua"]]),
        "sqlite": suite("sqlite3", {}, [["sqlite3", ":memory:", f"CREATE TABLE probe(value TEXT); INSERT INTO probe VALUES('{MARKER}'); SELECT value FROM probe;"]]),
    }


def run_command(argv: list[str], *, timeout: float = 90) -> dict:
    started = time.monotonic()
    try:
        result = subprocess.run(argv, text=True, capture_output=True, timeout=timeout, check=False)
        return {"argv": argv, "returncode": result.returncode,
                "stdout": result.stdout[-6000:], "stderr": result.stderr[-6000:],
                "seconds": round(time.monotonic() - started, 2)}
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"argv": argv, "returncode": None, "error": str(exc)}


def probe(name: str) -> dict:
    specification = suites()[name]
    report = {"name": name, "status": "failed", "tools": {}, "steps": [],
              "network_namespace": os.readlink("/proc/self/ns/net")}
    os.environ.update({
        "GOTOOLCHAIN": "local", "GOPROXY": "off", "GOSUMDB": "off", "GOMAXPROCS": "2",
        "CARGO_NET_OFFLINE": "true", "COMPOSER_DISABLE_NETWORK": "1",
        "DOTNET_CLI_TELEMETRY_OPTOUT": "1", "DOTNET_SKIP_FIRST_TIME_EXPERIENCE": "1",
        "DOTNET_NOLOGO": "1", "DOTNET_GENERATE_ASPNET_CERTIFICATE": "false",
        "GRADLE_OPTS": "-Dorg.gradle.jvmargs=-Xmx256m -Dorg.gradle.daemon=false",
        "MAVEN_OPTS": "-Xmx256m", "npm_config_offline": "true",
    })
    for tool in specification["tools"]:
        path = shutil.which(tool)
        report["tools"][tool] = {"path": path}
        if path:
            report["tools"][tool]["version"] = run_command([tool, *VERSIONS[tool]], timeout=30)
    missing = [tool for tool, value in report["tools"].items() if not value["path"]]
    if missing:
        report.update(status="missing", missing=missing)
        return report
    major = ""
    if name == "dotnet":
        version = report["tools"]["dotnet"]["version"].get("stdout", "")
        majors = [int(line.split(".")[0]) for line in version.splitlines() if line[:1].isdigit()]
        if not majors:
            report["error"] = "dotnet 存在，但未找到 SDK"
            return report
        major = str(max(majors))
    for filename, content in specification["files"].items():
        destination = Path(filename)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(content.replace("__SDK_MAJOR__", major), encoding="utf-8")
    if cache := specification.get("cache"):
        seed = Path(sys.prefix) / "share/awdp-toolchains" / cache
        if seed.is_dir():
            shutil.copytree(seed, cache)
            report["seed_cache"] = str(seed)
    for argv in specification["commands"]:
        step = run_command(argv)
        report["steps"].append(step)
        if step["returncode"] != 0:
            report["error"] = "断网构建或执行失败；查看最后一步输出"
            return report
    # 退出码为零不足以证明程序真的运行；最后一步还必须输出预期结果。
    if MARKER not in report["steps"][-1].get("stdout", "").splitlines():
        report["error"] = "执行未输出预期验收标记"
        return report
    report["status"] = "passed"
    return report


async def verify(root: Path, selected: list[str]) -> dict:
    from spiritscout.core.models import RunMode
    from spiritscout.tools.sandbox import offline_execution, sandbox_exec

    source = Path(__file__).read_text(encoding="utf-8")
    host_network = os.readlink("/proc/self/ns/net")
    reports = []
    token = offline_execution.set(True)
    try:
        for name in selected:
            code = f"import sys\nsys.argv = ['probe', '--probe', {name!r}]\nexec({source!r})"
            result = await sandbox_exec(code, language="python", workspace_root=str(root),
                                        workspace_scope=name, timeout=240,
                                        _run_mode=RunMode.AWDP, _lightweight_local=True)
            entry = {"name": name, "status": "failed", "error": result.error or result.stderr}
            if result.ok and result.returncode == 0:
                output = Path(result.parsed["output_file"]).read_text(encoding="utf-8")
                try:
                    entry = json.loads(output)
                    if not entry.get("network_namespace") or entry["network_namespace"] == host_network:
                        entry.update(status="failed", error="未确认网络命名空间隔离")
                except (ValueError, TypeError):
                    entry["error"] = "沙箱没有返回有效验收 JSON"
            reports.append(entry)
            print(f'{name}: {entry["status"]}', file=sys.stderr, flush=True)
    finally:
        offline_execution.reset(token)
    return {
        "ok": all(item["status"] == "passed" for item in reports),
        "execution": "real_lightweight_awdp_sandbox", "network": "unshare-net",
        "model_calls": 0, "results": reports,
        "qualification": "只证明所列微型项目及工具链可离线运行；题目依赖、旧版本、数据库服务和目标架构须单独验收",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--only", nargs="+", choices=list(suites()))
    parser.add_argument("--output", type=Path, help="保存完整 JSON 报告")
    parser.add_argument("--workspace", type=Path, help="保留编译工作区及完整日志，须使用新空目录")
    parser.add_argument("--probe", choices=list(suites()), help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.probe:
        print(json.dumps(probe(args.probe), ensure_ascii=False))
        return 0
    if args.workspace:
        root = args.workspace.resolve()
        if root.exists() and any(root.iterdir()):
            parser.error("--workspace 必须是新空目录，以免已有缓存掩盖缺失依赖")
        root.mkdir(parents=True, exist_ok=True)
        report = asyncio.run(verify(root, args.only or list(suites())))
    else:
        with tempfile.TemporaryDirectory(prefix="spiritscout-awdp-env-") as temp:
            report = asyncio.run(verify(Path(temp), args.only or list(suites())))
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"ok": report["ok"], "results": {r["name"]: r["status"] for r in report["results"]},
                      "report": str(args.output) if args.output else None}, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

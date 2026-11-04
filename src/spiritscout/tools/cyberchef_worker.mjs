// CyberChef runs in a fresh, offline bubblewrap process, never in the web server.
import fs from "node:fs";
import { pathToFileURL } from "node:url";

function suggestOperations(requested, ops) {
    // 报错时把最相近的已安装操作名带回给模型，止住凭记忆猜名的循环。
    const clean = (s) => s.toLowerCase().replace(/[^a-z0-9]/g, "");
    const want = clean(requested);
    const distance = (a, b) => {
        const row = Array.from({ length: b.length + 1 }, (_, i) => i);
        for (let i = 1; i <= a.length; i++) {
            let prev = row[0];
            row[0] = i;
            for (let j = 1; j <= b.length; j++) {
                const cur = row[j];
                row[j] = Math.min(row[j] + 1, row[j - 1] + 1,
                    prev + (a[i - 1] === b[j - 1] ? 0 : 1));
                prev = cur;
            }
        }
        return row[b.length];
    };
    // 两组并集：编辑距离相近的名字 + 与所猜词共享词元（hex/base64/dump…）的
    // 名字——格式词往往比方向词（decode/encode）更能指对操作（FromHex 等）。
    const tokens = requested.split(/[_\s\-]+/).flatMap((part) =>
        part.match(/[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+/g) || [])
        .filter((t) => t.length >= 3).map((t) => t.toLowerCase());
    const byDistance = [...ops].sort((a, b) =>
        distance(want, clean(a)) - distance(want, clean(b)));
    const byToken = [...ops].sort((a, b) => {
        const shared = (name) => {
            const lower = clean(name);
            return tokens.reduce((sum, t) => sum + (lower.includes(t) ? t.length : 0), 0);
        };
        return shared(b) - shared(a);
    });
    const merged = [];
    for (const name of [...byDistance.slice(0, 3), ...byToken.slice(0, 3)]) {
        if (!merged.includes(name)) merged.push(name);
    }
    return merged.slice(0, 5);
}

try {
    if (Number(process.versions.node.split(".")[0]) < 24 ||
        Number(process.versions.node.split(".")[0]) > 26) {
        throw new Error("CyberChef 11.5.0 requires Node.js 24–26");
    }
    const request = JSON.parse(fs.readFileSync("/job/request.json", "utf8"));
    const { default: chef } = await import(pathToFileURL(
        "/tool/runtime/node_modules/cyberchef/src/node/index.mjs"));
    const version = JSON.parse(fs.readFileSync(
        "/tool/runtime/node_modules/cyberchef/package.json", "utf8")).version;
    if (request.mode === "operations") {
        const matches = chef.help(request.query) || [];
        const page = matches.slice(request.offset, request.offset + request.limit);
        const operations = page.map(op => ({
            name: op.name,
            description: op.description,
            inputType: op.inputType,
            outputType: op.outputType,
            flowControl: op.flowControl,
            args: op.args,
        }));
        fs.writeFileSync("/output/catalog.json", JSON.stringify({
            version, operations, total: matches.length,
            next_offset: request.offset + page.length,
            has_more: request.offset + page.length < matches.length,
            notice: "Node API excludes some browser operations and recipe flow control. Execution errors are authoritative.",
        }));
    } else {
        const input = fs.readFileSync("/job/input.bin");
        // Buffer preserves bytes; never trim, normalise, or guess the input text.
        let dish;
        try {
            dish = await chef.bake(input, request.recipe);
        } catch (bakeError) {
            const unknown = String(bakeError?.message || bakeError)
                .match(/Couldn't find an operation with name '(.+?)'/);
            if (unknown && request.suggest_unknown_ops) {
                const ops = Object.keys(chef)
                    .filter((k) => typeof chef[k] === "function" && chef[k].opName)
                    .map((k) => chef[k].opName);
                throw new Error(
                    `${String(bakeError?.message || bakeError)} Closest installed operations: ` +
                    `${suggestOperations(unknown[1], ops).map((n) => `'${n}'`).join(", ")}. ` +
                    `Query cyberchef_operations for exact names and positional args.`);
            }
            throw bakeError;
        }
        const bytes = Buffer.from(dish.get("byteArray"));
        if (bytes.length > request.max_output_bytes) {
            throw new Error("CyberChef output exceeds max_output_bytes; no partial result saved");
        }
        fs.writeFileSync("/output/result.bin", bytes);
        fs.writeFileSync("/output/engine.json", JSON.stringify({version, dish_type: dish.type}));
    }
} catch (error) {
    console.error(String(error?.stack || error));
    process.exitCode = 1;
}

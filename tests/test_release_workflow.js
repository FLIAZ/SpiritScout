"use strict";
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../.github/workflows/release.yml'), 'utf8');
const gate = source.match(/    if: >-\n((?:      .*\n)+)/)[1].trim();
function eligible(event_name, {ref = 'refs/heads/main', conclusion = 'success', event = 'push', repository = 'owner/repo'} = {}) {
  return vm.runInNewContext(gate, {github: {event_name, ref, repository: 'owner/repo', event: {workflow_run: {
    conclusion, event, head_repository: {full_name: repository}
  }}}});
}
assert.equal(eligible('workflow_dispatch'), true);
assert.equal(eligible('workflow_dispatch', {ref: 'refs/heads/feature'}), false);
assert.equal(eligible('workflow_run'), true);
for (const changes of [{conclusion: 'failure'}, {conclusion: 'cancelled'}, {event: 'pull_request'}, {repository: 'fork/repo'}]) {
  assert.equal(eligible('workflow_run', changes), false);
}
const script = source.match(/          script: \|\n([\s\S]*?)\n  windows:/)[1].split('\n').map(line => line.slice(12)).join('\n');
const missing = () => Object.assign(new Error('Not found'), {status: 404});
async function check({release, releaseError, tag, tagError} = {}) {
  const outputs = {}, calls = [];
  const context = vm.createContext({process: {env: {TAG: 'v0.22.0'}}, context: {repo: {owner: 'owner', repo: 'repo'}},
    core: {info() {}, setOutput(name, value) {outputs[name] = value;}},
    github: {rest: {
      repos: {async getReleaseByTag() {calls.push('release'); if (releaseError) throw releaseError; if (!release) throw missing(); return {data: release};}},
      git: {async getRef() {calls.push('tag'); if (tagError) throw tagError; if (!tag) throw missing(); return {};}}
    }}
  });
  await vm.runInContext(`(async () => {${script}})()`, context);
  return {outputs, calls};
}
(async () => {
  assert.deepEqual(await check(), {outputs: {release: 'true'}, calls: ['release', 'tag']});
  assert.deepEqual(await check({release: {draft: false}}), {outputs: {release: 'false'}, calls: ['release']});
  await assert.rejects(check({release: {draft: true}}), /draft/);
  await assert.rejects(check({tag: true}), /without a published release/);
  for (const status of [401, 403, 500]) {
    const error = Object.assign(new Error('API failure'), {status});
    await assert.rejects(check({releaseError: error}), /API failure/);
    await assert.rejects(check({tagError: error}), /API failure/);
  }
  // The same tested commit must feed every build and the published tag.
  assert.match(source, /sha: \$\{\{ github.event.workflow_run.head_sha \|\| github.sha \}\}/);
  assert.equal((source.match(/ref: \$\{\{ needs.prepare.outputs.sha \}\}/g) || []).length, 2);
  assert.match(source, /--target "\$RELEASE_SHA"/);
  assert.equal((source.match(/if: needs.prepare.outputs.release == 'true'/g) || []).length, 2);
  console.log('release: trigger isolation, duplicate skip, partial releases, API errors and commit identity passed');
})().catch(error => {console.error(error); process.exitCode = 1;});

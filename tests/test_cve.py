"""验证 CVE 原文、拒绝记录、分页、失败/过期、离线边界和同步水位。"""
from __future__ import annotations

import asyncio
import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

import httpx

from spiritscout.config.settings import CVEConfig
from spiritscout.core.models import RunMode
from spiritscout.cve.maintenance import (
    bootstrap,
    import_records,
    index_templates,
    sync_cvelist,
    sync_nvd,
)
from spiritscout.cve.service import CVEService
from spiritscout.tools.cve import cve_lookup


def record(number=44228, modified='2026-10-01T00:00:00Z', state='PUBLISHED'):
    return {'cveMetadata': {'cveId': f'CVE-2021-{number}', 'dateUpdated': modified, 'state': state},
            'containers': {'cna': {'title': 'Log4j', 'descriptions': [{'lang': 'en', 'value': 'Apache Log4j flaw'}],
                'affected': [{'vendor': 'Apache', 'product': 'Log4j', 'versions': [
                    {'version': '2.0', 'lessThan': '2.15.0', 'versionType': 'semver', 'status': 'affected'}]}]},
                'adp': [{'providerMetadata': {'shortName': 'fixture'}, 'references': [{'url': 'https://example.org'}]}]}}


def nvd_record(number=44228):
    return {'cve': {'id': f'CVE-2021-{number}', 'lastModified': '2026-11-01T00:00:00.000',
                    'vulnStatus': 'Analyzed', 'descriptions': [{'lang': 'en', 'value': 'Apache Log4j'}],
                    'configurations': [{'operator': 'AND', 'nodes': [{'cpeMatch': [
                        {'criteria': 'cpe:2.3:a:apache:log4j:*:*:*:*:*:*:*:*',
                         'versionStartIncluding': '2.0', 'versionEndExcluding': '2.15.0', 'vulnerable': True}]}]}]}}


class CVETests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.cfg = CVEConfig(sqlite_path=str(self.root / 'cve.db'))
        self.service = CVEService(self.cfg)
        self.store = self.service.store

    def test_same_cve_multiple_sources_and_original_conditions(self):
        self.store.put_many([('cvelist', record()), ('nvd', nvd_record())])
        self.assertEqual(self.store.status()['cves'], 1)
        result = self.store.search('Apache Log4j')
        self.assertEqual(result['total'], 1)
        detail = self.store.detail('cve-2021-44228')
        self.assertEqual(len(detail['records']), 2)
        self.assertEqual(detail['records'][1]['record'], nvd_record())
        self.assertEqual(detail['records'][0]['record']['containers'], record()['containers'])

    def test_old_snapshot_cannot_resurrect_rejected_record(self):
        self.store.put_many([('cvelist', record(modified='2026-11-01T00:00:00Z', state='REJECTED'))])
        self.store.put_many([('cvelist', record())])
        self.assertEqual(self.store.detail('CVE-2021-44228')['records'][0]['state'], 'REJECTED')
        self.assertIn('REJECTED', self.store.search('Log4j')['items'][0]['states'])

    def test_keyword_pagination_and_fts_escaping(self):
        self.store.put_many([('cvelist', record(n)) for n in range(10000, 10003)])
        first = self.store.search('"Apache" OR', limit=1)
        self.assertEqual(first['total'], 0)  # OR 被当普通关键词，而非 SQL/FTS 操作符。
        page = self.store.search('Apache', limit=2)
        second = self.store.search('Apache', offset=page['next_offset'], limit=2)
        self.assertEqual(page['total'], 3)
        self.assertEqual(len(second['items']), 1)
        self.assertIsNone(second['next_offset'])

    def test_nested_official_zip_import_and_invalid_count(self):
        inner = io.BytesIO()
        with zipfile.ZipFile(inner, 'w') as archive:
            archive.writestr('cves/2021/CVE-2021-44228.json', json.dumps(record()))
            archive.writestr('../../outside.json', '{broken')
        outer = self.root / 'snapshot.zip'
        with zipfile.ZipFile(outer, 'w') as archive:
            archive.writestr('all.zip', inner.getvalue())
        self.assertEqual(import_records(self.store, outer), {'imported': 1, 'invalid': 1})
        self.assertFalse((self.root.parent / 'outside.json').exists())

    def test_template_association_all_protocols_and_removal(self):
        directory = self.root / 'templates'
        (directory / 'network').mkdir(parents=True)
        file = directory / 'network' / 'unusual-name.yaml'
        file.write_text('id: fixture\ninfo:\n  name: fixture\n  severity: high\n  classification:\n    cve-id: [CVE-2021-44228, CVE-2021-45046]\ntcp: []\n')
        result = index_templates(self.store, directory)
        self.assertEqual(result['indexed'], 1)
        self.assertEqual(self.store.detail('CVE-2021-44228')['local_templates'][0]['protocol'], 'tcp')
        file.unlink()
        index_templates(self.store, directory)
        self.assertFalse(self.store.detail('CVE-2021-44228')['local_templates'])

    def test_nvd_search_keeps_pagination_and_raw_ranges(self):
        def handler(req):
            self.assertEqual(req.url.params['startIndex'], '20')
            return httpx.Response(200, json={'startIndex': 20, 'totalResults': 42,
                                            'vulnerabilities': [nvd_record()]})
        self.service.transport = httpx.MockTransport(handler)
        with patch('spiritscout.cve.service._NVD_LAST', 0):
            result = self.service.search('Log4j', source='nvd', offset=20)
        self.assertEqual(result['next_offset'], 21)
        self.assertEqual(result['items'][0]['configurations'], nvd_record()['cve']['configurations'])

    def test_error_not_empty_and_stale_cache_is_labelled(self):
        self.store.cache_put('x', {'data': ['old']})
        with self.store.connect() as db:
            db.execute("UPDATE cache SET fetched=1")
        self.service.transport = httpx.MockTransport(lambda req: httpx.Response(403))
        result = self.service.fetch('epss', key='x', online=True)
        self.assertEqual(result['status'], 'error')
        self.assertTrue(result['stale'])
        self.assertEqual(result['data'], {'data': ['old']})
        self.assertEqual(self.service.fetch('epss', key='missing', online=False)['status'], 'offline')

    def test_osv_version_query_and_page_token(self):
        def handler(req):
            body = json.loads(req.content)
            self.assertEqual(body['version'], '2.14.1')
            self.assertEqual(body['package']['ecosystem'], 'Maven')
            self.assertEqual(body['page_token'], 'next')
            return httpx.Response(200, json={'vulns': [], 'next_page_token': 'more'})
        self.service.transport = httpx.MockTransport(handler)
        result = self.service.package('org.apache.logging.log4j:log4j-core', 'Maven', '2.14.1', page_token='next')
        self.assertEqual(result['data']['next_page_token'], 'more')

    def test_sync_failed_page_does_not_advance_watermark(self):
        def request(*args, **kwargs):
            if kwargs['params']['startIndex'] == 0:
                return {'startIndex': 0, 'totalResults': 2, 'vulnerabilities': [nvd_record()]}
            raise ValueError('fixture source failure')
        with patch.object(self.service, 'request', side_effect=request):
            with self.assertRaises(ValueError):
                sync_nvd(self.service, '2026-11-01T00:00:00+00:00')
        self.assertIsNone(self.store.meta_get('nvd_watermark'))
        self.assertEqual(self.store.status()['cves'], 1)

    def test_sync_checkpoints_after_all_pages(self):
        with patch.object(self.service, 'request', return_value={
                'startIndex': 0, 'totalResults': 1, 'vulnerabilities': [nvd_record()]}):
            result = sync_nvd(self.service, '2026-11-01T00:00:00+00:00')
        self.assertIsNotNone(result['watermark'])

    def test_competition_forces_offline_and_keeps_local_record(self):
        self.store.put_many([('cvelist', record())])
        with patch.object(self.service, 'request', side_effect=AssertionError('must stay offline')):
            result = asyncio.run(cve_lookup(_cve=self.service, _run_mode=RunMode.COMPETITION,
                                            action='lookup', cve_id='CVE-2021-44228', refresh=True))
        self.assertTrue(result.ok)
        self.assertEqual(len(result.parsed['records']), 1)
        self.assertTrue(all(s['status'] == 'offline' for s in result.parsed['sources']))

    def test_offline_cached_lookup_does_not_forge_freshness(self):
        self.store.cache_put('cvelist:CVE-2021-44228', record())
        with self.store.connect() as db:
            db.execute('UPDATE cache SET fetched=1')
        result = self.service.lookup('CVE-2021-44228', online=False)
        self.assertEqual(result['records'][0]['fetched_at'], 1)
        self.assertTrue(result['sources'][0]['stale'])

    def test_lightweight_ctf_and_awdp_lookup_use_local_library_without_network(self):
        self.store.put_many([('cvelist', record())])
        for mode in (RunMode.CTF, RunMode.AWDP):
            with self.subTest(mode=mode), patch.object(self.service, 'request', side_effect=AssertionError('offline')):
                result = asyncio.run(cve_lookup(_cve=self.service, _run_mode=mode, _lightweight_local=True,
                    action='lookup', cve_id='CVE-2021-44228', refresh=True))
                self.assertTrue(result.ok)
                self.assertEqual(len(result.parsed['records']), 1)
                self.assertTrue(all(s['status'] == 'offline' for s in result.parsed['sources']))

    def test_normal_ctf_keeps_existing_online_capability(self):
        with patch.object(self.service, 'lookup', return_value={'records': [], 'sources': []}) as lookup:
            asyncio.run(cve_lookup(_cve=self.service, _run_mode=RunMode.CTF, _lightweight_local=False,
                action='lookup', cve_id='CVE-2021-44228'))
        self.assertTrue(lookup.call_args.kwargs['online'])

    def test_large_result_is_saved_with_valid_json_preview(self):
        data = record()
        data['containers']['cna']['descriptions'][0]['value'] = 'x' * 30000
        self.store.put_many([('cvelist', data)])
        result = asyncio.run(cve_lookup(_cve=self.service, _run_mode=RunMode.COMPETITION,
            workspace_root=str(self.root), action='lookup', cve_id='CVE-2021-44228'))
        self.assertTrue(result.ok)
        out = json.loads(result.stdout)
        file = self.root / out['full_record_file']
        self.assertEqual(json.loads(file.read_text())['records'][0]['record'], data)
        self.assertTrue(out['preview_only'])

    def test_cvelist_delta_gap_does_not_advance(self):
        self.store.meta_put('cvelist_watermark', '2026-01-01T00:00:00+00:00')
        with patch.object(self.service, 'request', return_value={'changes': [
                {'fetchTime': '2026-11-01T00:00:00Z', 'new': []}]}):
            with self.assertRaisesRegex(ValueError, 'bootstrap'):
                sync_cvelist(self.service)
        self.assertEqual(self.store.meta_get('cvelist_watermark'), '2026-01-01T00:00:00+00:00')

    def test_cvelist_delta_deduplicates_and_preserves_updated_record(self):
        self.store.meta_put('cvelist_snapshot', {'coverage_until': '2026-11-01T00:00:00+00:00'})
        def request(source, **kwargs):
            if source == 'cvelist_delta':
                return {'changes': [
                    {'fetchTime': '2026-11-01T03:00:00Z', 'new': [{'cveId': 'CVE-2021-44228'}],
                     'updated': [{'cveId': 'CVE-2021-44228'}]},
                    {'fetchTime': '2026-10-31T00:00:00Z', 'new': []}]}
            return record(modified='2026-11-01T02:00:00Z', state='REJECTED')
        with patch.object(self.service, 'request', side_effect=request) as request_mock:
            result = sync_cvelist(self.service)
        self.assertEqual(result['imported'], 1)
        self.assertEqual(request_mock.call_count, 2)
        self.assertEqual(self.store.detail('CVE-2021-44228')['records'][0]['state'], 'REJECTED')

    def test_cve_intelligence_cannot_confirm_independent_verification(self):
        from spiritscout.agents.verifier import VerifierAgent
        raw = {'verdict': 'confirmed', 'harm': 'impact', 'rule_quote': 'fixture rule',
               'evidence': [{'execution_id': 'intel', 'quote': 'affected'}],
               'controls': [{'execution_id': 'control', 'quote': 'safe'}]}
        executions = {'intel': {'ok': True, 'tool': 'cve_lookup', 'text': 'affected'},
                      'control': {'ok': True, 'tool': 'http_probe', 'text': 'safe'}}
        result = VerifierAgent.validate(raw, executions, {'mission': 'fixture rule'})
        self.assertEqual(result['verdict'], 'inconclusive')

    def test_bootstrap_bad_checksum_preserves_existing_snapshot(self):
        directory = self.root / 'downloads'
        directory.mkdir()
        target = directory / 'cvelist-full.zip'
        target.write_bytes(b'previous-good-snapshot')
        def handler(req):
            if 'api.github.com' in str(req.url):
                return httpx.Response(200, json={'tag_name': 'cve_2026-11-05_0200Z',
                    'published_at': '2026-11-05T02:00:00Z', 'assets': [{
                        'name': '2026-11-05_all_CVEs.zip', 'size': 3,
                        'digest': 'sha256:' + '0' * 64,
                        'browser_download_url': 'https://example.org/fixture.zip'}]})
            return httpx.Response(200, content=b'bad')
        client = httpx.Client(transport=httpx.MockTransport(handler))
        with patch('spiritscout.cve.maintenance.httpx.Client', return_value=client):
            with self.assertRaisesRegex(ValueError, 'SHA256'):
                bootstrap(self.service, directory)
        self.assertEqual(target.read_bytes(), b'previous-good-snapshot')
        self.assertIsNone(self.store.meta_get('cvelist_snapshot'))

    def test_metadata_in_official_snapshot_is_not_a_broken_cve(self):
        path = self.root / 'metadata.zip'
        with zipfile.ZipFile(path, 'w') as archive:
            archive.writestr('cves/deltaLog.json', '[{}]')
            archive.writestr('cves/delta.json', '{}')
            archive.writestr('cves/CVE-2021-44228.json', json.dumps(record()))
        self.assertEqual(import_records(self.store, path), {'imported': 1, 'invalid': 0})

    def test_delta_uses_official_api_fallback_on_transport_failure(self):
        calls = []
        def handler(req):
            calls.append(req)
            if len(calls) == 1:
                raise httpx.ConnectError('fixture', request=req)
            self.assertEqual(req.headers['Accept'], 'application/vnd.github.raw+json')
            self.assertEqual(req.url.host, 'api.github.com')
            return httpx.Response(200, json=[{'fetchTime': '2026-11-01T00:00:00Z'}])
        self.service.transport = httpx.MockTransport(handler)
        with patch('spiritscout.cve.service.time.sleep'):
            result = self.service.request('cvelist_delta')
        self.assertEqual(len(result['changes']), 1)
        self.assertEqual(len(calls), 2)

    def test_broken_source_page_is_not_reported_as_no_vulnerabilities(self):
        self.service.transport = httpx.MockTransport(lambda req: httpx.Response(200, json={
            'startIndex': 99, 'totalResults': 100, 'vulnerabilities': []}))
        with patch('spiritscout.cve.service._NVD_LAST', 0):
            result = self.service.search('Log4j', source='nvd')
        self.assertEqual(result['source_status']['status'], 'error')
        self.assertIsNone(result['total'])

    def test_input_validation_and_disabled_tool(self):
        result = asyncio.run(cve_lookup(action='status'))
        self.assertFalse(result.ok)
        result = asyncio.run(cve_lookup(_cve=self.service, action='lookup', cve_id='../invalid'))
        self.assertFalse(result.ok)
        result = asyncio.run(cve_lookup(_cve=self.service, action='search', query='Apache', limit=True))
        self.assertFalse(result.ok)


if __name__ == '__main__':
    unittest.main()

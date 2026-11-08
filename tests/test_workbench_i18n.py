"""工作台中英切换：设置、批量缓存、鉴权和固定文案。"""
import json
import re
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from ethan.core.router import Settings
from ethan.workbench.api import Workbench
from ethan.workbench.translation import translate
from ethan.workbench.server import App
from tests.test_workbench import make_demo, NOON, TOKEN, OWNER
from ethan.workbench import auth

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / 'ethan/workbench/static'


class TranslationTest(unittest.TestCase):
    def setUp(self):
        self.db = SimpleNamespace(conn=sqlite3.connect(':memory:'))
        self.addCleanup(self.db.conn.close)
        self.router = Mock()
        self.router.complete.return_value.text = '["First", "Second"]'

    def test_order_duplicates_cache_and_skipped_strings(self):
        texts = ['第一句', '', '1234', '第二句', '第一句', 'abc']
        self.assertEqual(translate(self.db, self.router, texts)['texts'], ['First', '', '1234', 'Second', 'First', 'abc'])
        self.assertEqual(translate(self.db, self.router, ['第二句', '第一句'])['texts'], ['Second', 'First'])
        self.router.complete.assert_called_once()
        self.assertEqual(self.router.complete.call_args.kwargs['purpose'], 'translate')
        self.assertEqual(json.loads(self.router.complete.call_args.args[1][0]['content']), ['第一句', '第二句'])

    def test_validation_failure_and_retry(self):
        for texts in (None, '原文', [1], ['句子'] * 51, ['字' * 6001]):
            with self.assertRaises(ValueError):
                translate(self.db, self.router, texts)
        self.router.complete.return_value.text = '["错长度"]'
        self.assertFalse(translate(self.db, self.router, ['第一句', '第二句'])['ok'])
        self.assertEqual(self.db.conn.execute('SELECT count(*) FROM ui_translations').fetchone()[0], 0)
        self.router.complete.side_effect = RuntimeError('模型暂不可用')
        self.assertEqual(translate(self.db, self.router, ['原文'])['texts'], ['原文'])

    def test_empty_and_numbers_never_call_model(self):
        self.assertEqual(translate(self.db, self.router, ['', '123', '  ', '3.14', 'ID-123'])['texts'], ['', '123', '  ', '3.14', 'ID-123'])
        self.router.complete.assert_not_called()

    def test_quotes_code_and_identifiers_are_restored_exactly(self):
        self.router.complete.return_value.text = '["Remember __KEEP_0__, __KEEP_1__ and __KEEP_2__"]'
        original = '记住“她的原话”、`print(1)` 和 #123'
        self.assertEqual(translate(self.db, self.router, [original])['texts'], ['Remember “她的原话”, `print(1)` and #123'])
        prompt = self.router.complete.call_args.args[1][0]['content']
        self.assertNotIn('她的原话', prompt)
        self.assertNotIn('print(1)', prompt)
        self.router.complete.return_value.text = '["Missing the protected text"]'
        self.assertFalse(translate(self.db, self.router, ['另一句“不能变”'])['ok'])

    def test_cache_survives_reopen(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'cache.sqlite'
            db = SimpleNamespace(conn=sqlite3.connect(path))
            self.router.complete.return_value.text = '["Hello"]'
            translate(db, self.router, ['你好'])
            db.conn.close()
            db = SimpleNamespace(conn=sqlite3.connect(path))
            try:
                self.assertEqual(translate(db, None, ['你好'])['texts'], ['Hello'])
            finally:
                db.conn.close()


class LanguageSettingsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = make_demo(self.root)
        self.addCleanup(self.store.close)
        self.settings = Settings(self.root / 'settings.json')
        self.wb = Workbench(self.store, self.settings)

    def test_language_persists_and_rejects_unknown(self):
        self.assertEqual(self.wb.settings_page(NOON)['prefs']['ui_lang'], 'zh')
        for lang in ('en', 'zh'):
            self.assertTrue(self.wb.do_settings_set({'ui_lang': lang}, NOON.date())['ok'])
            other = Workbench(self.store, Settings(self.settings.path))
            self.assertEqual(other.settings_page(NOON)['prefs']['ui_lang'], lang)
            self.assertEqual(other.state(NOON)['ui_lang'], lang)
        self.assertFalse(self.wb.do_settings_set({'ui_lang': 'xx'}, NOON.date())['ok'])
        self.assertEqual(self.settings.get('ui_lang'), 'zh')

    def test_endpoint_requires_auth_and_validates(self):
        app = App(self.wb, TOKEN, self.root)
        body = json.dumps({'texts': ['123']}).encode()
        self.assertEqual(app.handle('POST', '/api/translate', {}, body)[0], 401)
        self.settings.set('telegram_owner_chat_id', OWNER)
        app = App(self.wb, TOKEN, self.root)
        headers = {'Authorization': 'Bearer ' + auth.make_session(app.key, OWNER)}
        status, _, response = app.handle('POST', '/api/translate', headers, body)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(response)['texts'], ['123'])
        self.assertEqual(app.handle('POST', '/api/translate', headers, b'{"texts": null}')[0], 400)


class FrontendLanguageTest(unittest.TestCase):
    def test_dictionary_keys_and_script_order(self):
        source = (STATIC / 'i18n.js').read_text()
        words = json.loads(source.split('const UI_WORDS = ', 1)[1].split(';\nlet uiLang', 1)[0])
        app = (STATIC / 'app.js').read_text()
        self.assertGreater(len(words), 400)
        self.assertEqual([key for key in words if key not in app], [])
        index = (STATIC / 'index.html').read_text()
        self.assertLess(index.index('/static/i18n.js'), index.index('/static/app.js'))
        for value in words.values():
            self.assertTrue(value.strip())

    def test_english_render_and_safe_fallback(self):
        node = shutil.which('node')
        if not node:
            self.skipTest('环境没有 Node，无法执行前端')
        # 用轻量 DOM 桩执行真实 render；接口内容翻译另有缓存和顺序测试。
        script = r'''
const fs = require('fs'), vm = require('vm');
const assert = require('assert');
const element = () => ({innerHTML:'', hidden:true, scrollTop:0, classList:{toggle(){}},
 querySelector(){return null}, querySelectorAll(){return []}, addEventListener(){}, getAttribute(){return null}});
const els = new Map();
const document = {documentElement:{lang:'zh'}, body:{className:''}, addEventListener(){},
 querySelector(key){if(!els.has(key))els.set(key,element());return els.get(key)}};
const context = vm.createContext({document, window:{scrollY:0,scrollTo(){},addEventListener(){}},
 console, queueMicrotask(){}, setTimeout(){}, clearTimeout(){}, setInterval(){}, navigator:{},
 localStorage:{getItem(){return null}}, Map, Set});
vm.runInContext(fs.readFileSync(process.argv[1]+'/i18n.js','utf8')+'\n'+
 fs.readFileSync(process.argv[1]+'/app.js','utf8').replace('boot();',''),context);
vm.runInContext(`uiLang='en'; S.state={day:'2026-11-08',weekday:7,celebrate:[],badges:{}};
if(t('今日')!=='Today'||t('不存在的词')!=='不存在的词') throw Error('查表错误');
for(const page of ['map','territory','me','relations','settings','cards','ach','brain','team']) {
 S.page=page; S.overlay=null; render();
}
if(!document.querySelector('#nav').innerHTML.includes('Today')) throw Error('导航没切换');
if(!uiText('<原文>',true).includes('&lt;原文&gt;')) throw Error('转义丢失');
uiLang='zh'; if(t('今日')!=='今日') throw Error('中文未还原');`,context);
'''
        with tempfile.TemporaryDirectory() as tmp:
            store = make_demo(Path(tmp))
            try:
                wb = Workbench(store, Settings(Path(tmp) / 'settings.json'))
                fixture = {"state": wb.state(NOON), "quests": wb.quests_page(NOON),
                           "season": wb.season_page(NOON), "map": wb.map_page(NOON),
                           "terr": wb.territory_page('body', NOON), "me": wb.me_page(),
                           "rel": wb.relations_page(), "settings": wb.settings_page(NOON),
                           "cards": wb.cards_page(), "ach": wb.ach_page(NOON),
                           "brain": wb.brain_page(), "team": wb.team_page(NOON)}
                script += "\nvm.runInContext(" + json.dumps(
                    "Object.assign(S, " + json.dumps(fixture, ensure_ascii=False) + ");uiLang='en';"
                    "for(const page of ['main','map','territory','me','relations','settings','cards','ach','brain']) {"
                    "S.page=page;S.overlay=null;render();}"
                    "for(const overlay of ['prep','desk','bigmap','me-pending','me-all','me-manage']) {"
                    "S.overlay=overlay;renderOverlay();}", ensure_ascii=False) + ",context);"
            finally:
                store.close()
            result = subprocess.run([node, '-e', script, str(STATIC)], text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_visible_content_batches_and_language_race(self):
        node = shutil.which('node')
        if not node:
            self.skipTest('环境没有 Node，无法执行前端')
        script = r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const nodes=Array.from({length:53},(_,i)=>({dataset:{},textContent:'待翻译内容'+i,isConnected:true,
 closest(){return false},getClientRects(){return [1]}}));
const hidden={dataset:{},textContent:'隐藏内容',isConnected:true,closest(){return false},getClientRects(){return []}};
const quote={dataset:{},textContent:'她说过的原话',isConnected:true,closest(){return true},getClientRects(){return [1]}};
nodes.push(hidden,quote);
const root={querySelectorAll(sel){return sel==='[data-ui-content]'?nodes:[]}};
const calls=[],waiters=[];
const context=vm.createContext({Map,Set,queueMicrotask,NodeFilter:{SHOW_TEXT:4},
 esc:x=>String(x),S:{overlay:null},document:{documentElement:{},querySelector(k){return k==='#page'?root:null},
 querySelectorAll(){return nodes},createTreeWalker(){return {nextNode(){return null}}}},
 api(path,payload){calls.push(payload.texts);return new Promise(resolve=>waiters.push(resolve))}});
vm.runInContext(fs.readFileSync(process.argv[1]+'/i18n.js','utf8'),context);
(async()=>{
 vm.runInContext("uiLang='en';localizeVisible()",context);await new Promise(setImmediate);
 assert.equal(calls.length,1);assert.equal(calls[0].length,50);
 assert(!calls[0].includes('隐藏内容'));assert(!calls[0].includes('她说过的原话'));
 vm.runInContext("uiLang='zh'",context);
 waiters.shift()({ok:true,texts:calls[0].map((_,i)=>'English '+i)});await new Promise(setImmediate);
 assert.equal(nodes[0].textContent,'待翻译内容0');assert.equal(calls.length,1);
 vm.runInContext("uiLang='en';localizeVisible()",context);await new Promise(setImmediate);
 assert.equal(nodes[0].textContent,'English 0');assert.equal(calls[1].length,3);
 waiters.shift()({ok:true,texts:['A','B','C']});await new Promise(setImmediate);
 assert.equal(nodes[52].textContent,'C');assert.equal(quote.textContent,'她说过的原话');
})().catch(e=>{console.error(e);process.exitCode=1});
'''
        result = subprocess.run([node, '-e', script, str(STATIC)], text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_forms_do_not_contain_translation_markup(self):
        app = (STATIC / 'app.js').read_text()
        self.assertIsNone(re.search(r'data-(?:key|agent|title)="\$\{uiText', app))
        self.assertIn('uiText(o.text, !o.mine)', app)
        self.assertIn('uiText(q.text, false)', app)
        for body in re.findall(r'<(?:textarea|option)\b[^>]*>(.*?)</(?:textarea|option)>', app, re.S):
            self.assertNotIn('uiText(', body)

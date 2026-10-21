// npm install --no-save playwright@1.62.1
// npx playwright install chromium
// node tests/ui_scene_choices.cjs
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const {chromium} = require('playwright');
const root = path.resolve(__dirname, '..');
const html = fs.readFileSync(path.join(root, 'ui/scene_choices.html'), 'utf8')
  .replace('/* CHOICE_APP */', fs.readFileSync(path.join(root, 'ui/scene_choices.js'), 'utf8'));
const groups = [
  {kind:'action', title:'Что я могу сделать', texts:['Подойти.', 'Сесть.', 'Выйти.']},
  {kind:'speech', title:'Что я могу сказать', texts:['Продолжай.', 'Нет.', 'Почему?']},
  {kind:'thought', title:'Что я могу подумать', texts:['Интересно.', 'Странно.', 'Посмотрим.']},
].map(g => ({...g, items:g.texts.map((text, i) => ({id:`${g.kind}-${i}`, text}))}));
const scene = {session_id:'8ab42c92-e6a3-4dfd-8a32-144c0a79f1e0', turn_number:2, scene_sha256:'a'.repeat(64), groups};

(async () => {
  const browser = await chromium.launch({headless:true,
    ...(process.env.CHROMIUM_PATH ? {executablePath:process.env.CHROMIUM_PATH,
      args:['--no-sandbox','--disable-dev-shm-usage','--no-zygote','--single-process',
        '--in-process-gpu','--use-gl=angle','--use-angle=swiftshader']} : {})});
  try {
    const page = await browser.newPage();
    const errors = [];
    page.on('pageerror', e => errors.push(e.message));
    async function load(mode = 'ok') {
      await page.goto('about:blank');
      await page.evaluate(({html, scene, mode}) => {
        window.requests = []; window.scene = scene;
        const iframe = document.createElement('iframe');
        iframe.style = 'border:0;width:100%;height:1100px';
        window.addEventListener('message', event => {
          const d = event.data;
          if (event.source !== iframe.contentWindow || d?.jsonrpc !== '2.0') return;
          window.requests.push(d);
          if (d.id) {
            let result = {};
            if (d.method === 'ui/initialize') result = {hostCapabilities:{message:{text:{}}}, hostContext:{theme:'light'}};
            if (d.method === 'tools/call') result = mode === 'stale' ? {isError:true} : {structuredContent:{current:true}};
            setTimeout(() => event.source.postMessage({jsonrpc:'2.0', id:d.id, result}, '*'), 10);
          }
          if (d.method === 'ui/notifications/initialized') iframe.contentWindow.postMessage({
            jsonrpc:'2.0', method:'ui/notifications/tool-result', params:{structuredContent:scene}}, '*');
        });
        iframe.srcdoc = html;
        document.body.style.margin = '0';
        document.body.append(iframe);
      }, {html, scene, mode});
      const frame = page.frameLocator('iframe');
      await frame.locator('input[type=checkbox]').last().waitFor();
      return frame;
    }
    let frame = await load();
    assert.equal(await frame.locator('input[type=checkbox]').count(), 9);
    const click = id => frame.locator(`[data-id="${id}"]`).click();
    const draft = () => frame.locator('#draft').inputValue();
    for (const id of ['action-1', 'speech-0', 'thought-1', 'action-0']) await click(id);
    assert.equal(await draft(), '(Сесть.) Продолжай. (Странно.) (Подойти.)');
    await click('speech-0');
    assert.equal(await draft(), '(Сесть.) (Странно.) (Подойти.)');
    await click('speech-0');
    assert.equal(await draft(), '(Сесть.) (Странно.) (Подойти.) Продолжай.');
    await frame.locator('#draft').fill('Мой текст.');
    await click('thought-0');
    assert.equal(await draft(), 'Мой текст. (Интересно.)');
    await click('thought-0');
    assert.equal(await draft(), 'Мой текст.');
    // Deselecting an option the player rewrote must not erase their words.
    await click('action-1');
    assert.equal(await draft(), 'Мой текст.');
    const exact = '  Моя реплика. (Мой жест.) (Моя мысль.)  ';
    await frame.locator('#draft').fill(exact);
    await page.evaluate(() => document.querySelector('iframe').contentWindow.postMessage({
      jsonrpc:'2.0', method:'ui/notifications/tool-result', params:{structuredContent:window.scene}}, '*'));
    assert.equal(await draft(), exact);
    await frame.locator('#send').click();
    await frame.locator('#status').filter({hasText:'Ход отправлен.'}).waitFor();
    const messages = await page.evaluate(() => window.requests.filter(r => r.method === 'ui/message'));
    assert.equal(messages.length, 1);
    assert.equal(messages[0].params.content[0].text, exact);
    assert.equal(await frame.locator('#send').isDisabled(), true);
    assert.equal(await page.evaluate(() => window.requests.filter(r => r.method === 'tools/call')[0].params.name), 'check_scene_choices');
    frame = await load('stale');
    await frame.locator('#draft').fill('Не терять текст');
    await frame.locator('#send').click();
    await frame.locator('#status').filter({hasText:'Сцена изменилась'}).waitFor();
    assert.equal(await draft(), 'Не терять текст');
    assert.equal(await page.evaluate(() => window.requests.filter(r => r.method === 'ui/message').length), 0);
    frame = await load();
    for (const [width, height] of [[375,812], [768,1024], [1440,900]]) {
      await page.setViewportSize({width,height});
      assert.equal(await frame.locator('body').evaluate(el => el.scrollWidth > innerWidth), false);
      if (process.env.UI_SCREENSHOT_DIR) await page.screenshot({path:path.join(process.env.UI_SCREENSHOT_DIR, `choices-${width}.png`),fullPage:true});
    }
    assert.deepEqual(errors, []);
    console.log('UI passed: 9 checkboxes, click order, deselect/reselect, manual edits, exact send, duplicate protection, stale scene, repeated result, 3 viewport widths.');
  } finally {await browser.close();}
})().catch(error => {console.error(error);process.exitCode = 1;});

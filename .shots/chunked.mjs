// 큰 파일 조각 업로드 확인: 업로드 → 점수 누락으로 실패 → 파일 다시 안 고르고 재제출 → 중복 거절
// B=... TEAM_PW=... BIG=/path/to/file node .shots/chunked.mjs
import { chromium } from '/home/seory0/projects/dageul/node_modules/playwright/index.mjs';

const B = process.env.B, TEAM = process.env.TEAM_PW, BIG = process.env.BIG;
const dir = new URL('.', import.meta.url).pathname;
const browser = await chromium.launch();
const p = await (await browser.newContext({ viewport: { width: 1280, height: 900 } })).newPage();
p.on('pageerror', err => console.log('pageerror', err.message));
p.on('console', m => m.type() === 'error' && console.log('console', m.text()));
await p.goto(B + '/login');
await p.fill('input[name=nick]', '대용량');
await p.fill('input[name=pw]', TEAM);
await p.click('button');
await p.waitForURL(B + '/');

await p.goto(B + '/p/1/submit');
await p.setInputFiles('input[name=file]', BIG);
await p.fill('input[name=title]', '큰 파일 테스트');
await p.fill('textarea[name=report]', '점수 줄 없음');
const t0 = Date.now();
await p.click('button.p');
// 진행 표시 한 번 찍기
await p.waitForFunction(() => /업로드 \d+%/.test(document.getElementById('up-status')?.textContent || ''));
await p.locator('form').screenshot({ path: `${dir}7-uploading.png` });
await p.waitForSelector('p.err', { timeout: 600000 });
console.log('upload+submit', ((Date.now() - t0) / 1000).toFixed(1) + 's');
console.log('err:', await p.textContent('p.err'));
console.log('kept:', await p.textContent('p.ok'));
await p.screenshot({ path: `${dir}8-kept-upload.png`, fullPage: true });

// 파일을 다시 고르지 않고 점수만 넣어서 제출
await p.fill('input[name=score]', '1234.5');
await p.click('button.p');
await p.waitForURL(/\/s\/\d+\?new=1/);
const sid = p.url().match(/\/s\/(\d+)/)[1];
console.log('submitted #' + sid, await p.locator('table.kv tr:has-text("파일") td:nth-child(2)').innerText());

// 같은 파일 다시 → 중복 거절
await p.goto(B + '/p/1/submit');
await p.setInputFiles('input[name=file]', BIG);
await p.fill('input[name=title]', '중복');
await p.fill('input[name=score]', '1');
await p.click('button.p');
await p.waitForSelector('p.err', { timeout: 600000 });
console.log('dup err:', await p.textContent('p.err'));
await browser.close();

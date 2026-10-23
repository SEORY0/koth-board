// 실제 브라우저 폼으로 로그인·문제 추가·제출·리드 처리까지 돌리고 화면을 찍는다
// B=http://127.0.0.1:8790 TEAM_PW=... LEAD_PW=... node .shots/e2e.mjs
import { chromium } from '/home/seory0/projects/dageul/node_modules/playwright/index.mjs';
import fs from 'node:fs';

const B = process.env.B, TEAM = process.env.TEAM_PW, LEAD = process.env.LEAD_PW;
const dir = new URL('.', import.meta.url).pathname;
const browser = await chromium.launch();
const shot = (p, name) => p.screenshot({ path: `${dir}${name}.png`, fullPage: true });
const login = async (nick, pw) => {
  const c = await browser.newContext({ viewport: { width: 1280, height: 900 } });
  const p = await c.newPage();
  await p.goto(B + '/login');
  await p.fill('input[name=nick]', nick);
  await p.fill('input[name=pw]', pw);
  await p.click('button');
  await p.waitForURL(B + '/');
  return p;
};

const anon = await browser.newPage({ viewport: { width: 1280, height: 900 } });
await anon.goto(B + '/login');
await shot(anon, '1-login');

const lead = await login('루비', LEAD);
await lead.goto(B + '/new');
await lead.fill('input[name=name]', 'wasm-koth');
await lead.fill('textarea[name=notes]', '로컬 채점: python verifier.py sol.wasm --seeds 50\n시간 기반 채점이라 재채점 점수로 정렬합니다.');
fs.writeFileSync('/tmp/verifier.py', 'print(1)\n');
await lead.setInputFiles('input[name=verifier]', '/tmp/verifier.py');
await lead.click('button.p');
await lead.waitForURL(/\/p\/1$/);

const subs = [
  ['철수', '빔서치 도입, 폭 32', '903.4', 'v1', 'beam.wasm'],
  ['영희', 'seed별 타임아웃 처리 + 캐시', '910.25', 'v1', 'cache.wasm'],
  ['민수', '휴리스틱 가중치 튜닝', '897.8', 'v0', 'tune.wasm'],
  ['지수', '+0.3% 미세조정', '903.9', 'v1', 'tiny.wasm'],
  ['하늘', 'SA로 후처리', '915.02', 'v1', 'sa.wasm'],
];
const pages = {};
for (const [nick, title, avg, ver, fname] of subs) {
  const p = (pages[nick] ??= await login(nick, TEAM));
  const f = '/tmp/' + fname;
  fs.writeFileSync(f, Buffer.from(nick + title + Math.random()));
  await p.goto(B + '/p/1/submit');
  await p.setInputFiles('input[name=file]', f);
  await p.fill('input[name=title]', title);
  await p.fill('textarea[name=report]',
    `[REPORT]\nverifier: ${ver}\nmin: ${(+avg - 20).toFixed(1)}\navg: ${avg}\nmax: ${(+avg + 15).toFixed(1)}\n[/REPORT]\n\n이전 파일 대비 개선:\n- ${title}\n`);
  await p.click('button.p');
  await p.waitForURL(/\/s\/\d+\?new=1/);
}
await shot(pages['철수'], '2-submitted');

const act = async (sid, action, value) => {
  await lead.goto(`${B}/s/${sid}`);
  const form = lead.locator(`form:has(button[value=${action}])`);
  if (value !== undefined) await form.locator('input[name=value]').fill(value);
  await form.locator(`button[value=${action}]`).click();
  await lead.waitForLoadState();
};
await act(1, 'claim');
await act(1, 'rescore', '903.1');
await act(1, 'accept');
await act(1, 'official', '901');
await act(4, 'reject', '+0.3%는 노이즈 범위');
await act(2, 'claim');
await act(2, 'rescore', '908.7');
await act(5, 'claim');
await shot(lead, '4-detail-lead');
await lead.goto(B + '/p/1');
await shot(lead, '3-problem');
await lead.goto(B + '/');
await shot(lead, '5-home');

const m = await browser.newContext({ viewport: { width: 390, height: 844 }, deviceScaleFactor: 2, isMobile: true });
await m.addCookies(await lead.context().cookies());
const mp = await m.newPage();
await mp.goto(B + '/p/1');
await shot(mp, '6-mobile');
await browser.close();
console.log('done');

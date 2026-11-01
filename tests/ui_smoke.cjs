process.chdir(require('path').resolve(__dirname,'..'));
const {JSDOM}=require('jsdom');const fs=require('fs');const assert=require('assert');
const child=require('child_process').spawn('python3',['server.py','--port','8876'],{env:{...process.env,JOBDESK_DATA:fs.mkdtempSync(require('os').tmpdir()+'/jobdesk-ui-')}});
process.on('exit',()=>child.kill());
(async()=>{
 await new Promise((resolve,reject)=>{child.stdout.once('data',resolve);child.once('error',reject)});
 const errors=[]; const dom=new JSDOM(fs.readFileSync('static/index.html','utf8'),{url:'http://localhost:8876',runScripts:'outside-only'});const w=dom.window;
 w.fetch=(url,opts)=>fetch(new URL(url,'http://127.0.0.1:8876'),opts);w.confirm=()=>true;
 w.HTMLDialogElement.prototype.showModal=function(){this.open=true};w.HTMLDialogElement.prototype.close=function(){this.open=false};
 w.addEventListener('error',e=>errors.push(e.error?.message||e.message));w.eval(fs.readFileSync('static/app.js','utf8'));
 const wait=async f=>{for(let i=0;i<150;i++){if(f())return;await new Promise(r=>setTimeout(r,20))}throw Error('Timeout: '+w.document.querySelector('#content').textContent.slice(0,300)+' / '+w.document.querySelector('#toast').textContent)};
 const q=s=>w.document.querySelector(s);const submit=s=>{const form=q(s),b=form.querySelector('button');form.dispatchEvent(new w.SubmitEvent('submit',{bubbles:true,cancelable:true,submitter:b}))};
 await wait(()=>q('.board'));assert(q('.welcome'));
 q('#add').click();q('#title').value='Manual QA';q('#company').value='Тестовая компания';q('#published_at').value=new Date().toISOString().slice(0,10);q('#description').value='Удалённая работа из любой страны. Тестирование API и веб-приложений.';submit('#job-form');
 await wait(()=>q('#edit-job'));assert(q('#modal').textContent.includes('По фильтрам подходит'));
 q('#letter').value='Здравствуйте! Прикладываю резюме.';q('#notes').value='<script>alert(1)</script>';q('#stage').value='interview';submit('#edit-job');
 await wait(()=>q('#edit-job')&&q('#notes').value.includes('<script>'));await new Promise(r=>setTimeout(r,100));
 q('#close-dialog').click();q('[data-view="table"]').click();assert(q('tbody').textContent.includes('Manual QA'));assert(q('tbody').textContent.includes('Интервью'));assert(q('[data-find-contact]'));assert(q('tbody').textContent.includes('Контакт ещё не искали'));
 q('[data-view="sources"]').click();assert(w.document.querySelectorAll('.source-row').length===5);q('#new-source').click();q('#name').value='Новый канал';q('#url').value='@qa_jobs';submit('#source-form');
 await wait(()=>!q('#dialog').open);assert(w.document.querySelectorAll('.source-row').length===6);
 q('[data-view="calendar"]').click();q('#new-event').click();q('#title').value='Созвон';q('#start').value='2026-11-05T15:00';submit('#event-form');
 await wait(()=>q('.event'));assert(q('.event').textContent.includes('Созвон'));
 q('[data-view="settings"]').click();assert(q('#include').value.includes('manual qa'));assert(q('#diagnose-hh'));assert(q('#serper-key'));q('#hh_email').value='qa@example.com';q('#exclude').value+='\nruby';submit('#settings-form');await wait(()=>q('#toast').textContent==='Настройки сохранены');
 q('[data-view="profile"]').click();q('#body').value='Проверенный опыт QA';submit('#profile-form');await wait(()=>q('#toast').textContent==='Профиль сохранён');
 q('[data-view="board"]').click();assert(q('.card').textContent.includes('Manual QA'));
 const state=await (await w.fetch('/api/state')).json();
 for(const [title,published_at] of [['QA с лета','2026-06-01'],['QA без даты','']]){
  const response=await w.fetch('/api/jobs',{method:'POST',headers:{'Content-Type':'application/json','X-JobDesk-Token':state.token},body:JSON.stringify({title,company:'Проверка даты',description:'Удалённая работа из любой страны. Тестирование API и веб-приложений.',published_at})});assert(response.ok);
 }
 q('[data-view="settings"]').click();submit('#settings-form');await wait(()=>q('#toast').textContent==='Настройки сохранены');q('[data-view="board"]').click();assert.strictEqual(w.document.querySelectorAll('.card').length,1);assert(!q('.board').textContent.includes('QA с лета'));assert(!q('.board').textContent.includes('QA без даты'));
 q('#job-filter').value='all';q('#job-filter').dispatchEvent(new w.Event('change'));assert.strictEqual(w.document.querySelectorAll('.card').length,3);
 for(const [title,days] of [['QA вчера',1],['QA позавчера',2]]){
  const response=await w.fetch('/api/jobs',{method:'POST',headers:{'Content-Type':'application/json','X-JobDesk-Token':state.token},body:JSON.stringify({title,company:'Проверка сортировки',description:'Удалённая работа из любой страны. Тестирование API и веб-приложений.',published_at:new Date(Date.now()-days*86400000).toISOString()})});const created=await response.json();assert(response.ok);
  const update=await w.fetch('/api/jobs/'+created.id,{method:'POST',headers:{'Content-Type':'application/json','X-JobDesk-Token':state.token},body:JSON.stringify({stage:'interview'})});assert(update.ok);
 }
 q('[data-view="profile"]').click();submit('#profile-form');await wait(()=>q('#toast').textContent==='Профиль сохранён');q('[data-view="board"]').click();
 assert.deepStrictEqual(Array.from(w.document.querySelectorAll('[data-stage="interview"] .card h3'),e=>e.textContent),['Manual QA','QA вчера','QA позавчера']);
 q('[data-view="table"]').click();assert.deepStrictEqual(Array.from(w.document.querySelectorAll('tbody tr td:first-child b'),e=>e.textContent),['Manual QA','QA вчера','QA позавчера','QA с лета','QA без даты']);
 assert.deepStrictEqual(Array.from(q('#collection-period').options,o=>o.value),['day','week','two_weeks','three_weeks','month']);
 q('#collection-period').value='week';q('#collection-period').dispatchEvent(new w.Event('change'));await wait(()=>q('#toast').textContent==='Период сбора сохранён');
 const afterPeriod=await (await w.fetch('/api/state')).json();assert.strictEqual(afterPeriod.settings.collection_period,'week');assert.strictEqual(afterPeriod.jobs[0].title,'Manual QA');assert.strictEqual(afterPeriod.jobs.at(-1).title,'QA без даты');
 q('[data-view="settings"]').click();assert(q('#hh-token'));q('#hh-token').value='test-private-token';submit('#hh-token-form');await wait(()=>q('#toast').textContent==='Токен HH сохранён');
 const exported=await (await w.fetch('/api/export')).text();assert(!exported.includes('test-private-token'));
 q('#add').click();q('#description').value='Middle QA Engineer\nКомпания: Plat4m.Tech\nУдалённая работа';q('#description').dispatchEvent(new w.Event('input'));await wait(()=>q('#title').value==='Middle QA Engineer');
 q('#title').value='Моя должность';q('#title').dispatchEvent(new w.Event('input'));q('#description').value='Senior QA Engineer\nНовое описание';q('#description').dispatchEvent(new w.Event('input'));await new Promise(r=>setTimeout(r,350));assert.strictEqual(q('#title').value,'Моя должность');q('#close-dialog').click();
 q('[data-view="table"]').click();q('tbody tr').click();await wait(()=>q('#edit-job'));
 const realFetch=w.fetch;let contactPending=true;
 w.fetch=async(url,options)=>{
  if(url==='/api/contacts/search')return new Response(JSON.stringify({started:true}));
  const response=await realFetch(url,options);
  if(url==='/api/state'){const body=await response.json();body.contact_running=contactPending?[1]:[];contactPending=false;return new Response(JSON.stringify(body))}
  if(url==='/api/jobs/1'){const body=await response.json();body.contact_status='found';body.contact_candidates=JSON.stringify([{value:'@hr_plat4mtech',kind:'telegram',source:'https://t.me/qa_jobs/42',method:'telegram',context:'HR компании'}]);return new Response(JSON.stringify(body))}
  return response;
 };
 q('#letter').value='Несохранённый текст письма';q('#find-job-contact').click();await wait(()=>q('#contact-results').textContent.includes('@hr_plat4mtech'));assert.strictEqual(q('#letter').value,'Несохранённый текст письма');assert(!q('#find-job-contact').disabled);w.fetch=realFetch;
 assert.deepStrictEqual(errors,[]);assert(!w.document.querySelector('script[src="alert(1)"]'));
 console.log('PASS: CRUD, sorting, periods, history, token privacy, title autofill without replacing edits, contact results refresh without losing unsaved letter. No JS errors.');dom.window.close();child.kill();
})().catch(e=>{console.error(e);child.kill();process.exitCode=1});

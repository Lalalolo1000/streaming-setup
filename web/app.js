'use strict';

let nodes=[];
let health=new Map();
let jobs={};
let updateState={running:false};
let recoveryGuard=null;
let currentView='simple';
let checkAllPromise=null;
let stateTimer=null;
let autoTimer=null;
let lastJobs={};
const AUTO_CHECK_MS=20000;
const CHECK_CONCURRENCY=6;

const $=id=>document.getElementById(id);
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const sleep=ms=>new Promise(r=>setTimeout(r,ms));

function setNotice(text,kind='',scope='all'){
  const el=$('notice');
  if(!text){el.hidden=true;el.textContent='';el.className='notice';el.dataset.scope='';return}
  el.dataset.scope=scope;
  if(scope==='admin'&&currentView!=='admin'){el.hidden=true;return}
  el.hidden=false;el.textContent=text;el.className=`notice ${kind}`;
}

function setView(view){
  currentView=view==='admin'?'admin':'simple';
  document.body.classList.toggle('admin-view',currentView==='admin');
  $('simple-view-button').classList.toggle('active',currentView==='simple');
  $('admin-view-button').classList.toggle('active',currentView==='admin');
  const notice=$('notice');
  if(currentView==='simple'&&notice.dataset.scope==='admin')notice.hidden=true;
}

function requestAdminView(){
  if(currentView==='admin')return;
  $('admin-warning-dialog').showModal();
}

function updateFavicon(activeCount){
  const total=Math.max(nodes.length||24,1);
  const active=`${String(activeCount).padStart(2,'0')}x`;
  const maximum=`${String(total).padStart(2,'0')}x`;
  const ratio=`${active}/${maximum}`;
  // Two lines keep the ratio legible even when the browser renders a tiny favicon.
  const svg=`<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64"><rect width="64" height="64" fill="#ffffff"/><rect x="1" y="1" width="62" height="62" fill="none" stroke="#111111" stroke-width="2"/><text x="32" y="28" text-anchor="middle" font-family="Arial,Helvetica,sans-serif" font-size="20" font-weight="700" fill="#111111">${active}</text><text x="32" y="48" text-anchor="middle" font-family="Arial,Helvetica,sans-serif" font-size="17" font-weight="700" fill="#111111">/${maximum}</text></svg>`;
  $('dynamic-favicon').href=`data:image/svg+xml,${encodeURIComponent(svg)}`;
  document.title=`${ratio} Streaming Setup`;
}

function sourceLabel(url){
  try{return new URL(url).hostname.replace(/^www\./,'')}
  catch{return 'Stream'}
}

function healthDisplay(raw){
  const key=String(raw||'unknown').toLowerCase();
  const table={
    live:['Live','live'],
    starting:['Startet','warning'],
    connecting:['Verbindet','warning'],
    retrying:['Neuer Versuch','warning'],
    cooldown:['Wartet','warning'],
    running:['Läuft','warning'],
    stopped:['Gestoppt','stopped'],
    supervisor_down:['Wächter ausgefallen','failed'],
    http_403:['403 · Zugriff abgelehnt','failed'],
    http_404:['404 · Nicht gefunden','failed'],
    source_unreachable:['Quelle nicht erreichbar','failed'],
    unsupported_url:['URL nicht unterstützt','failed'],
    no_stream:['Kein Stream verfügbar','failed'],
    player_error:['VLC-Fehler','failed'],
    stream_error:['Stream-Fehler','failed'],
    node_unreachable:['Nicht erreichbar','failed'],
    ssh_auth_failed:['Login fehlgeschlagen','failed'],
    ssh_host_key_changed:['SSH-Schlüssel geändert','failed'],
    ssh_failed:['SSH fehlgeschlagen','failed'],
    remote_command_failed:['Prüfung fehlgeschlagen','failed'],
    unknown:['Unbekannt','unknown']
  };
  return table[key]||[key.replaceAll('_',' '),'failed'];
}

function reasonDisplay(raw){
  const text=String(raw||'').trim();
  if(!text||text==='unknown')return '';
  const exact={
    'stream opened and player started':'Stream geöffnet, VLC läuft',
    'opening stream source':'Stream-Quelle wird geöffnet',
    'starting Streamlink':'Streamlink wird gestartet',
    'supervisor started':'Stream-Wächter gestartet',
    'supervisor stopped':'Stream-Wächter gestoppt',
    'supervisor is not running':'Stream-Wächter läuft nicht',
    'source returned HTTP 403 / Forbidden':'Quelle verweigert den Zugriff (HTTP 403)',
    'source returned HTTP 404 / Not Found':'Quelle wurde nicht gefunden (HTTP 404)',
    'Streamlink has no plugin for this URL':'Für diese URL gibt es kein passendes Streamlink-Plugin',
    'source is reachable but no playable stream is available':'Quelle erreichbar, aber aktuell kein abspielbarer Stream verfügbar',
    'could not reach the stream source':'Stream-Quelle konnte nicht erreicht werden',
    'VLC/player failed':'VLC konnte den Stream nicht abspielen',
    'waiting for Streamlink to open the source':'Warte darauf, dass Streamlink die Quelle öffnet',
    'legacy supervisor; press Start once to enable detailed health reporting':'Alter Stream-Wächter aktiv; im Admin einmal neu starten für detaillierten Status',
    'SSH connection failed':'SSH-Verbindung zum Raspberry Pi fehlgeschlagen',
    'SSH authentication failed':'SSH-Anmeldung am Raspberry Pi fehlgeschlagen',
    'Saved SSH host key no longer matches this Pi':'Gespeicherter SSH-Schlüssel passt nicht mehr zu diesem Raspberry Pi',
    'SSH failed':'SSH-Verbindung fehlgeschlagen',
    'Remote check failed':'Statusprüfung auf dem Raspberry Pi fehlgeschlagen',
    'Node could not be checked':'Raspberry Pi konnte nicht geprüft werden'
  };
  if(exact[text])return exact[text];
  if(text.startsWith('Streamlink exited unexpectedly'))return text.replace('Streamlink exited unexpectedly','Streamlink wurde unerwartet beendet');
  if(text.startsWith('too many quick failures; last error:'))return 'Zu viele schnelle Fehlversuche; letzter Fehler: '+reasonDisplay(text.split(':').slice(1).join(':').trim());
  if(text.includes('retrying in '))return reasonDisplay(text.split('; retrying in ')[0])+'; neuer Versuch in '+text.split('; retrying in ')[1].replace('s',' s');
  return text;
}

function stageDisplay(raw){
  const text=String(raw||'').trim();
  const table={
    'starting updater':'Update wird vorbereitet',
    'preflight':'Vorprüfung',
    'rebooting · waiting for shutdown':'Neustart: warte auf Herunterfahren',
    'rebooting · waiting for SSH':'Neustart: warte auf SSH',
    'apt update':'Paketlisten werden aktualisiert',
    'updating VLC':'VLC wird aktualisiert',
    'updating Streamlink':'Streamlink wird aktualisiert',
    'recovering after update failure':'Sichere Wiederherstellung nach Update-Fehler',
    'complete':'Abgeschlossen',
    'failed':'Fehlgeschlagen',
    'disable OverlayFS and reboot':'OverlayFS deaktivieren und neu starten',
    'update VLC and Streamlink':'VLC und Streamlink aktualisieren',
    'enable OverlayFS and reboot':'OverlayFS aktivieren und neu starten',
    'verify locked state':'Schreibschutz prüfen',
    'restart stream':'Stream neu starten',
    'leave stream stopped':'Stream gestoppt lassen',
    'master restarted during update; check recovery status':'Master wurde während des Updates neu gestartet; Wiederherstellung prüfen',
    'rebooting · waiting offline':'Neustart: warte auf Herunterfahren',
    'recovering after failure':'Sichere Wiederherstellung nach Fehler',
    'recovery complete':'Wiederherstellung abgeschlossen',
    'failed before updater launch':'Update konnte nicht gestartet werden',
    'starting recovery':'Wiederherstellung wird vorbereitet',
    'sending reboot command':'Neustart wird ausgelöst',
    'waiting for the Pi to shut down':'Warte, bis der Raspberry Pi herunterfährt',
    'reboot sent; shutdown was not observed, waiting for SSH':'Neustart ausgelöst; warte auf SSH',
    'Pi is offline; waiting for SSH':'Raspberry Pi ist offline; warte auf SSH',
    'master restarted; waiting for the Pi':'Master wurde neu gestartet; warte auf den Raspberry Pi',
    'Pi is back; starting configured stream':'Raspberry Pi ist wieder erreichbar; Stream wird gestartet',
    'sending shutdown command':'Herunterfahren wird ausgelöst',
    'waiting for Pi to go offline':'Warte, bis der Raspberry Pi offline ist',
    'Pi is shut down':'Raspberry Pi ist heruntergefahren',
    'shutdown command sent; offline state not observed':'Herunterfahren ausgelöst; Offline-Zustand konnte noch nicht bestätigt werden',
    'queued':'Aktion wartet auf Ausführung',
    'failed':'Fehlgeschlagen',
    'interrupted':'Unterbrochen'
  };
  if(table[text])return table[text];
  if(text.startsWith('reboot complete; stream state:'))return 'Neustart abgeschlossen; Stream-Status: '+healthDisplay(text.split(':').slice(1).join(':').trim())[0];
  if(text.endsWith(' queued'))return 'Aktion wartet auf Ausführung';
  if(text==='node removed from config while operation was pending')return 'Raspberry Pi wurde während einer laufenden Aktion aus der Konfiguration entfernt';
  if(text==='master restarted during shutdown request; check node state')return 'Master wurde während des Herunterfahrens neu gestartet; Zustand des Raspberry Pi prüfen';
  return text;
}


function humanError(raw){
  const text=String(raw||'').trim();
  const table={
    'software update is currently running':'Ein Software-Update läuft gerade.',
    'cannot change config while an update is running':'Die Konfiguration kann während eines Updates nicht geändert werden.',
    'finish reboot/shutdown operations before starting an update':'Warte, bis laufende Neustart-/Herunterfahr-Aktionen beendet sind.',
    'an interrupted-update recovery guard exists; recover it before starting another update':'Ein unterbrochenes Update muss zuerst sicher wiederhergestellt werden.',
    'an update/recovery is already running':'Ein Update oder eine Wiederherstellung läuft bereits.',
    'no interrupted-update recovery guard exists':'Es gibt keinen offenen Wiederherstellungsfall.',
    'Pi did not return to SSH after reboot':'Der Raspberry Pi war nach dem Neustart nicht wieder per SSH erreichbar.',
    'nodes.json must contain a JSON list':'nodes.json muss eine JSON-Liste enthalten.',
    'name is required':'Ein Name ist erforderlich.',
    'stream URL is required':'Eine Stream-URL ist erforderlich.',
    'target must look like pi@192.168.0.101 or pi@hostname':'Das SSH-Ziel muss z. B. pi@192.168.0.101 oder pi@hostname sein.',
    'connector may not contain spaces':'Der HDMI-Anschluss darf keine Leerzeichen enthalten.',
    'port must be an integer':'Der SSH-Port muss eine ganze Zahl sein.',
    'port must be between 1 and 65535':'Der SSH-Port muss zwischen 1 und 65535 liegen.',
    'invalid node index':'Ungültiger Raspberry-Pi-Eintrag.',
    'request body too large':'Die Anfrage ist zu groß.',
    'JSON body must be an object':'Ungültige Anfrage.',
    'sshpass is required for password-only SSH':'sshpass wird für die Passwort-Anmeldung benötigt.'
  };
  if(table[text])return table[text];
  if(text.startsWith('Pi rebooted, but stream start failed:'))return 'Der Raspberry Pi wurde neu gestartet, aber der Stream konnte danach nicht gestartet werden: '+text.split(':').slice(1).join(':').trim();
  if(text.includes('SSH password file missing')||text.includes('Required SSH password file is missing'))return 'Die gemeinsame SSH-Passwortdatei fehlt auf dem Master.';
  if(text.includes('sshpass is required'))return 'sshpass ist auf dem Master nicht installiert.';
  return text;
}

function jobFor(i){return jobs[nodes[i]?.target]||null}
function rowBusy(i){const j=jobFor(i);return !!(updateState.running||(j&&j.running))}

function render(){
  const list=$('node-list');
  if(!nodes.length){
    list.innerHTML='<div class="empty">Noch keine Raspberry Pis konfiguriert. Wechsle in die Admin-Ansicht, um einen hinzuzufügen.</div>';
    updateSummary();
    return;
  }
  list.innerHTML=nodes.map((n,i)=>`<article class="node" id="node-${i}">
    <div class="node-main">
      <div class="node-header">
        <div>
          <div class="node-name">${esc(n.name||`Raspberry Pi ${i+1}`)}</div>
          <div class="node-target-simple admin-only mono">${esc(n.target||'—')}:${esc(n.port??22)}</div>
        </div>
        <div class="status">
          <span id="dot-${i}" class="dot unknown"></span>
          <div>
            <div id="status-${i}" class="status-label">Wird geprüft …</div>
            <div id="detail-${i}" class="status-detail">—</div>
          </div>
        </div>
      </div>

      <div class="url-block">
        <div class="url-label">Stream-URL</div>
        <div class="node-url ${n.url?'':'empty-url'}">${esc(n.url||'Kein Stream-Link')}</div>
        <span class="source">Quelle: ${esc(n.url?sourceLabel(n.url):'noch nicht konfiguriert')}</span>
      </div>

      <div class="row-actions">
        <div class="row-actions-main">
          <button class="button primary" data-node-button type="button" onclick="openEdit(${i},false)">Bearbeiten</button>
        </div>
        <div class="row-actions-power">
          <button class="button warning" data-node-button type="button" onclick="powerAction(${i},'reboot')">Neu starten</button>
          <button class="button danger" data-node-button type="button" onclick="powerAction(${i},'shutdown')">Herunterfahren</button>
        </div>
      </div>
    </div>

    <div class="admin-row admin-only">
      <div class="admin-facts">
        <div class="fact"><div class="fact-title">SSH</div><div class="fact-value mono">${esc(n.target||'—')}:${esc(n.port??22)}</div></div>
        <div class="fact"><div class="fact-title">Qualität</div><div id="quality-${i}" class="fact-value">${esc(n.quality||'max480')}</div></div>
        <div class="fact"><div class="fact-title">Dateisystem</div><div id="lock-${i}" class="fact-value">Noch nicht geprüft</div></div>
        <div class="fact"><div class="fact-title">Versionen</div><div id="versions-${i}" class="fact-value">—</div></div>
        <div class="fact"><div class="fact-title">Letztes Update</div><div id="updated-${i}" class="fact-value">Unbekannt</div></div>
      </div>
      <div class="admin-actions">
        <button class="button" data-node-button type="button" onclick="runOne(${i},'start')">Starten / neu starten</button>
        <button class="button" data-node-button type="button" onclick="runOne(${i},'kill')">Stream stoppen</button>
        <button class="button" data-node-button type="button" onclick="showLogs(${i})">Protokoll</button>
        <button class="button" data-node-button type="button" onclick="silentCheck(${i},true)">Jetzt prüfen</button>
        <button class="button" data-node-button type="button" onclick="openEdit(${i},true)">Vollständige Konfiguration</button>
        <button class="button" data-node-button type="button" onclick="startUpdate(${i})">VLC + Streamlink aktualisieren</button>
      </div>
    </div>
  </article>`).join('');
  for(const [i,d] of health.entries())applyHealth(i,d);
  applyJobs();
  applyUpdate();
  updateSummary();
}

async function loadNodes(){
  const r=await fetch('/api/nodes');const d=await r.json();
  if(!r.ok)throw new Error(humanError(d.error)||'Konfiguration konnte nicht geladen werden');
  nodes=d.nodes||[];render();
}

function parseMachine(text){
  const d={};
  for(const line of String(text||'').split('\n')){const m=line.match(/^([A-Z][A-Z0-9_]*)=(.*)$/);if(m)d[m[1]]=m[2]}
  return d;
}

function transportHealth(failure,output=''){
  const map={node_unreachable:['node_unreachable','SSH connection failed'],ssh_auth_failed:['ssh_auth_failed','SSH authentication failed'],ssh_host_key_changed:['ssh_host_key_changed','Saved SSH host key no longer matches this Pi'],ssh_failed:['ssh_failed','SSH failed'],remote_command_failed:['remote_command_failed','Remote check failed']};
  const [state,reason]=map[failure]||['node_unreachable','Node could not be checked'];
  return {STREAM_HEALTH:state,STREAM_REASON:reason,STREAM_SOURCE:'SSH',SELECTED_STREAM:'unknown',LOCKED:'unknown',STREAMLINK_VERSION:'unknown',VLC_VERSION:'unknown',LAST_UPDATE_UTC:'unknown',TRANSPORT_OUTPUT:output};
}

function applyHealth(i,d){
  health.set(i,d);
  const raw=d.STREAM_HEALTH||(d.STATUS==='running'?'connecting':'stopped');
  const [label,cls]=healthDisplay(raw);
  const dot=$(`dot-${i}`),status=$(`status-${i}`),detail=$(`detail-${i}`);
  if(dot)dot.className=`dot ${cls}`;
  const card=$(`node-${i}`);
  if(card){card.classList.remove('state-live','state-warning','state-failed','state-stopped','state-unknown');card.classList.add(`state-${cls}`)}
  if(status)status.textContent=label;
  if(detail){
    const source=d.STREAM_SOURCE&&d.STREAM_SOURCE!=='unknown'?d.STREAM_SOURCE:sourceLabel(nodes[i]?.url||'');
    const quality=d.SELECTED_STREAM&&d.SELECTED_STREAM!=='unknown'?d.SELECTED_STREAM:'';
    const reason=d.STREAM_REASON&&d.STREAM_REASON!=='unknown'?reasonDisplay(d.STREAM_REASON):'';
    let text=raw==='live'?[source,quality].filter(Boolean).join(' · '):[source,reason].filter(Boolean).join(' · ');
    if(d.STREAM_RETRY_IN&&d.STREAM_RETRY_IN!=='0')text+=` · neuer Versuch in ${d.STREAM_RETRY_IN} s`;
    detail.textContent=text||'—';detail.title=detail.textContent;
    const statusBox=detail.closest('.status');if(statusBox)statusBox.title=detail.textContent;
  }
  const lock=$(`lock-${i}`);if(lock){lock.textContent=d.LOCKED==='yes'?'GESCHÜTZT':d.LOCKED==='no'?'SCHREIBBAR':'Unbekannt';lock.className=`fact-value ${d.LOCKED==='yes'?'good-text':d.LOCKED==='no'?'bad-text':''}`}
  const q=$(`quality-${i}`);if(q){const selected=d.SELECTED_STREAM&&d.SELECTED_STREAM!=='unknown'?d.SELECTED_STREAM:'—';q.textContent=`${d.STREAM_QUALITY_POLICY||nodes[i]?.quality||'max480'} · ${selected}`}
  const v=$(`versions-${i}`);if(v){let vlc=String(d.VLC_VERSION||'unknown');const m=vlc.match(/VLC (?:media player|version)\s+([^\s]+)/i);if(m)vlc=m[1];let sl=String(d.STREAMLINK_VERSION||'unknown').replace(/^streamlink\s+/i,'');v.textContent=`SL ${sl} · VLC ${vlc}`}
  const u=$(`updated-${i}`);if(u){const x=d.LAST_UPDATE_UTC;u.textContent=(!x||x==='unknown')?'Unbekannt':formatDate(x)}
  updateSummary();
}

function formatDate(value){const d=new Date(value);return Number.isNaN(d.getTime())?value:d.toLocaleString('de-DE')}

async function nodeCall(i,action){
  const r=await fetch(`/api/nodes/${i}/${action}`,{method:'POST'});const d=await r.json();
  if(!r.ok)throw new Error(humanError(d.error)||'Anfrage fehlgeschlagen');
  return d;
}

async function silentCheck(i,notify=false){
  if(rowBusy(i))return false;
  try{
    const d=await nodeCall(i,'check');
    if(d.ok)applyHealth(i,parseMachine(d.output||''));
    else applyHealth(i,transportHealth(d.failure,d.output));
    if(notify)setNotice(d.ok?`${nodes[i].name}: geprüft`:`${nodes[i].name}: ${healthDisplay((transportHealth(d.failure)).STREAM_HEALTH)[0]}`,d.ok?'good':'warn');
    return d.ok;
  }catch(e){applyHealth(i,transportHealth('node_unreachable',e.message));if(notify)setNotice(`${nodes[i].name}: ${e.message}`,'bad');return false}
}

async function poolIndices(indices,limit,fn){
  let cursor=0;const workers=Array.from({length:Math.min(limit,indices.length)},async()=>{while(true){const p=cursor++;if(p>=indices.length)return;await fn(indices[p])}});await Promise.all(workers)
}

async function silentCheckAll(notify=false){
  if(checkAllPromise)return checkAllPromise;
  checkAllPromise=(async()=>{
    const indices=nodes.map((_,i)=>i).filter(i=>!rowBusy(i));let failures=0;
    await poolIndices(indices,CHECK_CONCURRENCY,async i=>{if(!await silentCheck(i,false))failures++});
    $('last-check').textContent=`Zuletzt geprüft: ${new Date().toLocaleTimeString('de-DE', {hour:'2-digit',minute:'2-digit'})}`;
    if(notify)setNotice(failures?(failures===1?'1 Raspberry Pi konnte nicht geprüft werden':`${failures} Raspberry Pis konnten nicht geprüft werden`):`${indices.length} Raspberry Pis geprüft`,failures?'warn':'good');
    return failures;
  })();
  try{return await checkAllPromise}finally{checkAllPromise=null}
}

function updateSummary(){
  let live=0,waiting=0,failed=0,stopped=0,unknown=0,reachableRunning=0;
  nodes.forEach((_,i)=>{
    const job=jobFor(i);
    const d=health.get(i)||{};
    if(!job?.running&&d.STATUS==='running')reachableRunning++;
    if(job&&job.running){waiting++;return}
    const h=String(d.STREAM_HEALTH||'unknown').toLowerCase();
    if(h==='live')live++;else if(['starting','connecting','retrying','cooldown','running'].includes(h))waiting++;else if(h==='stopped')stopped++;else if(h==='unknown')unknown++;else failed++;
  });
  const parts=[`${reachableRunning}/${nodes.length} aktiv`];
  if(waiting)parts.push(`${waiting} warten`);if(failed)parts.push(`${failed} Probleme`);if(stopped)parts.push(`${stopped} gestoppt`);if(unknown)parts.push(`${unknown} ungeprüft`);
  $('summary').textContent=parts.join(' · ');
  updateFavicon(reachableRunning);
}

async function runOne(i,action){
  if(rowBusy(i)){setNotice(`${nodes[i].name}: Es läuft bereits eine Aktion`,'warn');return}
  const label=action==='start'?'Starte / starte neu':action==='kill'?'Stoppe Stream':'Führe Aktion aus';
  setNotice(`${label} ${nodes[i].name}…`);
  try{
    const d=await nodeCall(i,action);
    if(!d.ok){setNotice(`${nodes[i].name}: ${d.output||d.failure||'Befehl fehlgeschlagen'}`,'bad');return}
    setNotice(`${nodes[i].name}: ${action==='kill'?'Stream gestoppt':'Startbefehl angenommen'}`,'good');
    await sleep(action==='start'?1800:500);await silentCheck(i,false);
    if(action==='start'){setTimeout(()=>silentCheck(i,false),3500);setTimeout(()=>silentCheck(i,false),8000)}
  }catch(e){setNotice(`${nodes[i].name}: ${e.message}`,'bad')}
}

async function powerAction(i,kind){
  const node=nodes[i];
  const text=kind==='reboot'?`${node.name} neu starten?\n\nDer Server wartet, bis der Raspberry Pi wieder erreichbar ist, und startet danach automatisch den konfigurierten Stream.`:`${node.name} herunterfahren?\n\nDer Raspberry Pi bleibt ausgeschaltet, bis die Stromversorgung wiederhergestellt wird.`;
  if(!confirm(text))return;
  try{const d=await nodeCall(i,kind);if(!d.ok)throw new Error(humanError(d.error)||'Aktion fehlgeschlagen');jobs[node.target]=d.job;applyJobs();setNotice(`${node.name}: ${kind==='reboot'?'Neustart':'Herunterfahren'} gestartet`,'warn')}catch(e){setNotice(`${node.name}: ${e.message}`,'bad')}
}

async function runFleet(action){
  if(!nodes.length)return;
  if(action==='reboot'&&!confirm(`Alle ${nodes.length} Raspberry Pis neu starten?\n\nNach dem Neustart wird auf jedem Raspberry Pi der konfigurierte Stream wieder gestartet.`))return;
  if(action==='shutdown'&&!confirm(`Alle ${nodes.length} Raspberry Pis herunterfahren?\n\nSie bleiben ausgeschaltet, bis die Stromversorgung wiederhergestellt wird.`))return;
  if(action==='kill'&&!confirm(`Streams auf allen ${nodes.length} Raspberry Pis stoppen?`))return;
  setNotice('Aktion für alle Raspberry Pis wird gestartet …');
  let failed=0,done=0;
  await poolIndices(nodes.map((_,i)=>i),4,async i=>{
    try{
      if(action==='reboot'||action==='shutdown'){const d=await nodeCall(i,action);jobs[nodes[i].target]=d.job}
      else{const d=await nodeCall(i,action);if(!d.ok)failed++}
    }catch{failed++}
    finally{done++;setNotice(`Fortschritt: ${done}/${nodes.length}${failed?` · ${failed} fehlgeschlagen`:''}`,failed?'warn':'')}
  });
  applyJobs();
  if(action==='start'||action==='kill'){await sleep(1500);silentCheckAll(false)}
}

function applyJobs(){
  nodes.forEach((n,i)=>{
    const job=jobs[n.target];const row=$(`node-${i}`);if(!row)return;
    const buttons=row.querySelectorAll('[data-node-button]');
    if(job&&job.running){
      buttons.forEach(b=>b.disabled=true);
      const [label,cls]=job.kind==='reboot'?['Wird neu gestartet','warning']:['Wird heruntergefahren','warning'];
      const dot=$(`dot-${i}`),status=$(`status-${i}`),detail=$(`detail-${i}`);
      row.classList.remove('state-live','state-warning','state-failed','state-stopped','state-unknown');row.classList.add('state-warning');
      if(dot)dot.className=`dot ${cls}`;if(status)status.textContent=label;if(detail)detail.textContent=stageDisplay(job.message||job.stage||'');
    }else{
      buttons.forEach(b=>b.disabled=!!updateState.running);
      if(health.has(i))applyHealth(i,health.get(i));
    }
  });
  updateSummary();
}

async function refreshState(){
  try{
    const r=await fetch('/api/state');const d=await r.json();if(!r.ok)throw new Error(humanError(d.error)||'Serverstatus konnte nicht geladen werden');
    const previous=jobs;jobs=d.jobs||{};updateState=d.update||{running:false};recoveryGuard=d.recovery_guard||null;
    applyUpdate();applyRecovery();applyJobs();
    nodes.forEach((n,i)=>{const before=previous[n.target],after=jobs[n.target];if(before&&before.running&&after&&!after.running)setTimeout(()=>silentCheck(i,false),500)});
    lastJobs=previous;
  }catch(e){if(currentView==='admin')setNotice(`Serverstatus konnte nicht geladen werden: ${e.message}`,'bad','admin')}
}

function applyUpdate(){
  const strip=$('update-strip');const running=!!updateState.running;document.querySelectorAll('button[data-node-button], [data-fleet-action], .admin-panel button').forEach(b=>b.disabled=running);
  const has=running||updateState.started_at;
  const oldSuccess=!running&&updateState.returncode===0&&updateState.finished_at&&((Date.now()/1000)-updateState.finished_at>10);
  strip.hidden=!has||oldSuccess;
  if(!has||oldSuccess)return;
  $('update-title').textContent=running?'Software-Update läuft':(updateState.returncode===0?'Software-Update abgeschlossen':'Software-Update fehlgeschlagen');
  $('update-detail').textContent=[updateState.current_node||'',stageDisplay(updateState.stage||''),updateState.step?`Schritt ${updateState.step}/${updateState.total_steps||5}`:'',updateState.elapsed_seconds!=null?`${updateState.elapsed_seconds} s`:''].filter(Boolean).join(' · ');
  const pct=updateState.step?Math.max(2,Math.min(100,updateState.step/(updateState.total_steps||5)*100)):(running?2:100);$('update-progress').style.width=`${pct}%`;
  applyJobs();
}

function applyRecovery(){
  const banner=$('recovery-banner');
  if(!recoveryGuard){banner.hidden=true;return}
  banner.hidden=false;
  $('recovery-text').textContent=`${recoveryGuard.name||recoveryGuard.target||'Raspberry Pi'}: Ein Update wurde möglicherweise unterbrochen. Bevor ein neues Update möglich ist, muss geprüft werden, ob der Schreibschutz wieder sicher aktiv ist.`;
}

async function startUpdate(index){
  const label=index==null?`alle ${nodes.length} Raspberry Pis nacheinander`:nodes[index].name;
  if(!confirm(`VLC + Streamlink auf ${label} aktualisieren?\n\nWährend des Updates wird OverlayFS vorübergehend deaktiviert und der Raspberry Pi zweimal neu gestartet. Währenddessen nicht ausschalten.`))return;
  try{
    const body=index==null?{all:true}:{index};const r=await fetch('/api/update/start',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});const d=await r.json();if(!r.ok)throw new Error(humanError(d.error)||'Update konnte nicht gestartet werden');updateState=d;applyUpdate();setNotice('Update gestartet','warn','admin')
  }catch(e){setNotice(`Update: ${e.message}`,'bad','admin')}
}

async function startRecovery(){
  if(!confirm('Sichere Wiederherstellung starten?\n\nDabei wird geprüft, ob OverlayFS wieder aktiv ist. Falls nötig wird es aktiviert, der Raspberry Pi neu gestartet, der Schreibschutz geprüft und anschließend der Stream wieder gestartet.'))return;
  try{const r=await fetch('/api/recovery/start',{method:'POST'});const d=await r.json();if(!r.ok)throw new Error(humanError(d.error)||'Wiederherstellung konnte nicht gestartet werden');updateState=d;applyUpdate();setNotice('Sichere Wiederherstellung gestartet','warn','admin')}catch(e){setNotice(`Wiederherstellung: ${e.message}`,'bad','admin')}
}

async function showLogs(i){
  try{const d=await nodeCall(i,'logs');showDetails(`${nodes[i].name} · Stream-Protokoll`,d.output||'(Noch kein Protokoll vorhanden)')}catch(e){setNotice(`${nodes[i].name}: ${e.message}`,'bad')}
}

function showDetails(title,text){$('details-title').textContent=title;$('details-content').textContent=text;$('details-dialog').showModal()}

function openEdit(i,admin){
  const isNew=i==null;const n=isNew?{name:'',target:'pi@192.168.0.',port:22,url:'',quality:'max480',connector:'HDMI-A-1'}:nodes[i];
  $('edit-index').value=isNew?'':String(i);$('edit-name').value=n.name||'';$('edit-url').value=n.url||'';$('edit-target').value=n.target||'';$('edit-port').value=n.port??22;$('edit-quality').value=n.quality||'max480';$('edit-connector').value=n.connector||'HDMI-A-1';$('edit-title').textContent=isNew?'Raspberry Pi hinzufügen':admin?`${n.name} konfigurieren`:`${n.name} bearbeiten`;$('edit-error').hidden=true;$('edit-error').textContent='';
  $('edit-dialog').dataset.admin=admin?'1':'0';
  $('edit-dialog').classList.toggle('full-config',admin);
  $('edit-dialog').showModal();
}

async function saveEdit(){
  const index=$('edit-index').value===''?null:Number($('edit-index').value);const old=index==null?null:nodes[index];const admin=$('edit-dialog').dataset.admin==='1';
  const node={name:$('edit-name').value.trim(),url:$('edit-url').value.trim(),target:admin?$('edit-target').value.trim():old?.target,port:admin?Number($('edit-port').value):old?.port,quality:admin?$('edit-quality').value.trim()||'max480':old?.quality,connector:admin?$('edit-connector').value.trim()||'HDMI-A-1':old?.connector};
  if(index==null&&!admin){return}
  $('save-button').disabled=true;$('edit-error').hidden=true;
  try{
    const apply=!!old&&(node.url!==old.url||node.target!==old.target||Number(node.port)!==Number(old.port)||node.quality!==old.quality||node.connector!==old.connector);
    const r=await fetch('/api/config/node',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({index,node,apply})});const d=await r.json();if(!r.ok)throw new Error(humanError(d.error)||'Speichern fehlgeschlagen');
    if(d.apply&&!d.apply.ok){
      $('edit-dialog').close();await loadNodes();setNotice(`${node.name}: Konfiguration gespeichert, aber der Stream konnte nicht neu gestartet werden: ${d.apply.output||d.apply.failure||'unbekannter Fehler'}`,'bad');setTimeout(()=>silentCheck(d.index,false),800);return;
    }
    $('edit-dialog').close();await loadNodes();
    const warn=d.apply?.old_stop_warning;setNotice(warn?`${node.name}: Gespeichert und gestartet; beim Stoppen des alten Ziels gab es eine Warnung: ${warn}`:`${node.name}: gespeichert${apply?' und Stream neu gestartet':''}`,warn?'warn':'good');
    setTimeout(()=>silentCheck(d.index,false),1500);setTimeout(()=>silentCheck(d.index,false),5000)
  }catch(e){$('edit-error').textContent=e.message;$('edit-error').hidden=false}
  finally{$('save-button').disabled=false}
}

async function reloadConfig(){try{await loadNodes();await silentCheckAll(false);setNotice('Konfiguration neu geladen','good')}catch(e){setNotice(`Konfiguration konnte nicht neu geladen werden: ${e.message}`,'bad')}}

function openUpdateLog(){showDetails('Update- / Wiederherstellungsprotokoll',updateState.output||'(Noch keine Ausgabe)')}

$('simple-view-button').addEventListener('click',()=>setView('simple'));
$('admin-view-button').addEventListener('click',requestAdminView);
$('admin-warning-cancel').addEventListener('click',()=>$('admin-warning-dialog').close());
$('admin-warning-accept').addEventListener('click',()=>{$('admin-warning-dialog').close();setView('admin')});
$('save-button').addEventListener('click',saveEdit);
$('edit-close-button').addEventListener('click',()=>$('edit-dialog').close());
$('edit-cancel-button').addEventListener('click',()=>$('edit-dialog').close());
$('details-close').addEventListener('click',()=>$('details-dialog').close());
$('update-log-button').addEventListener('click',openUpdateLog);
$('check-all-button').addEventListener('click',()=>silentCheckAll(true));
$('update-all-button').addEventListener('click',()=>startUpdate(null));
$('add-node-button').addEventListener('click',()=>openEdit(null,true));
$('reload-button').addEventListener('click',reloadConfig);
$('recovery-button').addEventListener('click',startRecovery);
document.querySelectorAll('[data-fleet-action]').forEach(b=>b.addEventListener('click',()=>runFleet(b.dataset.fleetAction)));

window.openEdit=openEdit;window.powerAction=powerAction;window.runOne=runOne;window.silentCheck=silentCheck;window.startUpdate=startUpdate;window.showLogs=showLogs;

document.addEventListener('visibilitychange',()=>{if(!document.hidden){refreshState();silentCheckAll(false)}});

async function boot(){
  setView('simple');
  try{await loadNodes();await refreshState();await silentCheckAll(false);setNotice('','');}
  catch(e){setNotice(`Startfehler: ${e.message}`,'bad')}
  stateTimer=setInterval(refreshState,2000);
  autoTimer=setInterval(()=>{if(!document.hidden&&!updateState.running)silentCheckAll(false)},AUTO_CHECK_MS);
}
boot();

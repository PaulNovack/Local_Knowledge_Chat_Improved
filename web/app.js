const $ = id => document.getElementById(id);
const welcomeTemplate = $('welcome')?.cloneNode(true);
let socket = null, busy = false, transcript = [], current = null;
let introAudio = null, introAudioUrl = '', introFinish = null;
const introDialog = document.createElement('dialog');
introDialog.id = 'cdla-intro-dialog';
introDialog.setAttribute('aria-labelledby', 'cdla-intro-title');
introDialog.innerHTML = '<div class="intro-dialog-content"><h2 id="cdla-intro-title">CDLA study collection</h2><div id="cdla-intro-text"></div><div class="intro-dialog-actions"><button id="cdla-intro-close" class="secondary" type="button">Close</button></div></div>';
document.body.append(introDialog);
const introStylesheet = document.createElement('link');
introStylesheet.rel = 'stylesheet';
introStylesheet.href = '/cdla-intro.css';
document.head.append(introStylesheet);
const showAskAndTrain = location.origin === 'http://127.0.0.1:8000';
const training = {
  form: $('train-form'),
  question: $('train-question'),
  answer: $('train-answer'),
  routerFunction: $('train-router-function'),
  key: $('train-key'),
  status: $('train-status'),
  save: $('train-save'),
};
function setBusy(value) {
  busy = value; $('send').disabled = value || !$('topic').value;
  $('topic').disabled = value; $('refresh').disabled = value; $('clear').disabled = value;
  $('stop').hidden = !value;
  if (training.save) training.save.disabled = value || !training.question?.value.trim() || !training.answer?.value.trim();
}
function status(text) { $('status').textContent = text; }
function trainStatus(text) { if (training.status) training.status.textContent = text; }
function updateWelcome() {
  const welcome = $('welcome');
  if (!welcome) return;
  welcome.querySelector('#cdla-intro')?.remove();
  if ($('topic').value.toUpperCase() !== 'CDLA') return;
  const button = document.createElement('button');
  button.id = 'cdla-intro';
  button.type = 'button';
  button.className = 'secondary';
  button.textContent = 'Tell me how this works?';
  button.setAttribute('aria-label', 'Hear the spoken introduction to the CDLA collection');
  button.addEventListener('click', playCdlaIntro);
  welcome.append(button);
}
async function playCdlaIntro(event) {
  const button = event.currentTarget;
  button.disabled = true;
  status('Preparing the spoken introduction…');
  let audio = null;
  let audioUrl = '';
  const finish = () => {
    if (introFinish === finish) introFinish = null;
    if (introAudio === audio) introAudio = null;
    if (introAudioUrl === audioUrl && audioUrl) {
      URL.revokeObjectURL(audioUrl);
      introAudioUrl = '';
    }
    if (introDialog.open) introDialog.close();
    button.disabled = false;
  };
  try {
    const scriptResponse = await fetch('/cdla-intro.txt', {cache: 'no-store'});
    if (!scriptResponse.ok) throw new Error('Unable to load the CDLA introduction.');
    const text = (await scriptResponse.text()).trim();
    if (!text) throw new Error('The CDLA introduction is empty.');
    const response = await fetch('/api/speak', {
      method: 'POST',
      headers: {'content-type': 'application/json'},
      body: JSON.stringify({text}),
    });
    if (!response.ok) throw new Error('Unable to generate the spoken introduction.');
    audioUrl = URL.createObjectURL(await response.blob());
    introAudioUrl = audioUrl;
    audio = new Audio(audioUrl);
    introAudio = audio;
    introFinish = finish;
    $('cdla-intro-text').textContent = text;
    introDialog.showModal();
    audio.addEventListener('ended', finish, {once: true});
    audio.addEventListener('error', finish, {once: true});
    await audio.play();
    status('Playing the CDLA introduction…');
  } catch (error) {
    if (audio) audio.pause();
    finish();
    status(error.message || 'Unable to play the CDLA introduction.');
  }
}
$("cdla-intro-close").addEventListener('click', () => {
  introAudio?.pause();
  introFinish?.();
});
introDialog.addEventListener('cancel', event => {
  event.preventDefault();
  introAudio?.pause();
  introFinish?.();
});
function bubble(role, text) {
  $('welcome')?.remove();
  const card = document.createElement('article'); card.className = `message ${role}`;
  const label = document.createElement('div'); label.className = 'label'; label.textContent = role === 'user' ? 'YOU' : 'KNOWLEDGE ASSISTANT';
  const body = document.createElement('div'); body.className = 'body'; body.textContent = text;
  card.append(label, body); $('messages').append(card); scroll(); return {card, body};
}
function thinkingIndicator() {
  const wrap = document.createElement('div');
  wrap.className = 'thinking-indicator';
  for (let i = 0; i < 3; i += 1) {
    const dot = document.createElement('span');
    dot.className = 'thinking-dot';
    dot.style.animationDelay = `${i * 0.15}s`;
    wrap.append(dot);
  }
  const text = document.createElement('span');
  text.textContent = 'Thinking…';
  wrap.append(text);
  return wrap;
}
function scroll() { $('messages').scrollTop = $('messages').scrollHeight; }
function addSources(card, sources) {
  if (!sources.length) return;
  const details = document.createElement('details');
  const summary = document.createElement('summary'); summary.textContent = `${sources.length} retrieved excerpts · inspect sources`;
  details.append(summary);
  for (const s of sources) {
    const item = document.createElement('section'); item.className = 'source';
    const title = document.createElement('strong'); title.textContent = `[${s.id}] ${s.question}`;
    const text = document.createElement('p'); text.textContent = s.text;
    const meta = document.createElement('small'); meta.textContent = `${s.source || 'Imported data'} · similarity ${s.score}`;
    item.append(title, text, meta);
    try {
      const url = new URL(s.url);
      if (['https:', 'http:'].includes(url.protocol)) {
        const link = document.createElement('a'); link.href = url.href; link.target = '_blank'; link.rel = 'noopener noreferrer'; link.textContent = 'Open original source ↗'; item.append(link);
      }
    } catch (_) { /* Local data may have no public URL. */ }
    details.append(item);
  }
  card.append(details);
}
async function refresh() {
  try {
    const r = await fetch('/api/topics'); const data = await r.json();
    if (!r.ok) throw new Error(data.error || 'Unable to load topics');
    const prior = $('topic').value; $('topic').replaceChildren();
    for (const name of data.topics) { const o = document.createElement('option'); o.value = name; o.textContent = name.replaceAll('_', ' '); $('topic').append(o); }
    if (data.topics.includes(prior)) $('topic').value = prior;
    await refreshRouterFunctions();
    updateWelcome();
    if (!data.topics.length) status('No collections yet. Import a data file, then refresh.');
    const health = await (await fetch('/api/health')).json();
    $('health').textContent = health.ok ? `● Services ready · ${health.chat_model}` : '○ Services need attention — see /api/health';
  } catch (e) { $('health').textContent = e.message; }
  setBusy(false);
}
async function refreshRouterFunctions() {
  if (!training.routerFunction) return;
  const response = await fetch('/api/router-functions');
  const data = await response.json();
  if (!response.ok) throw new Error(data.detail || 'Unable to load router functions');
  training.routerFunction.replaceChildren();
  for (const name of data.routerFunctions) {
    const option = document.createElement('option');
    option.value = name;
    option.textContent = name || 'Blank';
    training.routerFunction.append(option);
  }
  training.routerFunction.value = '';
}
function chatWsUrl() {
  const url = new URL('/ws/chat', location.href);
  url.protocol = url.protocol === 'https:' ? 'wss:' : 'ws:';
  return url;
}
$('form').addEventListener('submit', event => {
  event.preventDefault(); const question = $('question').value.trim();
  if (!question || busy) return;
  if (window.handleStepVideoRequest?.(question)) {
    $('question').value = '';
    return;
  }
  if (!$('topic').value) return;
  const directionsWindow = window.prepareLocatorDirections?.(question, $('show-map')?.checked);
  bubble('user', question); const answer = bubble('assistant', '');
  answer.body.classList.add('thinking');
  answer.body.append(thinkingIndicator());
  const record = {question, answer: '', sources: [], topic: $('topic').value, complete: false, error: '', directionsWindow};
  transcript.push(record); current = record; setBusy(true); status('Connecting…');
  trainStatus('');
  $('question').value = '';
  const ws = new WebSocket(chatWsUrl()); socket = ws;
  ws.onopen = () => ws.send(JSON.stringify({topic: record.topic, question}));
  ws.onmessage = event => {
    try {
      const data = JSON.parse(event.data);
      if (data.type === 'token') {
        if (!record.answer) { answer.body.classList.remove('thinking'); answer.body.replaceChildren(); }
        record.answer += data.text;
        answer.body.textContent = record.answer;
        scroll();
      }
      if (data.type === 'sources') {
        record.sources = data.sources;
        addSources(answer.card, data.sources);
        window.resolveLocatorDirections?.(record.directionsWindow, data.sources, answer.card);
      }
      if (data.type === 'status') status(data.message);
      if (data.type === 'error') { record.error = data.message; status(data.message); if (!record.answer) { answer.body.classList.remove('thinking'); answer.body.textContent = data.message; } }
      if (data.type === 'done') {
        answer.body.classList.remove('thinking');
        record.complete = true;
        window.finishLocatorDirections?.(record.directionsWindow);
        status('Ready for your next question.');
        if (training.form && training.question && training.answer) {
          training.question.value = question;
          training.answer.value = record.answer;
          trainStatus('If the answer is wrong, edit it below and save the correction.');
          training.save && (training.save.disabled = !training.question.value.trim() || !training.answer.value.trim());
        }
      }
    } catch (_) { status('Received an invalid server response.'); ws.close(); }
  };
  ws.onerror = () => status('Connection failed. Check the backend terminal.');
  ws.onclose = () => {
    window.finishLocatorDirections?.(record.directionsWindow);
    if (socket === ws) { socket = null; setBusy(false); if (!record.complete) status(record.error || 'Response interrupted. You can try again.'); $('question').focus(); }
  };
});
$('stop').onclick = () => socket?.close();
$('question').addEventListener('keydown', e => { if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) { e.preventDefault(); $('form').requestSubmit(); } });
function clearChat() {
  transcript = [];
  introAudio?.pause();
  introFinish?.();
  if (introAudioUrl) URL.revokeObjectURL(introAudioUrl);
  introAudio = null;
  introAudioUrl = '';
  $('messages').replaceChildren();
  if (welcomeTemplate) $('messages').append(welcomeTemplate.cloneNode(true));
  updateWelcome();
  status('New conversation. Ask a question.');
}
$('clear').onclick = clearChat;
$('voice-chat') && ($('voice-chat').onclick = () => { location.href = '/chat-by-voice/'; });
$('voice-answer-chat') && ($('voice-answer-chat').onclick = () => { location.href = '/chat-and-answer-by-voice/'; });
$('type-voice') && ($('type-voice').onclick = () => { location.href = '/type-voice/'; });
$('view-responses') && ($('view-responses').onclick = () => { location.href = '/view-responses/'; });
$('topic').onchange = clearChat;
$('refresh').onclick = refresh;
$('download').onclick = () => {
  const blob = new Blob([JSON.stringify(transcript, null, 2)], {type: 'application/json'});
  const url = URL.createObjectURL(blob); const a = document.createElement('a'); a.href = url; a.download = 'knowledge-chat.json'; a.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
};
if (training.form) {
  training.form.addEventListener('input', () => setBusy(busy));
  training.form.addEventListener('submit', async event => {
    event.preventDefault();
    if (!training.question.value.trim() || !training.answer.value.trim() || busy) return;
    training.save.disabled = true;
    trainStatus('Saving correction…');
    try {
      const r = await fetch('/api/train', {
        method: 'POST',
        headers: {'content-type': 'application/json'},
        body: JSON.stringify({
          topic: $('topic').value,
          question: training.question.value.trim(),
          answer: training.answer.value.trim(),
          routerFunction: training.routerFunction.value,
          key: training.key.value.trim() === '' ? '' : Number(training.key.value),
        }),
      });
      const data = await r.json();
      if (!r.ok) throw new Error(data.error || data.detail || 'Unable to save correction');
      trainStatus('Saved to Q&A and Qdrant.');
    } catch (error) {
      trainStatus(error.message);
    } finally {
      training.save.disabled = busy || !training.question.value.trim() || !training.answer.value.trim();
    }
  });
}
if ($('ask-train')) {
  $('ask-train').hidden = !showAskAndTrain;
  if (showAskAndTrain) $('ask-train').onclick = () => { location.href = '/ask-and-train/'; };
}
if ($('back-chat')) $('back-chat').onclick = () => { location.href = '/'; };
refresh();

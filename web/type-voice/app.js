const $ = id => document.getElementById(id);

let socket = null;
let busy = false;
let transcript = [];
let lastReplyAudioUrl = '';

function setStatus(text) { $('status').textContent = text; }
function setSpeakStatus(text) { $('speak-status').textContent = text; }
function setHealth(text) { $('health').textContent = text; }
function setBusy(nextBusy) {
  busy = nextBusy;
  $('send').disabled = nextBusy;
  $('question').disabled = nextBusy;
}
function bubble(role, text) {
  $('welcome')?.remove();
  const card = document.createElement('article');
  card.className = `message ${role}`;
  const label = document.createElement('div');
  label.className = 'label';
  label.textContent = role === 'user' ? 'YOU' : 'KNOWLEDGE ASSISTANT';
  const body = document.createElement('div');
  body.className = 'body';
  body.textContent = text;
  card.append(label, body);
  $('messages').append(card);
  $('messages').scrollTop = $('messages').scrollHeight;
  return { card, body };
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
function addSources(card, sources) {
  if (!sources.length) return;
  const details = document.createElement('details');
  const summary = document.createElement('summary');
  summary.textContent = `${sources.length} retrieved excerpts · inspect sources`;
  details.append(summary);
  for (const s of sources) {
    const item = document.createElement('section');
    item.className = 'source';
    const title = document.createElement('strong');
    title.textContent = `[${s.id}] ${s.question}`;
    const text = document.createElement('p');
    text.textContent = s.text;
    const meta = document.createElement('small');
    meta.textContent = `${s.source || 'Imported data'} · similarity ${s.score}`;
    item.append(title, text, meta);
    try {
      const url = new URL(s.url);
      if (['https:', 'http:'].includes(url.protocol)) {
        const link = document.createElement('a');
        link.href = url.href;
        link.target = '_blank';
        link.rel = 'noopener noreferrer';
        link.textContent = 'Open original source ↗';
        item.append(link);
      }
    } catch (_) {
      // No public URL for local data.
    }
    details.append(item);
  }
  card.append(details);
}
async function loadTopics() {
  try {
    const response = await fetch('/api/topics');
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || 'Unable to load topics');
    const current = $('topic').value;
    $('topic').replaceChildren();
    for (const name of data.topics) {
      const option = document.createElement('option');
      option.value = name;
      option.textContent = name.replaceAll('_', ' ');
      $('topic').append(option);
    }
    if (data.topics.includes(current)) $('topic').value = current;
    if (!data.topics.length) setStatus('No collections yet. Import data, then refresh.');
  } catch (error) {
    setHealth(error.message);
  }
}
async function refreshHealth() {
  try {
    const response = await fetch('/api/health');
    const data = await response.json();
    const voiceState = data.checks?.piper === 'ok' ? 'Amy voice ready' : 'Amy voice unavailable';
    $('health').textContent = data.ok
      ? `● Services ready · ${data.chat_model} · ${voiceState}`
      : '○ Services need attention — see /api/health';
  } catch (error) {
    setHealth(error.message);
  }
}
function clearConversation() {
  transcript = [];
  $('messages').replaceChildren();
  $('question').value = '';
  $('question').focus();
  setStatus('Ready. Type a prompt and send it.');
  setSpeakStatus('Ready to speak replies.');
  $('replay-answer').disabled = true;
  if (lastReplyAudioUrl) {
    URL.revokeObjectURL(lastReplyAudioUrl);
    lastReplyAudioUrl = '';
  }
  $('reply-audio').removeAttribute('src');
}
function ensureSelectedTopic() {
  const topicSelect = $('topic');
  const options = Array.from(topicSelect.options)
    .map(option => option.value)
    .filter(value => value && value !== 'Loading…');
  if (options.length && !options.includes(topicSelect.value)) {
    topicSelect.value = options[0];
  }
  return options.includes(topicSelect.value);
}
function getWsUrl() {
  const url = new URL('/ws/chat', location.href);
  url.protocol = url.protocol === 'https:' ? 'wss:' : 'ws:';
  return url;
}
async function askQuestion(question) {
  if (window.handleStepVideoRequest?.(question)) return null;
  if (!ensureSelectedTopic()) {
    throw new Error('Select a knowledge collection first, then ask again.');
  }
  bubble('user', question);
  const answer = bubble('assistant', '');
  answer.body.classList.add('thinking');
  answer.body.append(thinkingIndicator());
  const record = { question, answer: '', sources: [], topic: $('topic').value, complete: false, error: '' };
  transcript.push(record);
  setStatus('Connecting to chat…');
  const ws = new WebSocket(getWsUrl());
  socket = ws;
  return await new Promise((resolve, reject) => {
    let settled = false;
    const done = () => {
      if (!settled) {
        settled = true;
        resolve(record.answer);
      }
    };
    const fail = error => {
      if (!settled) {
        settled = true;
        reject(error);
      }
    };
    ws.onopen = () => ws.send(JSON.stringify({ topic: record.topic, question }));
    ws.onmessage = event => {
      try {
        const data = JSON.parse(event.data);
        if (data.type === 'token') {
          if (!record.answer) {
            answer.body.classList.remove('thinking');
            answer.body.replaceChildren();
          }
          record.answer += data.text;
          answer.body.textContent = record.answer;
          $('messages').scrollTop = $('messages').scrollHeight;
        }
        if (data.type === 'sources') {
          record.sources = data.sources;
          addSources(answer.card, data.sources);
        }
        if (data.type === 'status') setStatus(data.message);
        if (data.type === 'error') {
          record.error = data.message;
          setStatus(data.message);
          if (!record.answer) {
            answer.body.classList.remove('thinking');
            answer.body.textContent = data.message;
          }
        }
        if (data.type === 'done') {
          answer.body.classList.remove('thinking');
          record.complete = true;
          setStatus('Ready for the next prompt.');
          setBusy(false);
          done();
        }
      } catch (_) {
        setBusy(false);
        setStatus('Received an invalid server response.');
        ws.close();
        fail(new Error('Received an invalid server response.'));
      }
    };
    ws.onerror = () => {
      setBusy(false);
      fail(new Error('Connection failed. Check the backend terminal.'));
    };
    ws.onclose = () => {
      if (socket === ws) socket = null;
      if (!settled) {
        setBusy(false);
        fail(new Error(record.error || 'Response interrupted.'));
      }
    };
  });
}
async function speakAnswer(text) {
  const cleaned = text.trim();
  if (!cleaned) return;
  setSpeakStatus('Preparing spoken reply…');
  const response = await fetch('/api/speak', {
    method: 'POST',
    headers: {'content-type': 'application/json'},
    body: JSON.stringify({ text: cleaned }),
  });
  const audio = await response.blob();
  if (!response.ok) {
    const detail = await audio.text().catch(() => 'Unable to synthesize speech');
    throw new Error(detail || 'Unable to synthesize speech');
  }
  if (lastReplyAudioUrl) URL.revokeObjectURL(lastReplyAudioUrl);
  lastReplyAudioUrl = URL.createObjectURL(audio);
  const player = $('reply-audio');
  player.src = lastReplyAudioUrl;
  $('replay-answer').disabled = false;
  try {
    await player.play();
    setSpeakStatus('Reply played.');
  } catch (error) {
    throw error;
  }
}
async function handleSubmit(event) {
  event.preventDefault();
  if (busy) return;
  const question = $('question').value.trim();
  if (!question) {
    setStatus('Type a prompt before sending.');
    return;
  }
  setBusy(true);
  setStatus('Sending prompt…');
  try {
    const answer = await askQuestion(question);
    if (answer) await speakAnswer(answer);
    $('question').value = '';
    $('question').focus();
  } catch (error) {
    setStatus(error.message);
    setSpeakStatus(error.message);
  } finally {
    setBusy(false);
  }
}

$('form').addEventListener('submit', handleSubmit);
$('question').addEventListener('keydown', e => {
  if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) {
    e.preventDefault();
    $('form').requestSubmit();
  }
});
$('clear').onclick = clearConversation;
$('topic').onchange = clearConversation;
$('refresh').onclick = async () => { await loadTopics(); await refreshHealth(); };
$('download').onclick = () => {
  const blob = new Blob([JSON.stringify(transcript, null, 2)], { type: 'application/json' });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement('a');
  anchor.href = url;
  anchor.download = 'type-voice.json';
  anchor.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
};
$('replay-answer').onclick = async () => {
  try {
    const player = $('reply-audio');
    if (!player.src) return;
    await player.play();
    setSpeakStatus('Reply played.');
  } catch (error) {
    setSpeakStatus(error.message);
  }
};
$('back-chat').onclick = () => { location.href = '/'; };
$('voice-chat').onclick = () => { location.href = '/chat-by-voice/'; };
$('voice-answer-chat').onclick = () => { location.href = '/chat-and-answer-by-voice/'; };

loadTopics();
refreshHealth();
setStatus('Ready. Type a prompt and send it.');
setSpeakStatus('Ready to speak replies.');
